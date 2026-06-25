# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.converse.orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cobalt_grinding.app import App
from cobalt_grinding.config import Config, MCPConfig
from cobalt_grinding.converse.orchestrator import (
    ConverseError,
    _validate_citations,
    ask,
)
from cobalt_grinding.daemon.mcp_clients import CallResult

# ---- helpers ----


def _bare_app(tmp_path: Path) -> App:
    return App(
        Config(
            smalt_dir=tmp_path / "wiki",
            cobalt_grinding_dir=tmp_path / "cobalt_grinding",
            mcp=MCPConfig(clients={}),
        )
    )


def _ok(payload: dict[str, Any] | list[Any]) -> CallResult:
    block = MagicMock()
    block.text = json.dumps(payload)
    return CallResult(ok=True, is_error=False, content=[block], error_text=None)


def _text_message(text: str) -> Any:
    block = MagicMock()
    block.type = "text"
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    return msg


def _install_smalt_with_search_results(
    app: App,
    *,
    hits: list[dict[str, Any]],
    page_bodies: dict[str, str] | None = None,
) -> AsyncMock:
    """Install a smalt mock that returns the given hits and per-page bodies."""
    bodies = page_bodies or {}

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        if tool == "search":
            return _ok({"count": len(hits), "results": hits})
        if tool == "traverse":
            return _ok({"edges": [], "visited_nodes": [args["from_id"]], "truncated": False})
        if tool == "read_page":
            pid = args["page_id"]
            return _ok(
                {
                    "id": pid,
                    "title": next((h["title"] for h in hits if h["id"] == pid), pid),
                    "type": "concept",
                    "body": bodies.get(pid, "(no body)"),
                    "frontmatter": {},
                }
            )
        raise AssertionError(f"unexpected tool: {tool}")

    fake = MagicMock()
    fake.call_tool = AsyncMock(side_effect=dispatch)
    app._mcp_clients = fake  # type: ignore[assignment]
    return fake.call_tool


def _install_llm(app: App, answer_text: str) -> AsyncMock:
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(return_value=_text_message(answer_text))
    fake_host = MagicMock()
    fake_host.provider = fake_provider
    app._host = fake_host  # type: ignore[assignment]
    return fake_provider.complete


# ---- happy path ----


