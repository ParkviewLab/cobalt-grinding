# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.retrieve.orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cobalt_grinding.app import App
from cobalt_grinding.config import Config, MCPConfig
from cobalt_grinding.daemon.mcp_clients import CallResult
from cobalt_grinding.retrieve.orchestrator import (
    ExpansionEdge,
    RetrieveError,
    SearchHit,
    get_page,
    list_gaps,
    report_gap,
    search,
    traverse,
)

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


def _err(message: str) -> CallResult:
    return CallResult(ok=False, is_error=True, content=[], error_text=message)


def _toolside_err(payload: dict[str, Any]) -> CallResult:
    block = MagicMock()
    block.text = json.dumps(payload)
    return CallResult(ok=True, is_error=True, content=[block], error_text=None)


def _install_dispatch(app: App, dispatch_fn: Any) -> AsyncMock:
    fake = MagicMock()
    fake.call_tool = AsyncMock(side_effect=dispatch_fn)
    app._mcp_clients = fake  # type: ignore[assignment]
    return fake.call_tool


# ---- search ----


async def test_search_returns_hits_from_smalt(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        assert tool == "search"
        assert args["query"] == "anthropic"
        assert args["top_k"] == 5
        return _ok(
            {
                "count": 2,
                "results": [
                    {
                        "id": "org-anthropic__1",
                        "title": "Anthropic",
                        "type": "entity",
                        "score": 1.5,
                        "snippet": "Anthropic founded...",
                        "aliases": ["AnthropicPBC"],
                    },
                    {
                        "id": "concept-mcp__2",
                        "title": "MCP",
                        "type": "concept",
                        "score": 0.9,
                        "snippet": "Model Context...",
                        "aliases": [],
                    },
                ],
            }
        )

    _install_dispatch(app, dispatch)
    result = await search(app, "anthropic", top_k=5)
    assert len(result.hits) == 2
    assert result.hits[0].id == "org-anthropic__1"
    assert result.hits[0].score == 1.5
    assert result.gap_detected is False
    assert result.expansion_edges == []


async def test_search_with_expansion_collects_traverse_edges(tmp_path: Path) -> None:
    """expand_hops > 0 triggers `smalt.traverse` calls for the top hits."""
    app = _bare_app(tmp_path)

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        if tool == "search":
            return _ok(
                {
                    "count": 1,
                    "results": [
                        {
                            "id": "hit-1",
                            "title": "Hit 1",
                            "type": "concept",
                            "score": 1.0,
                            "snippet": "snippet",
                            "aliases": [],
                        }
                    ],
                }
            )
        if tool == "traverse":
            assert args["from_id"] == "hit-1"
            assert args["hops"] == 1
            return _ok(
                {
                    "from_id": "hit-1",
                    "hops": 1,
                    "edges": [
                        {"from_id": "hit-1", "to_id": "neighbor-a", "label": "mentions"},
                        {"from_id": "hit-1", "to_id": "neighbor-b", "label": "defines"},
                    ],
                    "count": 2,
                    "visited_nodes": ["hit-1", "neighbor-a", "neighbor-b"],
                    "truncated": False,
                }
            )
        raise AssertionError(f"unexpected: {tool}")

    _install_dispatch(app, dispatch)
    result = await search(app, "test", expand_hops=1)
    assert len(result.hits) == 1
    assert len(result.expansion_edges) == 2
    assert {e.to_id for e in result.expansion_edges} == {"neighbor-a", "neighbor-b"}
    assert set(result.expanded_node_ids) == {"neighbor-a", "neighbor-b"}


async def test_search_expansion_dedupes_against_direct_hits(tmp_path: Path) -> None:
    """If traverse surfaces a node that's already in the direct hits,
    don't list it twice in `expanded_node_ids`."""
    app = _bare_app(tmp_path)

    async def dispatch(_client: str, tool: str, _args: dict[str, Any]) -> CallResult:
        if tool == "search":
            return _ok(
                {
                    "count": 2,
                    "results": [
                        {
                            "id": "hit-1",
                            "title": "1",
                            "type": "concept",
                            "score": 1,
                            "snippet": "",
                            "aliases": [],
                        },
                        {
                            "id": "hit-2",
                            "title": "2",
                            "type": "concept",
                            "score": 0.9,
                            "snippet": "",
                            "aliases": [],
                        },
                    ],
                }
            )
        if tool == "traverse":
            return _ok(
                {
                    "edges": [
                        {"from_id": "hit-1", "to_id": "hit-2", "label": "rel"},
                        {"from_id": "hit-1", "to_id": "neighbor", "label": "rel"},
                    ],
                    "visited_nodes": [],
                    "truncated": False,
                }
            )
        raise AssertionError(f"unexpected: {tool}")

    _install_dispatch(app, dispatch)
    result = await search(app, "x", expand_hops=1)
    # hit-2 came from search, not expansion — only neighbor is "new".
    assert result.expanded_node_ids == ["neighbor"]


async def test_search_marks_gap_detected_on_zero_hits(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)

    async def dispatch(_client: str, tool: str, _args: dict[str, Any]) -> CallResult:
        if tool == "search":
            return _ok({"count": 0, "results": []})
        raise AssertionError(f"unexpected: {tool}")

    _install_dispatch(app, dispatch)
    result = await search(app, "nothing-matches")
    assert result.hits == []
    assert result.gap_detected is True
    # No auto-emit to ebony — caller must call report_gap.


async def test_search_passes_property_filters(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)

    captured: dict[str, Any] = {}

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        if tool == "search":
            captured.update(args)
            return _ok({"count": 0, "results": []})
        raise AssertionError(f"unexpected: {tool}")

    _install_dispatch(app, dispatch)
    await search(
        app,
        "x",
        property_filters={"glossary": True, "domain": "ml"},
    )
    assert captured["glossary"] is True
    assert captured["domain"] == "ml"


async def test_search_rejects_empty_query(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    with pytest.raises(RetrieveError, match="query is required"):
        await search(app, "")
    with pytest.raises(RetrieveError, match="query is required"):
        await search(app, "   ")


async def test_search_propagates_smalt_dispatch_failure(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    _install_dispatch(app, AsyncMock(return_value=_err("smalt-mcp not running")))
    with pytest.raises(RetrieveError, match=r"smalt-mcp\.search dispatch failed"):
        await search(app, "x")


async def test_search_propagates_smalt_tool_error(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    _install_dispatch(
        app,
        AsyncMock(return_value=_toolside_err({"error": "validation_error", "message": "bad arg"})),
    )
    with pytest.raises(RetrieveError, match="bad arg"):
        await search(app, "x")


async def test_search_expansion_failure_is_nonfatal(tmp_path: Path) -> None:
    """If smalt.traverse fails for one of the top hits, expansion just
    surfaces what it got — the search itself still returns the direct
    hits."""
    app = _bare_app(tmp_path)

    async def dispatch(_client: str, tool: str, _args: dict[str, Any]) -> CallResult:
        if tool == "search":
            return _ok(
                {
                    "count": 1,
                    "results": [
                        {
                            "id": "hit-1",
                            "title": "h",
                            "type": "concept",
                            "score": 1,
                            "snippet": "",
                            "aliases": [],
                        },
                    ],
                }
            )
        if tool == "traverse":
            return _err("transient failure")
        raise AssertionError(f"unexpected: {tool}")

    _install_dispatch(app, dispatch)
    result = await search(app, "x", expand_hops=1)
    assert len(result.hits) == 1
    assert result.expansion_edges == []


# ---- get_page ----


async def test_get_page_proxies_smalt_read_page(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        assert tool == "read_page"
        assert args["page_id"] == "concept-mcp__1"
        return _ok({"id": "concept-mcp__1", "title": "MCP", "body": "...", "frontmatter": {}})

    _install_dispatch(app, dispatch)
    out = await get_page(app, "concept-mcp__1")
    assert out["id"] == "concept-mcp__1"


async def test_get_page_rejects_empty_id(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    with pytest.raises(RetrieveError, match="page_id is required"):
        await get_page(app, "")


# ---- traverse ----


async def test_traverse_proxies_smalt(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        assert tool == "traverse"
        assert args["from_id"] == "x"
        assert args["hops"] == 2
        assert args["label"] == "mentions"
        return _ok({"edges": [], "visited_nodes": ["x"]})

    _install_dispatch(app, dispatch)
    out = await traverse(app, "x", hops=2, label="mentions")
    assert out["visited_nodes"] == ["x"]


# ---- gaps ----


async def test_list_gaps_proxies_ebony(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)

    async def dispatch(client: str, tool: str, _args: dict[str, Any]) -> CallResult:
        assert client == "ebony-enriching"
        assert tool == "list_gaps"
        return _ok({"gaps": [{"id": "abc", "query": "anything"}]})

    _install_dispatch(app, dispatch)
    out = await list_gaps(app)
    assert out["gaps"][0]["id"] == "abc"


async def test_report_gap_proxies_ebony_add_gap(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)

    captured: dict[str, Any] = {}

    async def dispatch(client: str, tool: str, args: dict[str, Any]) -> CallResult:
        assert client == "ebony-enriching"
        assert tool == "add_gap"
        captured.update(args)
        return _ok({"gap_id": "deadbeef", "position": 1})

    _install_dispatch(app, dispatch)
    out = await report_gap(app, "unanswered query", why="user-asked", source="cogrind-workshop")
    assert out["gap_id"] == "deadbeef"
    assert captured["query"] == "unanswered query"
    assert captured["why"] == "user-asked"
    assert captured["source"] == "cogrind-workshop"


async def test_report_gap_rejects_empty_query(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    with pytest.raises(RetrieveError, match="query is required"):
        await report_gap(app, "")


# ---- SearchHit / ExpansionEdge sanity ----


def test_search_hit_from_smalt_row_handles_missing_fields() -> None:
    """Defensive parsing — missing/typo'd fields shouldn't crash."""
    h = SearchHit.from_smalt_row({"id": "x"})
    assert h.id == "x"
    assert h.title == ""
    assert h.score == 0.0


def test_expansion_edge_to_dict() -> None:
    e = ExpansionEdge(from_id="a", to_id="b", label="rel")
    d = e.to_dict()
    assert d == {"from_id": "a", "to_id": "b", "label": "rel"}
