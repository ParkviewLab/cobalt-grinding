# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Public host surface: `await app.host.run_agent(...)`.

Wires together the four host pieces — `AnthropicProvider`, the tools
index, the supervisor, and the LLM tool-use loop — so cobalt_grinding's
internal agents only see one coroutine.

```python
response = await app.host.run_agent(
    system=PROMPT,
    messages=[{"role": "user", "content": "..."}],
)
```

Tool selection is the host's job: the latest user message becomes the
retrieval query, `ToolsIndex.search` picks top-K candidates via hybrid
BM25 + vector, and the loop hands them to the LLM. Agents pass no
tool list themselves.

This is the only module agents import. All the supervisor / dispatch /
loop / provider plumbing stays internal to `cobalt_grinding.host.*`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from anthropic.types import Message

from cobalt_grinding.daemon.mcp_clients import McpClientManager, ToolDescriptor
from cobalt_grinding.host.loop import (
    DEFAULT_MAX_ITERS,
    EventFn,
    LLMProvider,
    run_tool_use_loop,
)
from cobalt_grinding.host.provider import tool_descriptors_to_anthropic

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 10


class _ToolsRetriever(Protocol):
    """Duck-typed `ToolsIndex.search` for testability."""

    def search(self, query: str, *, top_k: int = ...) -> list[ToolDescriptor]: ...


@dataclass
class Host:
    """Agent runtime. Constructed once at daemon startup and held on `App`.

    `run_agent` is the public surface. Everything else is plumbing the
    daemon wires once at startup and the agent code never touches.
    """

    provider: LLMProvider
    mcp_clients: McpClientManager
    tools_index: _ToolsRetriever
    # Optional pre-search hook the App uses to refresh the tools index
    # against current supervisor state (handles child restarts without a
    # daemon restart). Default None = caller does its own refresh, or
    # accepts a stale index. Tests pass None.
    refresh_tools: Callable[[], None] | None = None
    default_top_k: int = DEFAULT_TOP_K
    default_max_iters: int = DEFAULT_MAX_ITERS

    async def run_agent(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        max_iters: int | None = None,
        top_k: int | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        on_event: EventFn | None = None,
    ) -> Message:
        """Run one agent invocation end-to-end.

        - Retrieve top-K tools relevant to the latest user turn.
        - Hand them to the loop (which dispatches and re-asks until
          `end_turn`).
        - Return the final assistant `Message`.

        `messages` is mutated in place by the loop — caller sees the
        full conversation after this returns. Pass a copy if you need
        to preserve the input shape.
        """
        if self.refresh_tools is not None:
            self.refresh_tools()
        query = _retrieval_query(messages)
        descriptors = self.tools_index.search(query, top_k=top_k or self.default_top_k)
        tools = tool_descriptors_to_anthropic(descriptors)
        logger.debug(
            "host.run_agent: query=%r descriptors=%d max_iters=%d",
            query,
            len(descriptors),
            max_iters or self.default_max_iters,
        )
        return await run_tool_use_loop(
            provider=self.provider,
            system=system,
            messages=messages,
            tools=tools,
            mcp_clients=self.mcp_clients,
            max_iters=max_iters or self.default_max_iters,
            on_event=on_event,
        )


def _retrieval_query(messages: list[dict[str, Any]]) -> str:
    """Extract the retrieval query from the conversation history.

    Walks back to the most recent user message and returns its text.
    User messages can be either a plain string or a content-block list;
    we extract every text block and join them. If nothing usable is
    present (only tool_result blocks, e.g.), return empty string — the
    tools index treats that as "match anything" via vector search alone.
    """
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
            if text_parts:
                return " ".join(text_parts)
    return ""
