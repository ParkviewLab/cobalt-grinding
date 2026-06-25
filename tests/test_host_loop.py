# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for `cobalt_grinding.host.loop` — the LLM tool-use loop.

A `_ScriptedProvider` returns canned `Message`-shaped objects in order,
so we can drive the loop through realistic sequences (end_turn,
tool_use → end_turn, tool_use → tool_use → end_turn, iter cap, etc.)
without hitting the real Anthropic API. Dispatch is stubbed via a
fake `McpClientManager` that returns canned `CallResult`s — the
supervisor's real-subprocess behaviour is covered in
`test_daemon_mcp_clients.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from cobalt_grinding.daemon.mcp_clients import CallResult
from cobalt_grinding.host.loop import (
    DEFAULT_MAX_ITERS,
    IterCapExceeded,
    _mcp_content_to_anthropic,
    _to_tool_result_block,
    run_tool_use_loop,
)

# ---- minimal stand-ins for SDK content blocks ----


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _Msg:
    """Message-shaped object the loop consumes via .content / .stop_reason."""

    content: list[Any]
    stop_reason: str


# ---- fake provider that scripts a sequence of responses ----


@dataclass
class _ScriptedProvider:
    responses: list[_Msg]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete(self, **kwargs: Any) -> _Msg:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("ScriptedProvider ran out of canned responses")
        return self.responses.pop(0)


@dataclass
class _FakeManagerAlwaysOk:
    """Returns a successful CallResult for every dispatch."""

    canned_text: str = "tool result"
    calls: list[tuple[str, str, dict[str, Any] | None, float | None]] = field(default_factory=list)

    async def call_tool(
        self,
        client_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> CallResult:
        self.calls.append((client_name, tool_name, arguments, timeout))
        return CallResult(
            ok=True,
            is_error=False,
            content=[_TextBlock(text=self.canned_text)],
            error_text=None,
        )


@dataclass
class _FakeManagerAlwaysFails:
    """Returns a dispatch error for every call."""

    error_text: str = "client codeparse not running"

    async def call_tool(self, *args: Any, **kwargs: Any) -> CallResult:
        return CallResult(
            ok=False,
            is_error=True,
            content=[],
            error_text=self.error_text,
        )


# ---- happy-path ----


async def test_loop_returns_immediately_on_end_turn() -> None:
    provider = _ScriptedProvider(
        responses=[_Msg(content=[_TextBlock(text="hi")], stop_reason="end_turn")]
    )
    manager = _FakeManagerAlwaysOk()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "hello"}]

    result = await run_tool_use_loop(
        provider=provider,
        system="be helpful",
        messages=messages,
        tools=[{"name": "stub.greet", "description": "", "input_schema": {}}],
        mcp_clients=manager,  # type: ignore[arg-type]
    )

    assert result.stop_reason == "end_turn"
    # Provider called once.
    assert len(provider.calls) == 1
    # Assistant turn appended; no tool_result turn.
    assert messages[-1]["role"] == "assistant"
    # Manager never dispatched anything.
    assert manager.calls == []


async def test_loop_dispatches_one_tool_use_then_end_turn() -> None:
    provider = _ScriptedProvider(
        responses=[
            _Msg(
                content=[_ToolUseBlock(id="tu1", name="stub.greet", input={"name": "Gary"})],
                stop_reason="tool_use",
            ),
            _Msg(content=[_TextBlock(text="all done")], stop_reason="end_turn"),
        ]
    )
    manager = _FakeManagerAlwaysOk(canned_text="Hello, Gary!")
    messages: list[dict[str, Any]] = [{"role": "user", "content": "greet me"}]

    result = await run_tool_use_loop(
        provider=provider,
        system="s",
        messages=messages,
        tools=None,
        mcp_clients=manager,  # type: ignore[arg-type]
    )

    assert result.stop_reason == "end_turn"
    assert manager.calls == [("stub", "greet", {"name": "Gary"}, None)]
    # Conversation: original user, assistant tool_use, user tool_result, assistant text.
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    tool_result_msg = messages[2]
    block = tool_result_msg["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "tu1"
    assert block["content"][0] == {"type": "text", "text": "Hello, Gary!"}


async def test_loop_dispatches_multiple_tool_uses_in_one_turn() -> None:
    provider = _ScriptedProvider(
        responses=[
            _Msg(
                content=[
                    _ToolUseBlock(id="tu1", name="stub.greet", input={"name": "A"}),
                    _ToolUseBlock(id="tu2", name="stub.greet", input={"name": "B"}),
                ],
                stop_reason="tool_use",
            ),
            _Msg(content=[_TextBlock(text="ok")], stop_reason="end_turn"),
        ]
    )
    manager = _FakeManagerAlwaysOk()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "double"}]

    await run_tool_use_loop(
        provider=provider,
        system="s",
        messages=messages,
        tools=None,
        mcp_clients=manager,  # type: ignore[arg-type]
    )

    # Both tool_uses dispatched.
    assert len(manager.calls) == 2
    # Tool-result message has two blocks, matching ids.
    tr_msg = messages[2]
    assert [b["tool_use_id"] for b in tr_msg["content"]] == ["tu1", "tu2"]


async def test_loop_chains_two_iterations_of_tool_use() -> None:
    provider = _ScriptedProvider(
        responses=[
            _Msg(
                content=[_ToolUseBlock(id="t1", name="stub.greet", input={})],
                stop_reason="tool_use",
            ),
            _Msg(
                content=[_ToolUseBlock(id="t2", name="stub.greet", input={})],
                stop_reason="tool_use",
            ),
            _Msg(content=[_TextBlock(text="done")], stop_reason="end_turn"),
        ]
    )
    manager = _FakeManagerAlwaysOk()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]

    await run_tool_use_loop(
        provider=provider,
        system="s",
        messages=messages,
        tools=None,
        mcp_clients=manager,  # type: ignore[arg-type]
    )

    assert len(manager.calls) == 2
    assert len(provider.calls) == 3


# ---- error / dispatch-failure paths ----


async def test_loop_feeds_dispatch_error_back_as_is_error_block() -> None:
    provider = _ScriptedProvider(
        responses=[
            _Msg(
                content=[_ToolUseBlock(id="tu1", name="stub.greet", input={})],
                stop_reason="tool_use",
            ),
            _Msg(content=[_TextBlock(text="recovered")], stop_reason="end_turn"),
        ]
    )
    manager = _FakeManagerAlwaysFails(error_text="client stub not running")
    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]

    await run_tool_use_loop(
        provider=provider,
        system="s",
        messages=messages,
        tools=None,
        mcp_clients=manager,  # type: ignore[arg-type]
    )

    tool_result_block = messages[2]["content"][0]
    assert tool_result_block["is_error"] is True
    assert tool_result_block["content"] == "client stub not running"
    # The LLM was given a chance to recover; subsequent end_turn confirms.
    assert messages[-1]["content"][0].text == "recovered"


# ---- iter cap ----


async def test_loop_raises_when_iter_cap_exceeded() -> None:
    # Provider always asks for another tool — the loop should give up after
    # max_iters round-trips.
    provider = _ScriptedProvider(
        responses=[
            _Msg(
                content=[_ToolUseBlock(id=f"t{i}", name="stub.greet", input={})],
                stop_reason="tool_use",
            )
            for i in range(5)
        ]
    )
    manager = _FakeManagerAlwaysOk()

    with pytest.raises(IterCapExceeded):
        await run_tool_use_loop(
            provider=provider,
            system="s",
            messages=[{"role": "user", "content": "x"}],
            tools=None,
            mcp_clients=manager,  # type: ignore[arg-type]
            max_iters=3,
        )


# ---- terminal stop reasons other than end_turn ----


async def test_loop_returns_on_max_tokens_stop_reason() -> None:
    provider = _ScriptedProvider(
        responses=[_Msg(content=[_TextBlock(text="cut off")], stop_reason="max_tokens")]
    )
    manager = _FakeManagerAlwaysOk()
    result = await run_tool_use_loop(
        provider=provider,
        system="s",
        messages=[{"role": "user", "content": "x"}],
        mcp_clients=manager,  # type: ignore[arg-type]
    )
    assert result.stop_reason == "max_tokens"


async def test_loop_returns_on_unknown_stop_reason() -> None:
    """An unknown terminal reason from a future API change shouldn't loop forever."""
    provider = _ScriptedProvider(
        responses=[_Msg(content=[_TextBlock(text="?")], stop_reason="future_value_we_do_not_know")]
    )
    manager = _FakeManagerAlwaysOk()
    result = await run_tool_use_loop(
        provider=provider,
        system="s",
        messages=[{"role": "user", "content": "x"}],
        mcp_clients=manager,  # type: ignore[arg-type]
    )
    assert result.stop_reason == "future_value_we_do_not_know"


# ---- on_event progress emission ----


async def test_loop_emits_progress_events() -> None:
    captured: list[dict[str, Any]] = []
    provider = _ScriptedProvider(
        responses=[
            _Msg(
                content=[_ToolUseBlock(id="tu1", name="stub.greet", input={})],
                stop_reason="tool_use",
            ),
            _Msg(content=[_TextBlock(text="done")], stop_reason="end_turn"),
        ]
    )
    manager = _FakeManagerAlwaysOk()

    await run_tool_use_loop(
        provider=provider,
        system="s",
        messages=[{"role": "user", "content": "x"}],
        mcp_clients=manager,  # type: ignore[arg-type]
        on_event=captured.append,
    )

    kinds = [e["event"] for e in captured]
    assert kinds[0] == "loop_start"
    assert "tool_use_started" in kinds
    assert "tool_result_received" in kinds
    assert kinds[-1] == "end_turn"


async def test_loop_swallows_bad_event_callback() -> None:
    def bad(_: dict[str, Any]) -> None:
        raise RuntimeError("oops")

    provider = _ScriptedProvider(responses=[_Msg(content=[], stop_reason="end_turn")])
    manager = _FakeManagerAlwaysOk()
    # Should not raise — bad on_event must not interrupt the loop.
    await run_tool_use_loop(
        provider=provider,
        system="s",
        messages=[],
        mcp_clients=manager,  # type: ignore[arg-type]
        on_event=bad,
    )


# ---- helpers ----


def test_to_tool_result_block_success_passes_content_through() -> None:
    result = type(
        "R",
        (),
        {"ok": True, "is_error": False, "content": [_TextBlock(text="x")], "error_text": None},
    )()
    block = _to_tool_result_block("id1", result)  # type: ignore[arg-type]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "id1"
    assert block["content"] == [{"type": "text", "text": "x"}]
    assert "is_error" not in block


def test_to_tool_result_block_error_uses_text_with_is_error() -> None:
    result = type("R", (), {"ok": False, "is_error": True, "content": [], "error_text": "broken"})()
    block = _to_tool_result_block("id1", result)  # type: ignore[arg-type]
    assert block["is_error"] is True
    assert block["content"] == "broken"


def test_mcp_content_to_anthropic_handles_text_and_falls_back_to_str() -> None:
    blocks = _mcp_content_to_anthropic([_TextBlock(text="hello"), object()])
    assert blocks[0] == {"type": "text", "text": "hello"}
    # Object stringification fell back.
    assert blocks[1]["type"] == "text"
    assert isinstance(blocks[1]["text"], str)


def test_default_max_iters_constant() -> None:
    """Sanity: cap is reasonable, not 0 or unset."""
    assert DEFAULT_MAX_ITERS >= 5
