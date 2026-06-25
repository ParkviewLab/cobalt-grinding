# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for `cobalt_grinding.host.provider`.

We don't hit the real Anthropic API here — that lives behind
`@integration` in `test_host_integration.py`. These tests verify our
adapter logic: arg forwarding, defaults, overrides, and the
ToolDescriptor → Anthropic-tools dict conversion. The SDK client is
stubbed so the test suite stays fast and offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cobalt_grinding.daemon.mcp_clients import ToolDescriptor
from cobalt_grinding.host.provider import AnthropicProvider, tool_descriptors_to_anthropic


@dataclass
class _FakeMessages:
    """Minimal stand-in for `client.messages` — records create() calls."""

    canned_response: Any
    create_calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        return self.canned_response


@dataclass
class _FakeAnthropicClient:
    messages: _FakeMessages


def _provider_with_fake(canned: Any) -> tuple[AnthropicProvider, _FakeMessages]:
    """Build a provider whose internal client is replaced by a fake.

    The constructor still runs (so AsyncAnthropic is briefly built and
    discarded) — that's a tiny cost and keeps the test honest about
    what __init__ does.
    """
    fake_messages = _FakeMessages(canned_response=canned)
    provider = AnthropicProvider(api_key="test-key", model="claude-opus-4-7")
    provider._client = _FakeAnthropicClient(messages=fake_messages)  # type: ignore[assignment]
    return provider, fake_messages


# ---- complete() arg forwarding ----


async def test_complete_forwards_required_args() -> None:
    provider, fake = _provider_with_fake(canned="response-sentinel")
    result = await provider.complete(
        system="You are helpful.",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result == "response-sentinel"
    assert len(fake.create_calls) == 1
    call = fake.create_calls[0]
    assert call["model"] == "claude-opus-4-7"
    assert call["system"] == "You are helpful."
    assert call["messages"] == [{"role": "user", "content": "hi"}]
    assert call["max_tokens"] == 4096
    assert "tools" not in call  # only included when provided


async def test_complete_passes_tools_when_present() -> None:
    provider, fake = _provider_with_fake(canned=None)
    tools = [{"name": "stub.greet", "description": "hi", "input_schema": {}}]
    await provider.complete(
        system="s",
        messages=[],
        tools=tools,
    )
    assert fake.create_calls[0]["tools"] == tools


async def test_complete_omits_tools_when_empty_list() -> None:
    provider, fake = _provider_with_fake(canned=None)
    await provider.complete(system="s", messages=[], tools=[])
    # Empty tools list is falsy → not forwarded. The Anthropic SDK rejects
    # `tools=[]` in some flows; safer to omit than send empty.
    assert "tools" not in fake.create_calls[0]


async def test_complete_overrides_model_and_max_tokens_per_call() -> None:
    provider, fake = _provider_with_fake(canned=None)
    await provider.complete(
        system="s",
        messages=[],
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
    )
    call = fake.create_calls[0]
    assert call["model"] == "claude-haiku-4-5-20251001"
    assert call["max_tokens"] == 512


async def test_provider_constructor_accepts_max_tokens_default() -> None:
    provider, fake = _provider_with_fake(canned=None)
    provider._max_tokens = 1024  # simulate construction with custom default
    await provider.complete(system="s", messages=[])
    assert fake.create_calls[0]["max_tokens"] == 1024


def test_model_property_exposes_default_model() -> None:
    provider = AnthropicProvider(api_key="k", model="claude-sonnet-4-6")
    assert provider.model == "claude-sonnet-4-6"


# ---- tool_descriptors_to_anthropic ----


def test_tool_descriptors_to_anthropic_uses_prefixed_name() -> None:
    descriptors = [
        ToolDescriptor(
            prefixed_name="codeparse.parse_file",
            raw_name="parse_file",
            description="Parse a source file.",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            owning_child="codeparse",
        ),
        ToolDescriptor(
            prefixed_name="codeparse.supported_languages",
            raw_name="supported_languages",
            description="List supported langs.",
            input_schema={"type": "object", "properties": {}},
            owning_child="codeparse",
        ),
    ]
    result = tool_descriptors_to_anthropic(descriptors)
    assert result == [
        {
            "name": "codeparse.parse_file",
            "description": "Parse a source file.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        {
            "name": "codeparse.supported_languages",
            "description": "List supported langs.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


def test_tool_descriptors_to_anthropic_empty_input_returns_empty() -> None:
    assert tool_descriptors_to_anthropic([]) == []
