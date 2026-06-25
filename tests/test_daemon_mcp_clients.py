# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for `cobalt_grinding.daemon.mcp_clients` — MCP child supervisor.

The "real subprocess" tests spawn `tests/fixtures/stub_mcp_server.py`
through Python and exercise the supervisor end-to-end (handshake,
tools/list, call_tool, crash/restart, timeouts, shutdown). They're
marked `@integration` per the project convention because they spawn
real subprocesses and pay ~1-2s startup latency each.

The pure-logic tests (backoff math, unknown-client dispatch, call-on-
not-running) run in the fast suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path

import pytest

from cobalt_grinding.daemon.mcp_clients import (
    CallResult,
    ChildConfig,
    ChildState,
    McpClientManager,
    _backoff_delay,
)

STUB_PATH = Path(__file__).parent / "fixtures" / "stub_mcp_server.py"


# ---- pure-logic tests (no subprocess) ----


def test_backoff_delay_caps_at_60s() -> None:
    # 1, 2, 4, 8, 16, 32, then cap.
    assert _backoff_delay(1) == 1.0
    assert _backoff_delay(2) == 2.0
    assert _backoff_delay(3) == 4.0
    assert _backoff_delay(6) == 32.0
    assert _backoff_delay(7) == 60.0
    assert _backoff_delay(20) == 60.0


def test_effective_prefix_defaults_to_name() -> None:
    cfg = ChildConfig(name="codeparse", command="x")
    assert cfg.effective_prefix == "codeparse"
    cfg2 = ChildConfig(name="codeparse", command="x", tool_prefix="cp")
    assert cfg2.effective_prefix == "cp"


async def test_call_unknown_client_returns_structured_error() -> None:
    cm = McpClientManager([])
    result = await cm.call_tool("nope", "anything", {})
    assert result.ok is False
    assert result.is_error is True
    assert "unknown client" in (result.error_text or "")


async def test_call_on_non_running_client_returns_structured_error() -> None:
    cfg = ChildConfig(name="dormant", command="/bin/true", autostart=False)
    cm = McpClientManager([cfg])
    # No start() — client is in PENDING.
    assert cm.get_state("dormant") is ChildState.PENDING
    result = await cm.call_tool("dormant", "whatever", {})
    assert result.ok is False
    assert "not running" in (result.error_text or "")


async def test_no_autostart_does_not_spawn_supervisor() -> None:
    cfg = ChildConfig(name="dormant", command="/bin/true", autostart=False)
    cm = McpClientManager([cfg])
    await cm.start()
    # No supervisor task created for this config.
    assert "dormant" not in cm._tasks
    assert cm.get_state("dormant") is ChildState.PENDING
    await cm.shutdown()


# ---- integration: real subprocess tests ----

pytestmark_integration = pytest.mark.integration


def _stub_config(**overrides: object) -> ChildConfig:
    """Build a ChildConfig that runs the in-tree stub MCP server."""
    base: dict[str, object] = {
        "name": "stub",
        "command": sys.executable,
        "args": [str(STUB_PATH)],
        # Generous defaults — Python subprocess + FastMCP import costs ~1-3s.
        "startup_timeout": 30.0,
        "call_timeout": 10.0,
    }
    base.update(overrides)
    # Pyright/ty-friendly: spell out args explicitly.
    return ChildConfig(
        name=str(base["name"]),
        command=str(base["command"]),
        args=list(base["args"]),  # type: ignore[arg-type]
        env=base.get("env"),  # type: ignore[arg-type]
        cwd=base.get("cwd"),  # type: ignore[arg-type]
        tool_prefix=base.get("tool_prefix"),  # type: ignore[arg-type]
        autostart=bool(base.get("autostart", True)),
        restart=base.get("restart", "on-failure"),  # type: ignore[arg-type]
        call_timeout=float(base["call_timeout"]),  # type: ignore[arg-type]
        startup_timeout=float(base["startup_timeout"]),  # type: ignore[arg-type]
    )


async def _wait_for_state(
    cm: McpClientManager,
    name: str,
    target: ChildState,
    *,
    timeout: float = 30.0,
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if cm.get_state(name) is target:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"client {name}: state {cm.get_state(name)} did not reach {target} within {timeout}s"
    )


