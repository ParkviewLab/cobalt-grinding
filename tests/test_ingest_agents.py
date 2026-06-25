# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.ingest.agents.

The agents call an `AnthropicProvider`-shaped object's `complete()`
method. Tests use a Mock that returns a canned Message with TextBlock
content, then assert on the parsed output.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cobalt_grinding.ingest.agents import (
    ExtractedEntity,
    ExtractedGlossaryTerm,
    _parse_json_list,
    extract_entities,
    extract_glossary,
    summarize,
    synthesize_source_overview,
)

# ---- helpers ----


def _text_message(text: str) -> Any:
    """Mock an Anthropic Message with one TextBlock containing `text`."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    return msg


def _fake_provider(reply_text: str) -> AsyncMock:
    """A fake AnthropicProvider whose `complete()` returns `reply_text`."""
    provider = MagicMock()
    provider.complete = AsyncMock(return_value=_text_message(reply_text))
    return provider


# ---- summarizer ----


async def test_summarize_returns_text() -> None:
    provider = _fake_provider("This is a summary of the file.\n\nIt has two paragraphs.")
    out = await summarize(provider, body="some body content", source_label="foo.md")
    assert "summary" in out
    # Provider was called with the summarizer system prompt.
    args = provider.complete.await_args.kwargs
    assert "summarization" in args["system"].lower()


async def test_summarize_returns_empty_on_provider_error() -> None:
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=RuntimeError("API down"))
    out = await summarize(provider, body="content")
    assert out == ""


# ---- source-overview synthesizer ----


async def test_synthesize_source_overview_returns_text() -> None:
    provider = _fake_provider("This is a Python package for X. It exposes Y and Z.")
    out = await synthesize_source_overview(
        provider,
        section_summaries=[
            ("src/foo.py", "Foo module: defines class A"),
            ("src/bar.py", "Bar module: helpers for Foo"),
        ],
        source_label="example-repo",
    )
    assert "Python" in out


async def test_synthesize_source_overview_empty_when_no_summaries() -> None:
    """No section summaries → no LLM call, returns empty."""
    provider = MagicMock()
    provider.complete = AsyncMock()
    out = await synthesize_source_overview(provider, section_summaries=[], source_label="empty")
    assert out == ""
    provider.complete.assert_not_awaited()


async def test_synthesize_source_overview_empty_when_all_summaries_empty() -> None:
    provider = MagicMock()
    provider.complete = AsyncMock()
    out = await synthesize_source_overview(
        provider,
        section_summaries=[("a.md", ""), ("b.md", "")],
        source_label="empty",
    )
    assert out == ""
    provider.complete.assert_not_awaited()


# ---- entity extractor ----


async def test_extract_entities_parses_clean_json() -> None:
    reply = """[
        {"name": "Anthropic", "aliases": [], "kind": "org", "snippet": "founded by Anthropic"},
        {"name": "Claude Opus", "aliases": ["Opus"], "kind": "product", "snippet": "Claude Opus 4.7"}
    ]"""
    provider = _fake_provider(reply)
    entities = await extract_entities(provider, body="some content")
    assert len(entities) == 2
    assert entities[0].name == "Anthropic"
    assert entities[0].kind == "org"
    assert entities[1].aliases == ["Opus"]


async def test_extract_entities_strips_markdown_fence() -> None:
    """LLMs often wrap JSON in ```json fences despite being told not to."""
    reply = """```json
[{"name": "X", "aliases": [], "kind": "concept", "snippet": ""}]
```"""
    provider = _fake_provider(reply)
    entities = await extract_entities(provider, body="some content")
    assert len(entities) == 1
    assert entities[0].name == "X"


async def test_extract_entities_handles_prose_prefix() -> None:
    reply = """Sure, here are the entities I found:
[{"name": "Foo", "aliases": [], "kind": "concept", "snippet": "Foo bar"}]"""
    provider = _fake_provider(reply)
    entities = await extract_entities(provider, body="some content")
    assert len(entities) == 1
    assert entities[0].name == "Foo"


async def test_extract_entities_returns_empty_on_unparseable() -> None:
    provider = _fake_provider("not JSON at all")
    entities = await extract_entities(provider, body="content")
    assert entities == []


async def test_extract_entities_skips_items_missing_name() -> None:
    reply = """[
        {"name": "Valid", "aliases": [], "kind": "concept", "snippet": ""},
        {"aliases": ["NoName"], "kind": "concept", "snippet": ""},
        {"name": "", "aliases": [], "kind": "concept", "snippet": ""}
    ]"""
    provider = _fake_provider(reply)
    entities = await extract_entities(provider, body="content")
    assert len(entities) == 1
    assert entities[0].name == "Valid"


async def test_extract_entities_returns_empty_on_provider_error() -> None:
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=ConnectionError("no network"))
    entities = await extract_entities(provider, body="content")
    assert entities == []


# ---- glossary extractor ----


async def test_extract_glossary_parses_clean_json() -> None:
    reply = """[
        {"term": "MCP", "definition": "Model Context Protocol — a tool-use protocol", "snippet": "MCP server"},
        {"term": "LanceDB", "definition": "Embedded vector database", "snippet": "LanceDB-backed"}
    ]"""
    provider = _fake_provider(reply)
    terms = await extract_glossary(provider, body="content")
    assert len(terms) == 2
    assert terms[0].term == "MCP"
    assert "tool-use" in terms[0].definition


async def test_extract_glossary_skips_items_without_definition() -> None:
    reply = """[
        {"term": "Real", "definition": "good", "snippet": ""},
        {"term": "Bad", "definition": "", "snippet": ""}
    ]"""
    provider = _fake_provider(reply)
    terms = await extract_glossary(provider, body="content")
    assert len(terms) == 1
    assert terms[0].term == "Real"


# ---- _parse_json_list ----


@pytest.mark.parametrize(
    "text,expected_count",
    [
        ('[{"a":1}, {"b":2}]', 2),
        ('```json\n[{"a":1}]\n```', 1),
        ('Sure, here\'s the list: [{"a":1}]', 1),
        ("[]", 0),
        ("not json", 0),
        ("", 0),
        # Trailing prose after the list:
        ('[{"a":1}] and that\'s all', 1),
        # Non-list JSON:
        ('{"a": 1}', 0),
    ],
)
def test_parse_json_list(text: str, expected_count: int) -> None:
    parsed = _parse_json_list(text)
    assert isinstance(parsed, list)
    assert len(parsed) == expected_count


# ---- output dataclass sanity ----


def test_extracted_entity_defaults() -> None:
    e = ExtractedEntity(name="Foo")
    assert e.name == "Foo"
    assert e.aliases == []
    assert e.kind == "concept"
    assert e.snippet == ""


def test_extracted_glossary_term_required_fields() -> None:
    t = ExtractedGlossaryTerm(term="X", definition="y")
    assert t.term == "X"
    assert t.definition == "y"
    assert t.snippet == ""
