# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Anthropic LLM provider for the host's tool-use loop.

Wraps the official `anthropic` SDK so the loop has a single async
`complete(...)` to call. The provider returns the SDK's `Message`
object directly — the loop iterates `.content` blocks (text, tool_use)
and reads `.stop_reason` to decide whether to dispatch tools or
finish the turn.

Future providers (OpenAI, OpenRouter, ...) will need a Protocol seam
here. For M2.5 we ship Anthropic only and keep the call shape narrow;
the seam can be added when a second provider lands without breaking
existing callers — the loop reads only `.content` and `.stop_reason`.

`tool_descriptors_to_anthropic` is the small adapter that turns the
supervisor's `ToolDescriptor` list into the dict shape Anthropic's
`tools=` parameter expects. The host's loop calls it once per LLM
round-trip after `tools_index` retrieval picks the top-K candidates.
"""

from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import Message

from cobalt_grinding.daemon.mcp_clients import ToolDescriptor

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4096


class AnthropicProvider:
    """Async wrapper around `client.messages.create(...)`.

    Holds the API client + a default model so callers don't have to pass
    them on every call. Per-call overrides for model / max_tokens are
    accepted — useful for cheap-and-fast turns vs. heavier reasoning.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> Message:
        """Round-trip the LLM.

        `messages` and `tools` are passed through to the SDK in
        Anthropic's native dict shape — the loop is responsible for
        building them. Returns the raw `Message` so the loop can read
        `.content` and `.stop_reason` directly.
        """
        kwargs: dict[str, Any] = {
            "model": model or self._model,
            "max_tokens": max_tokens or self._max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        return await self._client.messages.create(**kwargs)


def tool_descriptors_to_anthropic(
    descriptors: list[ToolDescriptor],
) -> list[dict[str, Any]]:
    """Render `ToolDescriptor` list into Anthropic's `tools=` shape.

    The prefixed name is what the LLM sees and emits; dispatch strips
    the prefix back off before routing to the owning child.
    """
    return [
        {
            "name": d.prefixed_name,
            "description": d.description,
            "input_schema": d.input_schema,
        }
        for d in descriptors
    ]