@pytest.mark.integration
async def test_handshake_and_list_tools() -> None:
    cm = McpClientManager([_stub_config()])
    await cm.start()
    try:
        await _wait_for_state(cm, "stub", ChildState.RUNNING)
        tools = cm.list_tools()
        prefixed = {t.prefixed_name for t in tools}
        assert {"stub.greet", "stub.slow_greet"} <= prefixed
        # Inspect one tool for shape.
        greet = next(t for t in tools if t.prefixed_name == "stub.greet")
        assert greet.raw_name == "greet"
        assert greet.owning_child == "stub"
        assert "name" in greet.input_schema.get("properties", {})
    finally:
        await cm.shutdown()


@pytest.mark.integration
async def test_call_tool_round_trip() -> None:
    cm = McpClientManager([_stub_config()])
    await cm.start()
    try:
        await _wait_for_state(cm, "stub", ChildState.RUNNING)
        result = await cm.call_tool("stub", "greet", {"name": "Gary"})
        assert isinstance(result, CallResult)
        assert result.ok is True
        assert result.is_error is False
        # Content is a list of MCP content blocks; the stub returns a string,
        # which FastMCP wraps as a TextContent block.
        text = next((getattr(b, "text", None) for b in result.content), None)
        assert text == "Hello, Gary!"
    finally:
        await cm.shutdown()


@pytest.mark.integration
async def test_startup_timeout_does_not_block_other_children() -> None:
    """A wedged child with STUB_HANG_STARTUP=1 must trip its startup_timeout
    while other children handshake and serve normally.
    """
    wedged = _stub_config(
        name="wedged",
        env={**os.environ, "STUB_HANG_STARTUP": "1"},
        startup_timeout=2.0,
    )
    healthy = _stub_config(name="healthy")
    cm = McpClientManager([wedged, healthy])
    await cm.start()
    try:
        await _wait_for_state(cm, "healthy", ChildState.RUNNING)
        # Wedged child should not be RUNNING; it's either STARTING (still
        # in handshake), CRASHED (timeout fired), or back-off-cycling.
        assert cm.get_state("wedged") is not ChildState.RUNNING
    finally:
        await cm.shutdown(timeout=10.0)


@pytest.mark.integration
async def test_call_timeout_returns_structured_error_without_restart() -> None:
    """A call to slow_greet with STUB_HANG_TOOL=1 must time out and surface
    a structured error; the supervisor must NOT restart the child (slow ≠
    crashed)."""
    cfg = _stub_config(
        env={**os.environ, "STUB_HANG_TOOL": "1"},
        call_timeout=1.0,
    )
    cm = McpClientManager([cfg])
    await cm.start()
    try:
        await _wait_for_state(cm, "stub", ChildState.RUNNING)
        result = await cm.call_tool("stub", "slow_greet", {"name": "Gary"})
        assert result.ok is False
        assert "timed out" in (result.error_text or "")
        # Child still running; supervisor did not restart it.
        assert cm.get_state("stub") is ChildState.RUNNING
    finally:
        await cm.shutdown()


@pytest.mark.integration
async def test_restart_never_keeps_crashed_child_dead() -> None:
    """A bogus command will crash on spawn; with restart='never', the
    supervisor records CRASHED/STOPPED and gives up."""
    cfg = ChildConfig(
        name="bogus",
        command="/usr/bin/false",  # exits immediately with non-zero
        startup_timeout=2.0,
        restart="never",
    )
    cm = McpClientManager([cfg])
    await cm.start()
    try:
        # Wait for the supervisor task to settle.
        task = cm._tasks["bogus"]
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        # State should be STOPPED (supervisor task done) and not RUNNING.
        assert cm.get_state("bogus") in {ChildState.STOPPED, ChildState.CRASHED}
        assert cm.list_tools("bogus") == []
    finally:
        await cm.shutdown()


@pytest.mark.integration
async def test_shutdown_clears_state() -> None:
    cm = McpClientManager([_stub_config()])
    await cm.start()
    try:
        await _wait_for_state(cm, "stub", ChildState.RUNNING)
    finally:
        await cm.shutdown(timeout=10.0)
    # After shutdown, supervisor tasks done; state STOPPED.
    assert cm.get_state("stub") is ChildState.STOPPED
    assert all(t.done() for t in cm._tasks.values())


# Cleanup helper to silence pytest warnings about lingering signal handlers
# when the test module is rerun under -n auto. Not strictly necessary on macOS;
# defensive in case CI runs on Linux with strict signal handling.
@pytest.fixture(autouse=True)
def _restore_sigchld() -> object:
    handler = signal.getsignal(signal.SIGCHLD) if hasattr(signal, "SIGCHLD") else None
    yield
    if hasattr(signal, "SIGCHLD") and handler is not None:
        signal.signal(signal.SIGCHLD, handler)
