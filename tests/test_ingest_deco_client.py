# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.ingest.deco_client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cobalt_grinding.app import App
from cobalt_grinding.config import Config, MCPConfig
from cobalt_grinding.daemon.mcp_clients import CallResult
from cobalt_grinding.ingest.deco_client import (
    DecoUnavailable,
    analyze_file,
    is_supported_language,
    render_symbol_outline,
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


def _ok(payload: dict[str, Any]) -> CallResult:
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


# ---- is_supported_language ----


@pytest.mark.parametrize(
    "lang,expected",
    [
        ("python", True),
        ("c", True),
        ("cpp", True),
        ("rust", False),
        ("go", False),
        ("", False),
        (None, False),
    ],
)
def test_is_supported_language(lang: str | None, expected: bool) -> None:
    assert is_supported_language(lang) is expected


# ---- analyze_file ----


async def test_analyze_file_passes_args_to_deco(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    captured: dict[str, Any] = {}

    async def dispatch(client: str, tool: str, args: dict[str, Any]) -> CallResult:
        assert client == "deco-assaying"
        assert tool == "analyze_file"
        captured.update(args)
        return _ok({"symbols": [{"name": "f", "kind": "function"}], "imports": []})

    _install_dispatch(app, dispatch)
    payload = await analyze_file(app, content="def f(): pass", filename="foo.py", language="python")
    assert captured["content"] == "def f(): pass"
    assert captured["filename"] == "foo.py"
    assert captured["language"] == "python"
    assert captured["include_chunks"] is False  # default
    assert payload["symbols"][0]["name"] == "f"


async def test_analyze_file_rejects_unsupported_language(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    with pytest.raises(DecoUnavailable, match="not supported by deco"):
        await analyze_file(app, content="", filename="x.rs", language="rust")


async def test_analyze_file_raises_when_supervisor_not_started(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    # No _mcp_clients patch — property raises.
    with pytest.raises(DecoUnavailable, match="supervisor not started"):
        await analyze_file(app, content="def f(): pass", filename="x.py", language="python")


async def test_analyze_file_raises_on_dispatch_failure(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    _install_dispatch(app, AsyncMock(return_value=_err("deco-assaying not running")))
    with pytest.raises(DecoUnavailable, match="dispatch failed"):
        await analyze_file(app, content="x", filename="x.py", language="python")


async def test_analyze_file_raises_on_toolside_error(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    _install_dispatch(
        app,
        AsyncMock(return_value=_toolside_err({"error": "parse_failed", "message": "bad input"})),
    )
    with pytest.raises(DecoUnavailable, match="bad input"):
        await analyze_file(app, content="x", filename="x.py", language="python")


# ---- render_symbol_outline ----


def test_render_symbol_outline_formats_symbols_and_imports() -> None:
    payload = {
        "symbols": [
            {"name": "foo", "kind": "function", "span": {"start_line": 5}},
            {"name": "Bar", "kind": "class", "span": {"start_line": 12}},
        ],
        "imports": [
            {"module": "os"},
            {"module": "pathlib.Path"},
        ],
    }
    rendered = render_symbol_outline(payload)
    assert "### Symbols" in rendered
    assert "function" in rendered and "foo" in rendered
    assert "line 5" in rendered
    assert "class" in rendered and "Bar" in rendered
    assert "### Imports" in rendered
    assert "os" in rendered


def test_render_symbol_outline_handles_string_imports() -> None:
    """Some deco languages report imports as raw strings, not dicts."""
    payload = {"symbols": [], "imports": ["stdio.h", "string.h"]}
    rendered = render_symbol_outline(payload)
    assert "stdio.h" in rendered
    assert "string.h" in rendered


def test_render_symbol_outline_empty_returns_empty_string() -> None:
    rendered = render_symbol_outline({"symbols": [], "imports": []})
    assert rendered == ""


def test_render_symbol_outline_includes_parse_status_when_failed() -> None:
    payload = {
        "symbols": [],
        "imports": [],
        "parse_status": {"status": "failed", "reason": "syntax error at line 12"},
    }
    rendered = render_symbol_outline(payload)
    assert "Parse status" in rendered
    assert "failed" in rendered
    assert "syntax error" in rendered


def test_render_symbol_outline_hides_parse_status_when_ok() -> None:
    """Clean parses shouldn't clutter the page with a 'Parse status: ok'."""
    payload = {
        "symbols": [{"name": "x", "kind": "function", "span": {"start_line": 1}}],
        "imports": [],
        "parse_status": {"status": "ok"},
    }
    rendered = render_symbol_outline(payload)
    assert "Parse status" not in rendered


def test_render_symbol_outline_tolerates_missing_span() -> None:
    """Some symbols may have no line-number info; render the name only."""
    payload = {"symbols": [{"name": "foo", "kind": "function"}], "imports": []}
    rendered = render_symbol_outline(payload)
    assert "foo" in rendered
    # No `line N` for that symbol.
    assert "line" not in rendered.split("foo")[1].split("\n", 1)[0]
