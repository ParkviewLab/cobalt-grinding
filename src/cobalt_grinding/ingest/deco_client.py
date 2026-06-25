# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Thin async helper for the deco-assaying MCP child.

cobalt-grinding's M3 code handler hands raw source-file content to
deco's `analyze_file` tool, which runs tree-sitter and returns
structured JSON (symbols, imports, parse status). The orchestrator
formats the response into a short Markdown outline that appears below
the LLM-written summary on code section pages.

**Why a separate module from `smalt_client.py`**:
- deco-assaying is a CAPABILITY (parser), not the storage substrate.
  The plan's capability-vs-infrastructure line wants those split.
- deco might be configured with `autostart=false` (the default config
  ships it but operators may disable). All helpers here degrade
  silently when deco is unavailable.

**Languages deco supports** (per the deco-assaying repo's README at
the time of this writing): Python, C, C++. The format_classifier's
language values map cleanly:

  cobalt-grinding lang    →    deco language id
  -----------------------    ---------------------
  python                    python
  c                         c
  cpp                       cpp

For any other language → we don't call deco.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cobalt_grinding.app import App

logger = logging.getLogger(__name__)


DECO_CLIENT_NAME = "deco-assaying"

# Languages we ask deco to parse. Matches `format_classifier.classify`
# language strings; deco's `language` arg accepts the same names.
_DECO_SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"python", "c", "cpp"})


class DecoUnavailable(Exception):
    """Raised when the deco-assaying child can't service a request.

    All call sites in the orchestrator catch this and fall back to
    "no symbol outline" rather than failing the ingest.
    """


def is_supported_language(language: str | None) -> bool:
    """True if `language` is one deco can parse."""
    return language is not None and language in _DECO_SUPPORTED_LANGUAGES


async def analyze_file(
    app: App,
    *,
    content: str,
    filename: str,
    language: str,
    include_chunks: bool = False,
) -> dict[str, Any]:
    """Call `deco.analyze_file` on raw source-file content.

    Returns the decoded JSON payload. Raises `DecoUnavailable` for any
    dispatch / tool-side failure — the orchestrator treats that as
    "no symbol outline available."

    `include_chunks=False` by default: ingest just wants the symbol
    outline + imports, not the AST chunks deco can produce.
    """
    if not is_supported_language(language):
        raise DecoUnavailable(f"language {language!r} not supported by deco")
    try:
        clients = app.mcp_clients
    except RuntimeError as e:
        raise DecoUnavailable(f"MCP supervisor not started: {e}") from e

    arguments: dict[str, Any] = {
        "content": content,
        "filename": filename,
        "language": language,
        "include_chunks": include_chunks,
    }
    result = await clients.call_tool(DECO_CLIENT_NAME, "analyze_file", arguments)
    if not result.ok:
        raise DecoUnavailable(
            f"deco.analyze_file dispatch failed: {result.error_text or '(no error text)'}"
        )
    payload = _decode_content(result.content)
    if not isinstance(payload, dict):
        raise DecoUnavailable(
            f"deco.analyze_file returned non-dict payload: {type(payload).__name__}"
        )
    if result.is_error:
        msg = payload.get("message", payload.get("error", "(no error message)"))
        raise DecoUnavailable(f"deco.analyze_file returned error: {msg}")
    return payload


def render_symbol_outline(deco_payload: dict[str, Any]) -> str:
    """Format deco's `analyze_file` response as a Markdown symbol outline.

    Sections produced (each omitted if the corresponding deco field is
    empty / missing):
      - **Symbols** — bulleted list of `<kind> <name> — line <N>`.
      - **Imports** — bulleted list of imported modules / names.
      - **Parse status** — only shown when not "ok" (so clean parses
        don't clutter the page).

    Returns the empty string if the deco payload has nothing useful —
    the caller can skip emitting a "## Symbols" header in that case.
    """
    lines: list[str] = []

    symbols = deco_payload.get("symbols") or []
    if symbols:
        lines.append("### Symbols")
        lines.append("")
        for sym in symbols:
            if not isinstance(sym, dict):
                continue
            kind = str(sym.get("kind") or "?")
            name = str(sym.get("name") or "?")
            span = sym.get("span") or {}
            line_no = (
                span.get("start_line")
                if isinstance(span, dict)
                else sym.get("line_start") or sym.get("line")
            )
            if line_no is not None:
                lines.append(f"- `{kind}` **{name}** — line {line_no}")
            else:
                lines.append(f"- `{kind}` **{name}**")
        lines.append("")

    imports = deco_payload.get("imports") or []
    if imports:
        lines.append("### Imports")
        lines.append("")
        for imp in imports:
            if isinstance(imp, dict):
                target = imp.get("module") or imp.get("name") or imp.get("path") or "?"
                lines.append(f"- `{target}`")
            elif isinstance(imp, str):
                lines.append(f"- `{imp}`")
        lines.append("")

    parse_status = deco_payload.get("parse_status")
    if isinstance(parse_status, dict):
        status = parse_status.get("status") or parse_status.get("state")
        if status and str(status).lower() not in ("ok", "success"):
            lines.append("### Parse status")
            lines.append("")
            lines.append(f"- status: `{status}`")
            reason = parse_status.get("reason") or parse_status.get("message")
            if reason:
                lines.append(f"- reason: {reason}")
            lines.append("")
    elif isinstance(parse_status, str) and parse_status.lower() not in ("ok", "success", ""):
        lines.append("### Parse status")
        lines.append("")
        lines.append(f"- `{parse_status}`")
        lines.append("")

    return "\n".join(lines).rstrip()


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
