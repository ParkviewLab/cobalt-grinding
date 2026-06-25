# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The LLM tool-use loop.

Drives one agent invocation: send (system, messages, tools) to the
LLM; if it asks to use tools, dispatch them, append the results, ask
again; keep going until `end_turn` (or any other terminal stop_reason)
or until the iteration cap fires.

The loop is host-internal — agents call `app.host.run_agent(...)` which
wires everything together. The loop only knows about an `LLMProvider`-
shaped object (currently `AnthropicProvider`) and an `McpClientManager`
for dispatch.

Progress events (start, tool_use_started, tool_result_received,
end_turn) flow through `on_event` for the scheduler to pump into
`wiki.task_status`. Same channel cobalt_grinding already uses for indexer
progress.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from anthropic.types import Message

from cobalt_grinding.daemon.mcp_clients import McpClientManager
from cobalt_grinding.host.dispatch import DispatchResult, dispatch

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERS = 20
TERMINAL_STOP_REASONS = frozenset({"end_turn", "max_tokens", "stop_sequence", "refusal"})


class LLMProvider(Protocol):
    """Minimal duck-typed provider seam.

    The loop only needs `complete(system=..., messages=..., tools=...)`
    returning something with `.content` and `.stop_reason`. Today the
    only impl is `AnthropicProvider`; future providers (OpenAI, etc.)
    will satisfy this Protocol with their own translation layers.
    """

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> Message: ...


class IterCapExceeded(RuntimeError):
    """Raised when the loop hits `max_iters` without a terminal stop_reason.

    Surfaced to the agent as a clean error rather than swallowed —
    runaway tool loops are a real failure mode worth flagging early.
    """


EventFn = Callable[[dict[str, Any]], None]


async def run_tool_use_loop(
    *,
    provider: LLMProvider,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    mcp_clients: McpClientManager,
    max_iters: int = DEFAULT_MAX_ITERS,
    on_event: EventFn | None = None,
) -> Message:
    """Run the loop. Mutates `messages` in place, appending each turn.

    Returns the final assistant Message — the one whose stop_reason is
    terminal. Raises IterCapExceeded if the loop runs out of iterations
    without ever reaching one.

    `tools` is the LLM-facing list (Anthropic format). The host's caller
    has already done tools_index retrieval and conversion via
    `tool_descriptors_to_anthropic`.
    """
    _emit(on_event, {"event": "loop_start", "max_iters": max_iters})

    for iteration in range(1, max_iters + 1):
        response = await provider.complete(
            system=system,
            messages=messages,
            tools=tools or None,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason in TERMINAL_STOP_REASONS:
            _emit(
                on_event,
                {"event": "end_turn", "stop_reason": response.stop_reason, "iteration": iteration},
            )
            return response

        if response.stop_reason == "tool_use":
            tool_result_blocks = await _dispatch_all_tool_uses(
                response.content,
                mcp_clients=mcp_clients,
                on_event=on_event,
            )
            messages.append({"role": "user", "content": tool_result_blocks})
            continue

        # Unknown stop_reason — defensive return rather than infinite loop.
        # The Anthropic API may add new terminal reasons; treating unknown
        # as terminal is safer than treating it as "keep going."
        logger.warning("loop: unknown stop_reason %r — treating as terminal", response.stop_reason)
        _emit(
            on_event,
            {"event": "end_turn", "stop_reason": response.stop_reason, "iteration": iteration},
        )
        return response

    raise IterCapExceeded(f"tool-use loop exceeded {max_iters} iterations without end_turn")


async def _dispatch_all_tool_uses(
    content_blocks: list[Any],
    *,
    mcp_clients: McpClientManager,
    on_event: EventFn | None,
) -> list[dict[str, Any]]:
    """Walk the assistant's content; dispatch each tool_use block; build
    the tool_result blocks for the next user-turn message.
    """
    tool_results: list[dict[str, Any]] = []
    for block in content_blocks:
        if getattr(block, "type", None) != "tool_use":
            continue
        tool_use_id = getattr(block, "id", "")
        name = getattr(block, "name", "")
        # `block.input` is a dict-ish; some SDK versions expose it as a
        # pydantic-typed field, others as a plain dict. dict() copes with
        # both.
        arguments = dict(getattr(block, "input", {}) or {})

        _emit(on_event, {"event": "tool_use_started", "id": tool_use_id, "name": name})
        result = await dispatch(name, arguments, mcp_clients=mcp_clients)
        _emit(
            on_event,
            {
                "event": "tool_result_received",
                "id": tool_use_id,
                "name": name,
                "ok": result.ok,
                "is_error": result.is_error,
            },
        )

        tool_results.append(_to_tool_result_block(tool_use_id, result))
    return tool_results


def _to_tool_result_block(tool_use_id: str, result: DispatchResult) -> dict[str, Any]:
    """Render a DispatchResult as an Anthropic `tool_result` content block.

    Success path: pass the MCP content blocks through (as Anthropic-shaped
    text dicts). Failure path: feed `error_text` back as a single text
    block with `is_error=True` so the LLM can read what went wrong and
    decide whether to retry, switch tools, or give up.
    """
    if result.ok and not result.is_error:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": _mcp_content_to_anthropic(result.content),
        }
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": result.error_text or "tool call failed",
        "is_error": True,
    }


def _mcp_content_to_anthropic(mcp_content: list[Any]) -> list[dict[str, Any]]:
    """Translate MCP CallToolResult content blocks to Anthropic content.

    M2.5 only handles text content; non-text blocks (Image, Audio,
    EmbeddedResource) are stringified as text — good enough for codeparse
    and similar text-returning tools. Richer block types can be added as
    M3+ tools start using them.
    """
    blocks: list[dict[str, Any]] = []
    for c in mcp_content:
        text = getattr(c, "text", None)
        if isinstance(text, str):
            blocks.append({"type": "text", "text": text})
        else:
            blocks.append({"type": "text", "text": str(c)})
    return blocks


def _emit(on_event: EventFn | None, event: dict[str, Any]) -> None:
    """Best-effort progress event emit. Never lets a buggy callback
    interrupt the loop.
    """
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:  # pragma: no cover — defensive
        logger.exception("loop on_event callback raised; continuing")
