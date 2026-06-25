# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.ingest.flint_client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cobalt_grinding.app import App
from cobalt_grinding.config import Config, MCPConfig
from cobalt_grinding.daemon.mcp_clients import CallResult
from cobalt_grinding.ingest.flint_client import (
    FlintUnavailable,
    join_pages,
    pdf_info,
    read_text,
    read_text_url,
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


# ---- read_text ----


async def test_read_text_passes_path_to_flint(tmp_path: Path) -> None:
    """flint expects `{source: {path: ...}}`, not a flat `path` arg."""
    app = _bare_app(tmp_path)
    captured: dict[str, Any] = {}

    async def dispatch(client: str, tool: str, args: dict[str, Any]) -> CallResult:
        assert client == "flint-slating"
        assert tool == "pdf_read_text"
        captured.update(args)
        return _ok({"pages": [{"page": 1, "text": "page-1 body"}], "page_count": 1})

    _install_dispatch(app, dispatch)
    payload = await read_text(app, path="/some/file.pdf")
    assert captured["source"] == {"path": "/some/file.pdf"}
    assert payload["page_count"] == 1
    assert payload["pages"][0]["text"] == "page-1 body"


async def test_read_text_passes_password_when_provided(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    captured: dict[str, Any] = {}

    async def dispatch(_client: str, _tool: str, args: dict[str, Any]) -> CallResult:
        captured.update(args)
        return _ok({"pages": [], "page_count": 0})

    _install_dispatch(app, dispatch)
    await read_text(app, path="/x.pdf", password="secret")
    assert captured["password"] == "secret"


async def test_read_text_raises_when_supervisor_not_started(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    # No `_mcp_clients` patched — the property raises.
    with pytest.raises(FlintUnavailable, match="supervisor not started"):
        await read_text(app, path="/x.pdf")


async def test_read_text_raises_on_dispatch_failure(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    _install_dispatch(app, AsyncMock(return_value=_err("flint-slating not running")))
    with pytest.raises(FlintUnavailable, match="dispatch failed"):
        await read_text(app, path="/x.pdf")


async def test_read_text_raises_on_toolside_error(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    _install_dispatch(
        app,
        AsyncMock(return_value=_toolside_err({"error": "bad_source", "detail": "no such file"})),
    )
    with pytest.raises(FlintUnavailable, match="no such file"):
        await read_text(app, path="/missing.pdf")


async def test_read_text_url_passes_url_source(tmp_path: Path) -> None:
    """`read_text_url` sends `{source: {url: ...}}`, never `path`."""
    app = _bare_app(tmp_path)
    captured: dict[str, Any] = {}

    async def dispatch(_client: str, _tool: str, args: dict[str, Any]) -> CallResult:
        captured.update(args)
        return _ok({"pages": [{"page": 1, "text": "fetched"}], "page_count": 1})

    _install_dispatch(app, dispatch)
    payload = await read_text_url(app, url="https://example.com/paper.pdf")
    assert captured["source"] == {"url": "https://example.com/paper.pdf"}
    assert "path" not in captured["source"]
    assert payload["page_count"] == 1


async def test_pdf_info_passes_path(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    captured: dict[str, Any] = {}

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        assert tool == "pdf_info"
        captured.update(args)
        return _ok({"page_count": 5, "sha256": "deadbeef", "metadata": {"title": "X"}})

    _install_dispatch(app, dispatch)
    info = await pdf_info(app, path="/local.pdf")
    assert captured["source"] == {"path": "/local.pdf"}
    assert info["page_count"] == 5
    assert info["sha256"] == "deadbeef"


async def test_pdf_info_passes_url(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    captured: dict[str, Any] = {}

    async def dispatch(_client: str, _tool: str, args: dict[str, Any]) -> CallResult:
        captured.update(args)
        return _ok({"page_count": 12, "sha256": "abc"})

    _install_dispatch(app, dispatch)
    info = await pdf_info(app, url="https://x.org/p.pdf")
    assert captured["source"] == {"url": "https://x.org/p.pdf"}
    assert info["page_count"] == 12


async def test_pdf_info_requires_exactly_one_of_path_or_url(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    # Neither
    with pytest.raises(FlintUnavailable, match="exactly one"):
        await pdf_info(app)
    # Both
    with pytest.raises(FlintUnavailable, match="exactly one"):
        await pdf_info(app, path="/x.pdf", url="https://x/y.pdf")


async def test_read_text_raises_when_payload_has_inline_error(tmp_path: Path) -> None:
    """flint reports tool errors via `isError=true` but ALSO via an
    `{error, detail}` shape in the payload itself (the dispatch
    function uses `_err()` for both). The helper handles both."""
    app = _bare_app(tmp_path)
    _install_dispatch(
        app,
        AsyncMock(return_value=_ok({"error": "pypdf_failed", "detail": "encrypted PDF"})),
    )
    with pytest.raises(FlintUnavailable, match="encrypted PDF"):
        await read_text(app, path="/x.pdf")


# ---- join_pages ----


def test_join_pages_concatenates_with_page_markers() -> None:
    payload = {
        "pages": [
            {"page": 1, "text": "first body"},
            {"page": 2, "text": "second body"},
        ],
        "page_count": 2,
    }
    joined = join_pages(payload)
    assert "first body" in joined
    assert "second body" in joined
    assert "page 1" in joined
    assert "page 2" in joined


def test_join_pages_skips_empty_pages() -> None:
    """Empty-text pages shouldn't introduce spurious blank sections."""
    payload = {
        "pages": [
            {"page": 1, "text": "real"},
            {"page": 2, "text": ""},
            {"page": 3, "text": "more"},
        ]
    }
    joined = join_pages(payload)
    assert "real" in joined
    assert "more" in joined


def test_join_pages_handles_missing_pages_field() -> None:
    assert join_pages({}) == ""


def test_join_pages_tolerates_non_dict_entries() -> None:
    """Defensive: malformed `pages` entries don't crash the join."""
    payload = {"pages": [None, "bad", {"page": 1, "text": "good"}]}
    joined = join_pages(payload)
    assert "good" in joined
