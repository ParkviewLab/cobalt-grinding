# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Host-internal tool dispatch.

The LLM emits `tool_use` blocks with prefixed tool names like
`"codeparse.parse_file"`. The host needs to:

  1. Split the prefix off so we know which MCP child owns the tool.
  2. Call the underlying tool via `McpClientManager`.
  3. Turn the outcome into a single `DispatchResult` shape that
     `loop.py` can lift into an Anthropic `tool_result` block.

Every failure path (malformed name, unknown client, dispatch error,
tool error) becomes a `DispatchResult` — never an exception. The
caller's loop is on the hot path; wrapping each call in try/except
would be both verbose and miss the structured-error point.

This module is host-internal. Agents never import it; they call
`app.host.run_agent(...)` and the host handles dispatch on their behalf.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cobalt_grinding.daemon.mcp_clients import McpClientManager


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of one tool dispatch.

    `ok` distinguishes dispatch-layer failures (timeout, child gone,
    unknown client) from successful round-trips; `is_error` reflects
    the MCP `isError` flag for tools that ran but reported failure.
    Both shapes get rendered to the LLM as `tool_result(is_error=True)`
    blocks — the LLM doesn't care which layer fell over, only that
    the call didn't succeed.
    """

    ok: bool
    is_error: bool
    content: list[Any]
    error_text: str | None


async def dispatch(
    prefixed_name: str,
    arguments: dict[str, Any] | None,
    *,
    mcp_clients: McpClientManager,
    timeout: float | None = None,
) -> DispatchResult:
    """Route a prefixed tool name to its owning MCP child.

    `prefixed_name` looks like `"codeparse.parse_file"`. The first dot
    splits client name from raw tool name. We then ask the supervisor
    to call the tool, and propagate the result.

    `timeout` overrides the per-client `call_timeout` if provided.
    """
    client_name, _, tool_name = prefixed_name.partition(".")
    if not client_name or not tool_name:
        return DispatchResult(
            ok=False,
            is_error=True,
            content=[],
            error_text=f"malformed tool name (expected 'client.tool'): {prefixed_name!r}",
        )

    result = await mcp_clients.call_tool(client_name, tool_name, arguments, timeout=timeout)
    return DispatchResult(
        ok=result.ok,
        is_error=result.is_error,
        content=result.content,
        error_text=result.error_text,
    )
