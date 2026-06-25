# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""M5 converse orchestrator — answer questions over the Smalt.

Public surface: `await ask(app, question)`.

**M5 first-cut design (per master plan):**

1. **Retrieval pass** — `retrieve.search(question, expand_hops=1)`
   gets the top-K relevant pages + 1-hop graph neighbors.
2. **Context hydration** — for each top hit, `retrieve.get_page()`
   fetches the full body. The bodies become the LLM's context.
3. **Answer pass** — one `provider.complete()` call with a focused
   system prompt that tells the LLM to answer **only** from the
   provided context, citing pages with `[page:<id>]` tokens.
4. **Citation validation** — parse `[page:<id>]` tokens out of the
   answer; mark each as valid (id was in the context) or
   invalid (id wasn't — likely hallucinated).
5. **Gap detection** — if the search returned zero hits OR every
   citation was invalid, the answer didn't actually use the corpus
   and we flag `gap_detected=True`. (We don't auto-emit; the caller
   decides via `wiki.report_gap`.)

**Why this shape, not the agentic-loop alternative**: M5 ends Phase 1
with a working ask-the-wiki demo. An iterative LLM-driven retrieval
loop (via `host.run_agent` over the child smalt-mcp tools) would be
more "agentic" but ships later. The Phase 1 bar is "given a question,
return a cited answer or admit you don't know," which this shape
hits in one round-trip with deterministic citation validation.

**Graceful degradation**:
- No `ANTHROPIC_API_KEY` → `ask()` raises `ConverseError` (the
  wiki.ask handler translates to a structured error). Unlike ingest,
  we can't degrade to "no LLM" mode for a Q&A — there's nothing
  useful to return.
- Smalt unreachable → `ConverseError` with the smalt error message.
- LLM call fails → `ConverseError`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from cobalt_grinding.retrieve import orchestrator as retrieve

if TYPE_CHECKING:
    from cobalt_grinding.app import App
    from cobalt_grinding.host.provider import AnthropicProvider

logger = logging.getLogger(__name__)


# Token cap per page body when building the LLM context. Long pages get
# truncated with a header; the answer pass tells the LLM to ask for
# more via `[page:id]` references but otherwise work from the snippet.
_PAGE_BODY_CHAR_LIMIT = 4000

# Hard cap on hits we feed as context to the LLM. Above this we burn
# tokens for diminishing return; below ~3 the corpus is undersampled.
_MAX_CONTEXT_PAGES = 8

# Cap on tokens the answer pass is allowed to produce.
_ANSWER_MAX_TOKENS = 2048


CONVERSE_SYSTEM_PROMPT = """\
You are the Cobalt-Grinding LLM-Wiki conversational agent. Your job is
to answer the user's question using ONLY the page excerpts provided in
the user message. Each excerpt has an id you must cite when you use it.

**Rules:**

1. Cite every claim with `[page:<id>]` tokens immediately after the
   sentence using the information. Multiple cites are fine:
   `[page:foo][page:bar]`.

2. If the provided pages don't contain enough to answer, say so
   explicitly: "I don't have enough information in the wiki to answer
   this." Don't invent. Don't guess.

3. If only part of the question is answerable, answer that part and
   explicitly flag what's missing.

4. Do NOT cite page ids that aren't in the provided excerpts. The
   citation list is closed — if a thought wasn't in an excerpt, you
   don't have a source for it.

5. Keep the answer concise. 1-4 paragraphs typically. Cited prose,
   not bulleted regurgitation of the context.
"""


# Regex matching `[page:<id>]` citation tokens in the LLM's output.
_CITATION_RE = re.compile(r"\[page:([A-Za-z0-9_\-:./]+)\]")


# ---- result types ----


@dataclass(frozen=True)
class Citation:
    """One `[page:<id>]` token surfaced by the LLM's answer."""

    page_id: str
    valid: bool  # True if `page_id` was in the context we provided

    def to_dict(self) -> dict[str, Any]:
        return {"page_id": self.page_id, "valid": self.valid}


@dataclass(frozen=True)
class AskResult:
    """What the orchestrator returns to the wiki.ask tool handler."""

    question: str
    answer: str
    citations: list[Citation] = field(default_factory=list)
    invalid_citations: list[str] = field(default_factory=list)
    hits_used: list[retrieve.SearchHit] = field(default_factory=list)
    gap_detected: bool = False
    truncated_context: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "citations": [c.to_dict() for c in self.citations],
            "invalid_citations": list(self.invalid_citations),
            "hits_used": [h.to_dict() for h in self.hits_used],
            "gap_detected": self.gap_detected,
            "truncated_context": self.truncated_context,
        }


class ConverseError(RuntimeError):
    """Raised for any unrecoverable converse failure. The wiki.ask tool
    handler catches this and returns a structured error payload."""


# ---- public surface ----


