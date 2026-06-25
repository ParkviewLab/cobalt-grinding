# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Thin async helper for the flint-slating MCP child.

flint-slating is the PDF-reading capability (pypdf + Docling under the
hood). cobalt-grinding's M3 PDF handler hands a filesystem path to
flint's `pdf_read_text` tool, gets back per-page text, and feeds the
joined text into the ingest pipeline as if it were a regular text
file.

**Why a separate module from `smalt_client.py` / `deco_client.py`**:
- flint is a CAPABILITY (PDF parser), not the storage substrate. Per
  the plan's capability-vs-infrastructure split, capabilities each
  live in their own client helper.
- flint might be configured with `autostart=false` (operators may
  disable). All helpers here degrade silently when flint is
  unavailable.

**First-cut design**: we use `pdf_read_text` (fast, pypdf-only, always
sync). Trade-off: plain text only — loses headings, tables, and
multi-column reading order that `pdf_read_markdown` would preserve.
Markdown via Docling is a follow-up enhancement once the async-job
flow is wired (Docling runs sync for ≤20-page PDFs and async for
larger ones; the async path needs `get_job_status`/`get_job_result`
polling).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cobalt_grinding.app import App

logger = logging.getLogger(__name__)


FLINT_CLIENT_NAME = "flint-slating"


class FlintUnavailable(Exception):
    """Raised when the flint-slating child can't service a request.

    All call sites in the ingest pipeline catch this and surface a
    structured error or fall back gracefully rather than failing the
    whole ingest.
    """


async def read_text(
    app: App,
    *,
    path: str,
    password: str | None = None,
) -> dict[str, Any]:
    """Call `flint.pdf_read_text` on a local PDF path. See
    `read_text_url` for the URL variant."""
    return await _call(app, "pdf_read_text", source={"path": path}, password=password)


async def read_text_url(
    app: App,
    *,
    url: str,
    password: str | None = None,
) -> dict[str, Any]:
    """Call `flint.pdf_read_text` on a remote PDF URL. flint streams
    the PDF to a content-addressed cache, so subsequent flint calls
    against the same URL re-use the cached bytes."""
    return await _call(app, "pdf_read_text", source={"url": url}, password=password)


async def pdf_info(
    app: App,
    *,
    path: str | None = None,
    url: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Call `flint.pdf_info` for quick structural facts: `page_count`,
    PDF metadata (title/author/subject/dates), `is_encrypted`,
    `sha256` (of the PDF bytes), `size`. Exactly one of `path` or
    `url` must be set."""
    if (path is None) == (url is None):
        raise FlintUnavailable("pdf_info requires exactly one of `path` or `url`")
    source: dict[str, Any] = {"path": path} if path is not None else {"url": url}
    return await _call(app, "pdf_info", source=source, password=password)


async def _call(
    app: App,
    tool_name: str,
    *,
    source: dict[str, Any],
    password: str | None,
) -> dict[str, Any]:
    """Shared dispatch for any flint tool that takes a `{source, password}`
    argument shape. Decodes the response, normalizes errors into
    `FlintUnavailable`."""
    try:
        clients = app.mcp_clients
    except RuntimeError as e:
        raise FlintUnavailable(f"MCP supervisor not started: {e}") from e

    arguments: dict[str, Any] = {"source": source}
    if password is not None:
        arguments["password"] = password

    result = await clients.call_tool(FLINT_CLIENT_NAME, tool_name, arguments)
    if not result.ok:
        raise FlintUnavailable(
            f"flint.{tool_name} dispatch failed: {result.error_text or '(no error text)'}"
        )
    payload = _decode_content(result.content)
    if not isinstance(payload, dict):
        raise FlintUnavailable(
            f"flint.{tool_name} returned non-dict payload: {type(payload).__name__}"
        )
    if result.is_error or payload.get("error"):
        msg = payload.get("detail") or payload.get("error", "(no error message)")
        raise FlintUnavailable(f"flint.{tool_name} returned error: {msg}")
    return payload


def join_pages(payload: dict[str, Any]) -> str:
    """Concatenate per-page text into a single body string.

    The shape from `pdf_read_text` is `{pages: [{page, text}], ...}`.
    We separate pages with a thin form-feed marker so the LLM (and
    later debugging) can see where pages split if needed.
    """
    pages = payload.get("pages") or []
    parts: list[str] = []
    for entry in pages:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text") or ""
        page_no = entry.get("page")
        if page_no is not None:
            parts.append(f"--- page {page_no} ---")
        parts.append(text)
    return "\n\n".join(p for p in parts if p)


def _decode_content(content: list[Any]) -> Any:
    """Extract + JSON-decode the first TextContent block of a CallResult."""
    if not content:
        return None
    first = content[0]
    text = getattr(first, "text", None)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
