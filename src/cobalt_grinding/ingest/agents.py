# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""M3 Chunk 2 sub-agents.

Each agent is a single-purpose LLM call: prompt → narrow structured
output. They are deliberately small and stateless — given a
`LLMProvider` (or anything with the same `complete(system, messages,
...)` shape) and a `RawSection`, they return the agent-specific output
(prose summary, extracted entities, glossary terms).

Why call `app.host.provider.complete()` directly instead of
`app.host.run_agent(...)`:

- These agents do pure extraction; they shouldn't be reaching for MCP
  child tools. `run_agent` would retrieve top-K tools from the index
  and surface them — the LLM might then choose to call one (e.g.
  `smalt.search`), which is wrong for an extraction pass.
- The provider's `complete()` is the smallest possible LLM call:
  one round-trip, no tool plumbing, no loop overhead. Faster +
  cheaper for the per-file extraction work.

If a future sub-agent does need MCP tools (the code handler calling
`deco.parse_file`, for example), it should use `run_agent` — that's
why the host has both layers.

**Robustness over strictness:** if the LLM returns malformed JSON,
prose with markdown fences, or partial output, the parsers strip
common contamination and return what they can. An ingest run that
finds zero entities is acceptable; one that crashes a whole source's
ingest because of one bad JSON char is not.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cobalt_grinding.host.provider import AnthropicProvider

logger = logging.getLogger(__name__)


# Generous; an "extract every entity from this source" pass shouldn't
# blow this out unless the source is a thousand-line file mentioning
# dozens of things, which is rare.
_AGENT_MAX_TOKENS = 4096

# Cap body length we send to the LLM. Going much past this for a
# focused extraction pass burns tokens for diminishing returns; the
# summarizer particularly only needs the gist. If we want to handle
# very long sources, the right answer is chunking (Chunk 3 work, not
# Chunk 2).
_BODY_TOKEN_LIMIT_CHARS = 30000


# ---- agent output types ----


@dataclass(frozen=True)
class ExtractedEntity:
    """One entity surfaced by the entity_extractor."""

    name: str
    aliases: list[str] = field(default_factory=list)
    kind: str = "concept"
    snippet: str = ""


@dataclass(frozen=True)
class ExtractedGlossaryTerm:
    """One glossary term surfaced by the glossary_extractor."""

    term: str
    definition: str
    snippet: str = ""


# ---- prompts ----


SUMMARIZER_PROMPT = """\
You are a technical summarization agent for the Cobalt-Grinding LLM-Wiki.

Read the source file and write a concise 2-3 paragraph summary of what
this file is and what it covers. Focus on the file's purpose and the
domain it belongs to. Synthesize — do not restate code line-by-line or
copy text verbatim.

Return ONLY the summary prose. No markdown headers, no JSON, no
explanation of what you're doing.
"""


SOURCE_OVERVIEW_PROMPT = """\
You are writing the overview of a multi-file source for the
Cobalt-Grinding LLM-Wiki.

You'll be shown a list of section summaries — one paragraph per file
in the source. Synthesize them into a 2-3 paragraph "what this source
is" overview at the source level: what domain, what role it plays,
how the sections fit together. Don't list every section verbatim — a
TOC is auto-generated separately.

Return ONLY the overview prose. No markdown headers, no JSON, no
explanation of what you're doing.
"""


ENTITY_EXTRACTOR_PROMPT = """\
You extract named entities from a source file for indexing in the
Cobalt-Grinding LLM-Wiki. An entity is a real-world thing the source
talks about: a person, organization, product, repository, package,
place, or named concept (e.g. "Claude Opus", "FastMCP", "Anthropic").

DO NOT include: function names, class names, variable names, file
paths, or other code-internal references — those are tracked at the
section level, not as entities.

Return a JSON list. Each item has exactly these keys:
  - "name": canonical name as a short string
  - "aliases": list of other names this entity goes by (may be empty)
  - "kind": one of "person", "org", "product", "repo", "package", "place", "concept"
  - "snippet": short verbatim excerpt where the entity is mentioned (under 200 chars)

If the source mentions nothing entity-worthy, return [].

Return ONLY the JSON list. No prose, no markdown fence, no header.
"""


GLOSSARY_EXTRACTOR_PROMPT = """\
You extract glossary terms from a source file for indexing in the
Cobalt-Grinding LLM-Wiki. A glossary term is a domain-specific word,
phrase, or acronym the source defines or uses meaningfully, where a
reader of the wiki would benefit from a short standalone definition.

Skip generic English words. Include domain terms, acronyms, and
project-specific concepts. Skip anything you'd consider an "entity"
(a real-world named thing) — those go to the entity extractor.

Return a JSON list. Each item has exactly these keys:
  - "term": the term as it appears in the source
  - "definition": 1-3 sentence definition synthesized from the source
  - "snippet": short verbatim excerpt where the term is used (under 200 chars)

If the source has nothing glossary-worthy, return [].

Return ONLY the JSON list. No prose, no markdown fence, no header.
"""


# ---- agents ----


async def summarize(provider: AnthropicProvider, *, body: str, source_label: str = "") -> str:
    """Run the summarizer agent on a source file's body.

    Returns the synthesized summary text. Returns the empty string if
    the LLM produces nothing parseable — caller should fall back to a
    short auto-generated placeholder.
    """
    user_content = _build_user_content(body, source_label=source_label, task="summarize")
    try:
        message = await provider.complete(
            system=SUMMARIZER_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=_AGENT_MAX_TOKENS,
        )
    except Exception:
        logger.exception("summarizer agent failed")
        return ""
    return _extract_text(message).strip()