async def ask(
    app: App,
    question: str,
    *,
    top_k: int = _MAX_CONTEXT_PAGES,
    expand_hops: int = 1,
    max_tokens: int = _ANSWER_MAX_TOKENS,
    prior_messages: list[dict[str, str]] | None = None,
) -> AskResult:
    """Answer one question against the Smalt.

    Steps: retrieve top-K + 1-hop neighbors → hydrate → LLM-answer
    with `[page:id]` citations → validate citations against the
    context we sent → return.

    Args:
      question: free-text natural-language question.
      top_k: max number of pages to include as context (default 8).
      expand_hops: 1-hop graph expansion seeds from the top hits
        (default 1; pass 0 to disable).
      max_tokens: cap on the LLM's answer length.
      prior_messages: optional list of `{role, content}` dicts
        representing prior turns in a multi-turn conversation.
        When provided, the LLM sees these BEFORE the current
        question + excerpts, so follow-ups like "tell me more"
        and "what about X" work naturally. Caller (REPL) maintains
        the history; defaults to None = single-turn.

    Raises:
      `ConverseError` if: the question is empty, retrieval failed,
      the LLM provider isn't available, or the LLM call failed.
    """
    if not question or not question.strip():
        raise ConverseError("question is required (non-whitespace)")

    provider = _get_provider(app)

    try:
        search_result = await retrieve.search(app, question, top_k=top_k, expand_hops=expand_hops)
    except retrieve.RetrieveError as e:
        raise ConverseError(f"retrieval failed: {e}") from e

    # If retrieval produced no hits, we can still try to answer — but
    # the LLM will be told there are no excerpts, and the answer will
    # admit ignorance. Mark gap_detected so the caller can choose to
    # report it.
    contexts: list[_Context] = []
    truncated_context = False
    for hit in search_result.hits[:top_k]:
        try:
            page = await retrieve.get_page(app, hit.id)
        except retrieve.RetrieveError as e:
            logger.warning("get_page(%s) failed during converse: %s", hit.id, e)
            continue
        body = _coerce_body(page)
        if len(body) > _PAGE_BODY_CHAR_LIMIT:
            body = body[:_PAGE_BODY_CHAR_LIMIT]
            truncated_context = True
        contexts.append(_Context(id=hit.id, title=hit.title, body=body, hit=hit))

    user_content = _build_user_content(question, contexts)
    # Build the message list for the LLM: prior turns (if any) +
    # this turn's user message. We sanitize prior_messages to keep
    # only well-formed entries — caller-side history may have been
    # truncated or trimmed and we'd rather skip a bad entry than
    # crash the call.
    messages = _sanitize_prior_messages(prior_messages)
    messages.append({"role": "user", "content": user_content})

    try:
        message = await provider.complete(
            system=CONVERSE_SYSTEM_PROMPT,
            messages=messages,
            max_tokens=max_tokens,
        )
    except Exception as e:
        raise ConverseError(f"LLM call failed: {e}") from e

    answer_text = _extract_text(message).strip()
    context_ids = {ctx.id for ctx in contexts}
    citations, invalid_citations = _validate_citations(answer_text, context_ids)

    # Local gap signal: zero hits OR the LLM didn't cite anything from
    # the context (which means the answer was either "I don't know" or
    # a hallucination — either way, the corpus didn't help).
    gap_detected = len(search_result.hits) == 0 or (
        len(citations) > 0 and all(not c.valid for c in citations)
    )

    return AskResult(
        question=question,
        answer=answer_text,
        citations=citations,
        invalid_citations=invalid_citations,
        hits_used=[ctx.hit for ctx in contexts],
        gap_detected=gap_detected,
        truncated_context=truncated_context,
    )


# ---- internals ----


@dataclass(frozen=True)
class _Context:
    """One hit hydrated with its body, ready to feed to the LLM."""

    id: str
    title: str
    body: str
    hit: retrieve.SearchHit


def _sanitize_prior_messages(
    prior: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    """Filter `prior` to a list of well-formed `{role, content}`
    entries Anthropic will accept. Skip bad entries silently — caller
    history may be truncated / partially-malformed; better to send a
    cleaned subset than to fail the whole turn."""
    if not prior:
        return []
    out: list[dict[str, str]] = []
    for entry in prior:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = entry.get("content")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str) or not content:
            continue
        out.append({"role": role, "content": content})
    return out


def _build_user_content(question: str, contexts: list[_Context]) -> str:
    """Assemble the user-turn content: question + provided page excerpts.

    Each excerpt is fenced with its page id so the LLM's `[page:id]`
    citation matches what we hand it.
    """
    parts: list[str] = [
        "Question:",
        question,
        "",
        "Excerpts from the wiki — cite by id when you use one:",
    ]
    if not contexts:
        parts.append("(no relevant pages found)")
    else:
        for ctx in contexts:
            parts.append("")
            parts.append(f"--- page:{ctx.id} — title: {ctx.title} ---")
            parts.append(ctx.body)
        parts.append("")
        parts.append("--- end of excerpts ---")
    return "\n".join(parts)


def _extract_text(message: Any) -> str:
    """Pull every text block out of an Anthropic Message; join them."""
    parts: list[str] = []
    for block in getattr(message, "content", []):
        block_type = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if block_type == "text" and isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _coerce_body(page_payload: dict[str, Any]) -> str:
    """Extract the body text from a smalt.read_page response. Falls
    back to empty string if the body field is missing."""
    body = page_payload.get("body")
    if isinstance(body, str):
        return body
    return ""


def _validate_citations(answer: str, context_ids: set[str]) -> tuple[list[Citation], list[str]]:
    """Parse `[page:id]` tokens from `answer`; mark each as valid (id
    was in the context) or invalid. Returns `(citations, invalid_ids)`.

    Dedupes by page id — the same citation appearing twice in the
    answer is counted once.
    """
    seen: set[str] = set()
    citations: list[Citation] = []
    invalid: list[str] = []
    for match in _CITATION_RE.finditer(answer):
        page_id = match.group(1)
        if page_id in seen:
            continue
        seen.add(page_id)
        valid = page_id in context_ids
        citations.append(Citation(page_id=page_id, valid=valid))
        if not valid:
            invalid.append(page_id)
    return citations, invalid


def _get_provider(app: App) -> AnthropicProvider:
    """Return the host's LLM provider or raise ConverseError.

    Unlike ingest's `_get_provider_or_none`, converse can't gracefully
    fall back without an LLM — the whole job is to answer a question.
    """
    try:
        return cast("AnthropicProvider", app.host.provider)
    except RuntimeError as e:
        raise ConverseError(f"LLM provider not available (set ANTHROPIC_API_KEY): {e}") from e
