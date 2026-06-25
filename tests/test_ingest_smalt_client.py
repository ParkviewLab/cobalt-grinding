# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.ingest.smalt_client — async helpers over the
smalt-mcp MCP child."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cobalt_grinding.app import App
from cobalt_grinding.config import Config, MCPConfig
from cobalt_grinding.daemon.mcp_clients import CallResult
from cobalt_grinding.ingest.smalt_client import (
    SmaltClientError,
    add_outgoing_links,
    find_source_by_location_uri,
    write_entity_page,
    write_glossary_page,
    write_section_page,
    write_source_page,
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


# ---- find_source_by_location_uri ----


async def test_find_source_returns_none_when_no_matches(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)

    async def dispatch(_client: str, tool: str, _args: dict[str, Any]) -> CallResult:
        if tool == "find_by_alias":
            return _ok({"matches": [], "count": 0})
        raise AssertionError(f"unexpected tool: {tool}")

    _install_dispatch(app, dispatch)
    out = await find_source_by_location_uri(app, location_uri="file:/nope")
    assert out is None


async def test_find_source_returns_existing_with_hash(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        if tool == "find_by_alias":
            assert args["alias"] == "file:/path"
            return _ok(
                {
                    "matches": [{"id": "file-foo__abc", "title": "foo.md", "type": "source"}],
                    "count": 1,
                }
            )
        if tool == "read_page":
            assert args["page_id"] == "file-foo__abc"
            return _ok({"frontmatter": {"source_content_hash": "deadbeef"}})
        raise AssertionError(f"unexpected: {tool}")

    _install_dispatch(app, dispatch)
    out = await find_source_by_location_uri(app, location_uri="file:/path")
    assert out is not None
    assert out.canonical_id == "file-foo__abc"
    assert out.source_content_hash == "deadbeef"


async def test_find_source_swallows_smalt_errors(tmp_path: Path) -> None:
    """If smalt is unreachable during the lookup, treat it as no-existing —
    the ingest pipeline will then create a fresh page."""
    app = _bare_app(tmp_path)

    async def dispatch(_client: str, _tool: str, _args: dict[str, Any]) -> CallResult:
        return _err("smalt down")

    _install_dispatch(app, dispatch)
    out = await find_source_by_location_uri(app, location_uri="file:/whatever")
    assert out is None


# ---- write_source_page ----


async def test_write_source_page_passes_through_response(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        assert tool == "write_page"
        assert args["mode"] == "create"
        assert args["frontmatter"]["id"] == "file-x"
        return _ok({"id": "file-x__xyz", "path": "pages/sources/file-x__xyz.md"})

    _install_dispatch(app, dispatch)
    out = await write_source_page(app, frontmatter={"id": "file-x", "type": "source"}, body="body")
    assert out["id"] == "file-x__xyz"


async def test_write_source_page_raises_on_dispatch_failure(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    _install_dispatch(app, AsyncMock(return_value=_err("client smalt-mcp not running")))
    with pytest.raises(SmaltClientError, match=r"smalt\.write_page dispatch failed"):
        await write_source_page(app, frontmatter={"id": "x", "type": "source"}, body="")


async def test_write_source_page_raises_on_toolside_error(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    _install_dispatch(
        app,
        AsyncMock(return_value=_toolside_err({"error": "validation_error", "message": "bad fm"})),
    )
    with pytest.raises(SmaltClientError, match="bad fm"):
        await write_source_page(app, frontmatter={"id": "x", "type": "source"}, body="")


# ---- write_entity_page ----


async def test_write_entity_page_builds_correct_frontmatter(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    captured: dict[str, Any] = {}

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        assert tool == "write_page"
        captured.update(args)
        return _ok({"id": "org-anthropic__abc", "path": "pages/entities/org-anthropic__abc.md"})

    _install_dispatch(app, dispatch)
    out = await write_entity_page(
        app,
        name="Anthropic",
        aliases=["AI Co"],
        kind="org",
        mentioned_in_source_id="file-readme__src",
        snippet="founded by Anthropic in 2021",
    )
    assert out["id"] == "org-anthropic__abc"
    fm = captured["frontmatter"]
    assert fm["type"] == "entity"
    assert fm["entity_kind"] == "org"
    assert fm["title"] == "Anthropic"
    assert "Anthropic" in fm["aliases"]
    assert "AI Co" in fm["aliases"]
    # The back-link to the source page is set up at write time.
    assert any(
        link.get("target") == "file-readme__src" and link.get("label") == "mentioned_in"
        for link in fm["links_out"]
    )


# ---- write_section_page ----


async def test_write_section_page_uses_section_id_pattern(tmp_path: Path) -> None:
    """Section pages have an id with `::` (smalt's upsert path) and a
    `parent_source` pointing to the parent source-index page."""
    app = _bare_app(tmp_path)
    captured: dict[str, Any] = {}

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        assert tool == "write_page"
        captured.update(args)
        return _ok({"id": args["frontmatter"]["id"], "path": "pages/sources/x.md"})

    _install_dispatch(app, dispatch)
    out = await write_section_page(
        app,
        section_id="dir-foo__abc::src/bar.py",
        parent_source_id="dir-foo__abc",
        title="src/bar.py",
        body="content",
        aliases=["file:/tmp/foo/src/bar.py"],
        location_uri="file:/tmp/foo/src/bar.py",
        source_content_hash="abc123",
    )
    fm = captured["frontmatter"]
    assert "::" in fm["id"]
    assert fm["parent_source"] == "dir-foo__abc"
    assert fm["title"] == "src/bar.py"
    assert fm["location_uri"] == "file:/tmp/foo/src/bar.py"
    assert fm["source_content_hash"] == "abc123"
    assert out["id"] == "dir-foo__abc::src/bar.py"


# ---- write_glossary_page ----


async def test_write_glossary_page_creates_concept_with_glossary_flag(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    captured: dict[str, Any] = {}

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        assert tool == "write_page"
        captured.update(args)
        return _ok({"id": "glossary-mcp__xyz", "path": "pages/concepts/glossary-mcp__xyz.md"})

    _install_dispatch(app, dispatch)
    out = await write_glossary_page(
        app,
        term="MCP",
        definition="Model Context Protocol — tool-use protocol",
        mentioned_in_source_id="file-readme__src",
        snippet="The MCP server exposes...",
    )
    assert out["id"] == "glossary-mcp__xyz"
    fm = captured["frontmatter"]
    assert fm["type"] == "concept"
    assert fm["glossary"] is True
    assert fm["title"] == "MCP"
    assert any(ev.get("source_id") == "file-readme__src" for ev in fm.get("evidence", []))


# ---- add_outgoing_links ----


async def test_add_outgoing_links_passes_through_batch(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    captured: dict[str, Any] = {}

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        assert tool == "add_links"
        captured.update(args)
        return _ok({"page_id": args["page_id"], "added": len(args["links"]), "results": []})

    _install_dispatch(app, dispatch)
    await add_outgoing_links(
        app,
        page_id="file-readme__src",
        targets=[
            {"target": "org-anthropic__a"},
            {"target": "glossary-mcp__b", "label": "defines"},
        ],
    )
    assert captured["page_id"] == "file-readme__src"
    assert len(captured["links"]) == 2


async def test_add_outgoing_links_is_noop_with_empty_targets(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    # No dispatch installed — the helper must not call_tool.
    fake = MagicMock()
    fake.call_tool = AsyncMock()
    app._mcp_clients = fake  # type: ignore[assignment]
    out = await add_outgoing_links(app, page_id="file-x", targets=[])
    assert out["added"] == 0
    fake.call_tool.assert_not_awaited()