async def extract_entities(
    provider: AnthropicProvider, *, body: str, source_label: str = ""
) -> list[ExtractedEntity]:
    """Run the entity_extractor agent on a source file's body."""
    user_content = _build_user_content(body, source_label=source_label, task="extract entities")
    try:
        message = await provider.complete(
            system=ENTITY_EXTRACTOR_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=_AGENT_MAX_TOKENS,
        )
    except Exception:
        logger.exception("entity_extractor agent failed")
        return []
    raw_text = _extract_text(message)
    parsed = _parse_json_list(raw_text)
    out: list[ExtractedEntity] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = _str_or_none(item.get("name"))
        if not name:
            continue
        aliases_raw = item.get("aliases") or []
        aliases = [a for a in aliases_raw if isinstance(a, str) and a]
        kind = _str_or_none(item.get("kind")) or "concept"
        snippet = _str_or_none(item.get("snippet")) or ""
        out.append(ExtractedEntity(name=name, aliases=aliases, kind=kind, snippet=snippet))
    return out


async def synthesize_source_overview(
    provider: AnthropicProvider,
    *,
    section_summaries: list[tuple[str, str]],
    source_label: str = "",
) -> str:
    """Run the source-overview agent given a list of `(section_rel_path,
    section_summary)` tuples. Returns the synthesized overview, or
    empty string on agent failure / empty input."""
    if not section_summaries:
        return ""
    bullet_lines = [
        f"- `{rel_path}`: {summary}" for rel_path, summary in section_summaries if summary
    ]
    if not bullet_lines:
        return ""
    body = "Section summaries:\n\n" + "\n".join(bullet_lines)
    user_content = _build_user_content(
        body, source_label=source_label, task="synthesize source overview"
    )
    try:
        message = await provider.complete(
            system=SOURCE_OVERVIEW_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=_AGENT_MAX_TOKENS,
        )
    except Exception:
        logger.exception("source-overview agent failed")
        return ""
    return _extract_text(message).strip()


async def extract_glossary(
    provider: AnthropicProvider, *, body: str, source_label: str = ""
) -> list[ExtractedGlossaryTerm]:
    """Run the glossary_extractor agent on a source file's body."""
    user_content = _build_user_content(
        body, source_label=source_label, task="extract glossary terms"
    )
    try:
        message = await provider.complete(
            system=GLOSSARY_EXTRACTOR_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=_AGENT_MAX_TOKENS,
        )
    except Exception:
        logger.exception("glossary_extractor agent failed")
        return []
    raw_text = _extract_text(message)
    parsed = _parse_json_list(raw_text)
    out: list[ExtractedGlossaryTerm] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        term = _str_or_none(item.get("term"))
        definition = _str_or_none(item.get("definition"))
        if not term or not definition:
            continue
        snippet = _str_or_none(item.get("snippet")) or ""
        out.append(ExtractedGlossaryTerm(term=term, definition=definition, snippet=snippet))
    return out


# ---- helpers ----


def _build_user_content(body: str, *, source_label: str, task: str) -> str:
    """Frame the source body as a user turn for one of the agents.

    Truncates `body` to `_BODY_TOKEN_LIMIT_CHARS` to keep the prompt
    bounded. Truncation is a known limitation for very long sources —
    handled properly by chunking in Chunk 3.
    """
    truncated = ""
    if len(body) > _BODY_TOKEN_LIMIT_CHARS:
        body = body[:_BODY_TOKEN_LIMIT_CHARS]
        truncated = "\n\n[truncated — only the first portion of this source is shown to the agent]"
    header = f"Source: {source_label}\nTask: {task}\n\n" if source_label else f"Task: {task}\n\n"
    return f"{header}---\n{body}{truncated}\n---"


def _extract_text(message: Any) -> str:
    """Pull every text block out of an Anthropic Message; join them."""
    parts: list[str] = []
    for block in getattr(message, "content", []):
        block_type = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if block_type == "text" and isinstance(text, str):
            parts.append(text)
    return "".join(parts)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse_json_list(text: str) -> list[Any]:
    """Parse a JSON list from LLM output. Robust to common contamination
    patterns (markdown fences, leading/trailing prose). Returns `[]` on
    any parse failure — partial extraction beats aborting a whole ingest."""
    if not text or not text.strip():
        return []
    cleaned = text.strip()
    # Strip markdown fences like ```json ... ```
    cleaned = _FENCE_RE.sub("", cleaned).strip()
    # If the model wrote something like "Here's the list: [ ... ]", grab
    # the first balanced JSON list we can find.
    if not cleaned.startswith("["):
        first_bracket = cleaned.find("[")
        if first_bracket >= 0:
            cleaned = cleaned[first_bracket:]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Last-ditch attempt: trim trailing prose by walking back to the
        # last `]`.
        last_bracket = cleaned.rfind("]")
        if last_bracket > 0:
            try:
                parsed = json.loads(cleaned[: last_bracket + 1])
            except json.JSONDecodeError:
                logger.warning("agent output not JSON-parseable; returning []")
                return []
        else:
            logger.warning("agent output not JSON-parseable; returning []")
            return []
    if not isinstance(parsed, list):
        logger.warning(
            "agent output parsed but is not a list (got %s); returning []", type(parsed).__name__
        )
        return []
    return parsed


def _str_or_none(value: Any) -> str | None:
    """Coerce a value into a non-empty stripped string, or None."""
    if isinstance(value, str):
        s = value.strip()
        return s or None
    return None