async def test_ask_returns_answer_with_valid_citations(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    hits = [
        {
            "id": "concept-mcp__1",
            "title": "MCP",
            "type": "concept",
            "score": 1.5,
            "snippet": "MCP is...",
            "aliases": [],
        },
        {
            "id": "concept-llm__2",
            "title": "LLM",
            "type": "concept",
            "score": 1.0,
            "snippet": "LLM stands for...",
            "aliases": [],
        },
    ]
    _install_smalt_with_search_results(
        app,
        hits=hits,
        page_bodies={
            "concept-mcp__1": "MCP is the Model Context Protocol, a tool-use protocol.",
            "concept-llm__2": "An LLM is a Large Language Model.",
        },
    )
    answer = (
        "MCP is the Model Context Protocol [page:concept-mcp__1]. An LLM is a "
        "Large Language Model [page:concept-llm__2]."
    )
    _install_llm(app, answer)

    result = await ask(app, "What is MCP and what is an LLM?")

    assert "Model Context Protocol" in result.answer
    assert len(result.citations) == 2
    assert all(c.valid for c in result.citations)
    assert result.invalid_citations == []
    assert result.gap_detected is False
    assert len(result.hits_used) == 2


async def test_ask_flags_invalid_citations(tmp_path: Path) -> None:
    """If the LLM cites a page id we didn't send as context, it's
    flagged. Likely hallucination."""
    app = _bare_app(tmp_path)
    _install_smalt_with_search_results(
        app,
        hits=[
            {
                "id": "real-page__a",
                "title": "Real",
                "type": "concept",
                "score": 1.0,
                "snippet": "real content",
                "aliases": [],
            }
        ],
        page_bodies={"real-page__a": "Real content body"},
    )
    answer = "Foo per [page:real-page__a]. Also bar per [page:made-up-id__b]."
    _install_llm(app, answer)

    result = await ask(app, "Tell me about Foo and Bar")
    assert len(result.citations) == 2
    valid_ids = [c.page_id for c in result.citations if c.valid]
    invalid_ids = [c.page_id for c in result.citations if not c.valid]
    assert valid_ids == ["real-page__a"]
    assert invalid_ids == ["made-up-id__b"]
    assert result.invalid_citations == ["made-up-id__b"]
    # gap_detected is False because at least one valid citation was used.
    assert result.gap_detected is False


async def test_ask_sets_gap_detected_when_no_hits(tmp_path: Path) -> None:
    """Zero search hits → gap_detected, regardless of what the LLM says."""
    app = _bare_app(tmp_path)
    _install_smalt_with_search_results(app, hits=[])
    _install_llm(app, "I don't have enough information to answer.")
    result = await ask(app, "What about an entirely unrelated topic?")
    assert result.hits_used == []
    assert result.gap_detected is True


async def test_ask_sets_gap_detected_when_all_citations_invalid(tmp_path: Path) -> None:
    """Hits existed but the LLM didn't cite any of them — instead cited
    made-up ids. Marks gap_detected: the corpus didn't actually help."""
    app = _bare_app(tmp_path)
    _install_smalt_with_search_results(
        app,
        hits=[
            {
                "id": "real-page__a",
                "title": "Real",
                "type": "concept",
                "score": 1.0,
                "snippet": "",
                "aliases": [],
            }
        ],
        page_bodies={"real-page__a": "body"},
    )
    _install_llm(app, "Foo per [page:not-real__x]. Bar per [page:also-not-real__y].")
    result = await ask(app, "anything")
    assert result.gap_detected is True
    assert all(not c.valid for c in result.citations)


async def test_ask_passes_prior_messages_to_llm(tmp_path: Path) -> None:
    """Multi-turn: prior_messages get prepended to the LLM's messages
    list. The current question + excerpts are appended as the final
    user turn."""
    app = _bare_app(tmp_path)
    hits = [
        {
            "id": "concept-x__1",
            "title": "X",
            "type": "concept",
            "score": 1.0,
            "snippet": "X is...",
            "aliases": [],
        }
    ]
    _install_smalt_with_search_results(
        app, hits=hits, page_bodies={"concept-x__1": "X is a thing."}
    )
    fake_complete = _install_llm(app, "X is described by [page:concept-x__1].")

    prior = [
        {"role": "user", "content": "what is the project about?"},
        {"role": "assistant", "content": "It's about X."},
    ]
    result = await ask(app, "tell me more about X", prior_messages=prior)

    # Verify the LLM saw the prior turns + a new user turn.
    args = fake_complete.await_args.kwargs
    messages = args["messages"]
    assert len(messages) == 3
    assert messages[0]["role"] == "user"
    assert "what is the project about" in messages[0]["content"]
    assert messages[1]["role"] == "assistant"
    assert "It's about X" in messages[1]["content"]
    # Final turn is the new question + excerpts (built by _build_user_content).
    assert messages[2]["role"] == "user"
    assert "tell me more about X" in messages[2]["content"]
    assert "concept-x__1" in messages[2]["content"]  # excerpt id

    # And the answer flows back as normal.
    assert "described by" in result.answer


async def test_ask_sanitizes_bad_prior_messages(tmp_path: Path) -> None:
    """Malformed prior_messages (wrong role, empty content, non-dict)
    are skipped rather than crashing the call."""
    app = _bare_app(tmp_path)
    _install_smalt_with_search_results(app, hits=[])
    fake_complete = _install_llm(app, "I don't have info.")

    bad_prior = [
        {"role": "user", "content": "good question"},
        {"role": "system", "content": "system prompts not allowed here"},  # bad role
        {"role": "user", "content": ""},  # empty content
        "not a dict",  # not a dict
        {"role": "assistant"},  # missing content
        {"role": "assistant", "content": "good answer"},
    ]
    await ask(app, "another question", prior_messages=bad_prior)  # type: ignore[arg-type]

    messages = fake_complete.await_args.kwargs["messages"]
    # Only 2 prior entries survived + the new user turn = 3.
    assert len(messages) == 3
    assert messages[0]["content"] == "good question"
    assert messages[1]["content"] == "good answer"


async def test_ask_empty_prior_messages_is_single_turn(tmp_path: Path) -> None:
    """When `prior_messages=None` (or []), behavior is identical to
    pre-multi-turn — just one user message goes to the LLM."""
    app = _bare_app(tmp_path)
    _install_smalt_with_search_results(app, hits=[])
    fake_complete = _install_llm(app, "no info.")

    await ask(app, "q", prior_messages=None)
    assert len(fake_complete.await_args.kwargs["messages"]) == 1

    fake_complete.reset_mock()
    await ask(app, "q", prior_messages=[])
    assert len(fake_complete.await_args.kwargs["messages"]) == 1


async def test_ask_handles_zero_citation_answer(tmp_path: Path) -> None:
    """If the LLM legitimately says 'I don't know' with no citations,
    that's a valid answer — not a hallucination. gap_detected stays
    False (search did return hits)."""
    app = _bare_app(tmp_path)
    _install_smalt_with_search_results(
        app,
        hits=[
            {
                "id": "page__1",
                "title": "P",
                "type": "concept",
                "score": 1.0,
                "snippet": "",
                "aliases": [],
            }
        ],
        page_bodies={"page__1": "body"},
    )
    _install_llm(app, "I don't have enough information in the wiki to answer this.")
    result = await ask(app, "obscure question")
    assert result.citations == []
    # Hits existed → gap_detected reflects "search worked" — caller
    # can still report a gap if they want to.
    assert result.gap_detected is False


async def test_ask_truncates_long_page_bodies(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    long_body = "x" * 10000  # over the 4000-char cap
    _install_smalt_with_search_results(
        app,
        hits=[
            {
                "id": "big__1",
                "title": "Big",
                "type": "concept",
                "score": 1.0,
                "snippet": "",
                "aliases": [],
            }
        ],
        page_bodies={"big__1": long_body},
    )
    _install_llm(app, "answer [page:big__1]")
    result = await ask(app, "anything")
    assert result.truncated_context is True


async def test_ask_dedupes_repeated_citations(tmp_path: Path) -> None:
    """Citing the same page id twice in the answer counts once."""
    app = _bare_app(tmp_path)
    _install_smalt_with_search_results(
        app,
        hits=[
            {
                "id": "p__1",
                "title": "P",
                "type": "concept",
                "score": 1,
                "snippet": "",
                "aliases": [],
            }
        ],
        page_bodies={"p__1": "body"},
    )
    _install_llm(app, "X [page:p__1]. Y [page:p__1]. Z [page:p__1].")
    result = await ask(app, "q")
    assert len(result.citations) == 1
    assert result.citations[0].page_id == "p__1"


# ---- error paths ----


async def test_ask_rejects_empty_question(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    with pytest.raises(ConverseError, match="question is required"):
        await ask(app, "")
    with pytest.raises(ConverseError, match="question is required"):
        await ask(app, "   ")


async def test_ask_raises_when_no_llm_provider(tmp_path: Path) -> None:
    """Unlike ingest, converse can't degrade gracefully without an LLM."""
    app = _bare_app(tmp_path)
    # No host installed → app.host raises RuntimeError.
    with pytest.raises(ConverseError, match="LLM provider not available"):
        await ask(app, "What is X?")


async def test_ask_raises_on_retrieval_failure(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    _install_llm(app, "irrelevant")
    fake = MagicMock()
    # Force smalt.search to fail.
    fake.call_tool = AsyncMock(
        return_value=CallResult(ok=False, is_error=True, content=[], error_text="smalt down")
    )
    app._mcp_clients = fake  # type: ignore[assignment]
    with pytest.raises(ConverseError, match="retrieval failed"):
        await ask(app, "anything")


async def test_ask_raises_when_llm_call_fails(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    _install_smalt_with_search_results(app, hits=[])
    # LLM call raises.
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(side_effect=RuntimeError("rate limit"))
    fake_host = MagicMock()
    fake_host.provider = fake_provider
    app._host = fake_host  # type: ignore[assignment]
    with pytest.raises(ConverseError, match="LLM call failed"):
        await ask(app, "anything")


# ---- citation parsing ----


def test_validate_citations_parses_tokens() -> None:
    answer = "Foo [page:a__1] and bar [page:b__2] but also [page:c__3]."
    citations, invalid = _validate_citations(answer, context_ids={"a__1", "b__2"})
    assert {c.page_id for c in citations} == {"a__1", "b__2", "c__3"}
    valid_ids = {c.page_id for c in citations if c.valid}
    assert valid_ids == {"a__1", "b__2"}
    assert invalid == ["c__3"]


def test_validate_citations_handles_section_ids() -> None:
    """Section-id pattern `<source>::<rel-path>` must parse correctly
    (the `::` is part of the id)."""
    answer = "From section [page:dir-foo__1::src/main.py]."
    citations, _invalid = _validate_citations(answer, context_ids={"dir-foo__1::src/main.py"})
    assert len(citations) == 1
    assert citations[0].valid is True
    assert citations[0].page_id == "dir-foo__1::src/main.py"


def test_validate_citations_returns_empty_for_no_tokens() -> None:
    citations, invalid = _validate_citations("No citations here.", context_ids=set())
    assert citations == []
    assert invalid == []
