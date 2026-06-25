# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.ingest.handlers — file readers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cobalt_grinding.app import App
from cobalt_grinding.config import Config, MCPConfig
from cobalt_grinding.daemon.mcp_clients import CallResult
from cobalt_grinding.ingest.format_classifier import (
    FileClassification,
    FileKind,
    classify,
)
from cobalt_grinding.ingest.handlers import handle, handle_async


def test_handles_text(tmp_path: Path) -> None:
    f = tmp_path / "note.md"
    f.write_text("# title\n\nbody text\n", encoding="utf-8")
    section = handle(f, classify(f))
    assert section.classification.kind is FileKind.TEXT
    assert "title" in section.body
    assert "body text" in section.body
    assert section.truncated is False
    assert len(section.content_hash) == 64  # sha256 hex


def test_handles_code_carries_language(tmp_path: Path) -> None:
    f = tmp_path / "script.py"
    f.write_text("def foo():\n    return 42\n", encoding="utf-8")
    section = handle(f, classify(f))
    assert section.classification.kind is FileKind.CODE
    assert section.metadata.get("language") == "python"


def test_handles_config(tmp_path: Path) -> None:
    f = tmp_path / "settings.toml"
    f.write_text('[section]\nkey = "value"\n', encoding="utf-8")
    section = handle(f, classify(f))
    assert section.classification.kind is FileKind.CONFIG
    assert "[section]" in section.body


def test_content_hash_is_stable_across_reads(tmp_path: Path) -> None:
    f = tmp_path / "stable.md"
    f.write_text("identical content", encoding="utf-8")
    first = handle(f, classify(f))
    second = handle(f, classify(f))
    assert first.content_hash == second.content_hash


def test_content_hash_changes_when_content_changes(tmp_path: Path) -> None:
    f = tmp_path / "changing.md"
    f.write_text("v1", encoding="utf-8")
    first_hash = handle(f, classify(f)).content_hash
    f.write_text("v2", encoding="utf-8")
    second_hash = handle(f, classify(f)).content_hash
    assert first_hash != second_hash


def test_handle_rejects_unsupported_kind(tmp_path: Path) -> None:
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    bad_cls = FileClassification(kind=FileKind.UNSUPPORTED)
    with pytest.raises(ValueError, match="unsupported file kind"):
        handle(f, bad_cls)


def test_handles_non_utf8_bytes_via_replacement(tmp_path: Path) -> None:
    """Real-world files sometimes have invalid UTF-8. Better to index
    them with replacement chars than fail the whole source."""
    f = tmp_path / "weird.txt"
    f.write_bytes(b"hello \xff\xfe world")
    section = handle(f, classify(f))
    # Body decodes without raising; replacement chars in place of the
    # invalid bytes are fine.
    assert "hello" in section.body
    assert "world" in section.body


def test_sync_handle_rejects_pdf(tmp_path: Path) -> None:
    """PDFs must go through `handle_async`, not the sync entry point."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(ValueError, match="requires async handler"):
        handle(f, classify(f))


# ---- handle_async (PDF path; sync kinds fall through to handle()) ----


def _bare_app(tmp_path: Path) -> App:
    return App(
        Config(
            smalt_dir=tmp_path / "wiki",
            cobalt_grinding_dir=tmp_path / "cobalt_grinding",
            mcp=MCPConfig(clients={}),
        )
    )


def _ok_call_result(payload: dict[str, Any]) -> CallResult:
    block = MagicMock()
    block.text = json.dumps(payload)
    return CallResult(ok=True, is_error=False, content=[block], error_text=None)


def _install_flint_mock(app: App, payload: dict[str, Any]) -> AsyncMock:
    async def dispatch(client: str, tool: str, _args: dict[str, Any]) -> CallResult:
        assert client == "flint-slating"
        assert tool == "pdf_read_text"
        return _ok_call_result(payload)

    fake = MagicMock()
    fake.call_tool = AsyncMock(side_effect=dispatch)
    app._mcp_clients = fake  # type: ignore[assignment]
    return fake.call_tool


async def test_handle_async_pdf_calls_flint_and_returns_text(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake bytes")
    app = _bare_app(tmp_path)
    _install_flint_mock(
        app,
        {
            "pages": [
                {"page": 1, "text": "first page body"},
                {"page": 2, "text": "second page body"},
            ],
            "page_count": 2,
        },
    )

    section = await handle_async(app, f, classify(f))

    assert section.classification.kind is FileKind.PDF
    assert "first page body" in section.body
    assert "second page body" in section.body
    assert section.metadata.get("page_count") == 2
    # Hash is computed locally from the raw bytes, not from flint.
    assert len(section.content_hash) == 64


async def test_handle_async_pdf_gracefully_handles_flint_unavailable(
    tmp_path: Path,
) -> None:
    """If flint is down, we still return a RawSection with the hash
    + empty body so the page can be written with a 'no content
    available' marker downstream."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")
    app = _bare_app(tmp_path)
    # Set up an mcp_clients that errors on every call (simulates flint
    # not running).

    async def dispatch(*_a: Any, **_kw: Any) -> CallResult:
        return CallResult(ok=False, is_error=True, content=[], error_text="flint not running")

    fake = MagicMock()
    fake.call_tool = AsyncMock(side_effect=dispatch)
    app._mcp_clients = fake  # type: ignore[assignment]

    section = await handle_async(app, f, classify(f))
    assert section.body == ""
    assert section.metadata.get("flint_unavailable") is True
    assert len(section.content_hash) == 64


async def test_handle_async_falls_through_to_sync_for_text(tmp_path: Path) -> None:
    """Non-PDF kinds bypass the MCP child path entirely."""
    f = tmp_path / "readme.md"
    f.write_text("hi", encoding="utf-8")
    app = _bare_app(tmp_path)
    # No mcp_clients installed — fine, since the sync path doesn't use it.
    section = await handle_async(app, f, classify(f))
    assert section.classification.kind is FileKind.TEXT
    assert "hi" in section.body
