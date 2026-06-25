# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Supervisor for `[mcp.clients.*]` children.

cobalt-grinding is an MCP host: at startup it spawns each configured child MCP
server, performs the MCP handshake, captures each child's `tools/list`,
and keeps the connection alive for as long as the daemon runs. Crashed
children are restarted with capped exponential backoff; a wedged
child at startup never pins daemon startup; a wedged tool call returns
a structured timeout (the LLM can recover) without auto-restarting the
child (slow ≠ crashed).

Public surface:

  cm = McpClientManager([ChildConfig(...), ...])
  await cm.start()                        # spawn supervisors; returns immediately
  cm.list_tools()                         # snapshot of all live tools
  await cm.call_tool(name, tool, args)    # round-trip a tool call
  await cm.shutdown()                     # terminate every child

The host's `dispatch.py` is the only caller of `call_tool` in normal
operation. `tools_index.py` reads `list_tools()` to populate its
LanceDB-backed retrieval index.

Concurrency model: all methods run on the asyncio event loop. There are
no threads here. State mutations between awaits don't race because the
event loop serializes them; per-child supervisor tasks don't share
mutable state with each other (only with the manager, via single-await
read-modify-write boundaries).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


# ---- Config + result types ----


@dataclass(frozen=True)
class ChildConfig:
    """Per-child config, parsed from `[mcp.clients.<name>]`.

    `tool_prefix=None` means "use the section name." Mandatory prefix
    avoids silent collisions when two children expose tools with the
    same raw name (e.g. both a code parser and a pdf parser exposing
    `parse_file`).
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    tool_prefix: str | None = None
    autostart: bool = True
    restart: Literal["on-failure", "always", "never"] = "on-failure"
    call_timeout: float = 30.0
    startup_timeout: float = 10.0

    @property
    def effective_prefix(self) -> str:
        return self.tool_prefix or self.name


class ChildState(StrEnum):
    PENDING = "pending"  # supervisor not started yet
    STARTING = "starting"  # subprocess spawned; handshake in flight
    RUNNING = "running"  # handshake done; ready for calls
    CRASHED = "crashed"  # last attempt failed; in backoff
    STOPPED = "stopped"  # supervisor exited; no more restarts


@dataclass(frozen=True)
class ToolDescriptor:
    """One tool exposed by a child, prefixed for collision-avoidance."""

    prefixed_name: str  # "codeparse.parse_file"
    raw_name: str  # "parse_file" (what the child actually answers to)
    description: str
    input_schema: dict[str, Any]
    owning_child: str  # client name from config


@dataclass(frozen=True)
class CallResult:
    """Outcome of a `call_tool`. Wraps MCP `CallToolResult` and adds the
    supervisor's own dispatch-layer flags.

    `ok=False` means dispatch failed (timeout, child not running, unknown
    tool); `is_error=True` means the tool ran but signalled failure
    itself (MCP `isError`). Both shapes are surfaced to the LLM as a
    `tool_result(is_error=true)` block — the host doesn't care which
    layer failed, only the LLM does.
    """

    ok: bool
    is_error: bool
    content: list[Any]  # MCP content blocks (TextContent / Image / ...)
    error_text: str | None  # human-readable; populated whenever ok=False or is_error=True


# ---- Manager ----


class McpClientManager:
    """Spawn / supervise / dispatch-to a fleet of MCP child servers."""

    def __init__(
        self,
        configs: Iterable[ChildConfig],
        *,
        errlog: Any | None = None,
    ) -> None:
        self._configs: dict[str, ChildConfig] = {c.name: c for c in configs}
        # stderr destination for spawned children. Defaults to the daemon's
        # own stderr; tests redirect to capture child output.
        self._errlog = errlog if errlog is not None else sys.stderr
        self._states: dict[str, ChildState] = {n: ChildState.PENDING for n in self._configs}
        self._sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, list[ToolDescriptor]] = {n: [] for n in self._configs}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._shutdown_event = asyncio.Event()

    # ---- lifecycle ----

    async def start(self) -> None:
        """Kick off background supervisor tasks for every autostart child.
        Returns immediately — handshakes happen in the supervisor tasks
        so a wedged child can't block daemon startup.
        """
        for cfg in self._configs.values():
            if not cfg.autostart:
                continue
            self._tasks[cfg.name] = asyncio.create_task(
                self._supervise_child(cfg),
                name=f"mcp-client:{cfg.name}",
            )

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Signal every supervisor to stop and wait for clean exit."""
        self._shutdown_event.set()
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks.values(), return_exceptions=True),
                timeout=timeout,
            )
        except TimeoutError:
            for task in self._tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    # ---- introspection ----

    def get_state(self, name: str) -> ChildState:
        return self._states.get(name, ChildState.PENDING)

    def is_configured(self, name: str) -> bool:
        """True if `name` appears in `[mcp.clients.*]`."""
        return name in self._configs

    def is_autostart(self, name: str) -> bool:
        """True if `name` is configured with `autostart=true`. False if
        `name` isn't configured at all, or is configured with
        `autostart=false`. Bootstrap-style callers use this to skip the
        wait-for-RUNNING step on substrates the operator has explicitly
        disabled.
        """
        cfg = self._configs.get(name)
        return cfg is not None and cfg.autostart

    async def wait_running(
        self, name: str, *, timeout: float = 10.0, poll_interval: float = 0.1
    ) -> None:
        """Poll until child `name` reaches `ChildState.RUNNING` or timeout.

        Added in M2.7 (Step 2) so the daemon's bootstrap step can wait
        for the smalt-mcp child to be handshake-complete before calling
        `smalt.bootstrap` via MCP. The supervisor handshakes children
        asynchronously after `start()` returns; without this helper,
        early callers would race the handshake and see `client not
        running` errors from `call_tool`.

        Raises:
          `KeyError` if `name` isn't configured.
          `TimeoutError` if `name` doesn't reach RUNNING within `timeout`.
        """
        if name not in self._configs:
            raise KeyError(f"unknown MCP client: {name!r}")
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            state = self.get_state(name)
            if state is ChildState.RUNNING:
                return
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(
                    f"MCP client {name!r} did not reach RUNNING within "
                    f"{timeout}s (current state: {state})"
                )
            await asyncio.sleep(poll_interval)

    def list_tools(self, client_name: str | None = None) -> list[ToolDescriptor]:
        """Snapshot of currently-known tools.

        If `client_name` is None, returns tools from every running child.
        Snapshot is built by copy so callers can iterate without worrying
        about supervisor mutations mid-loop.
        """
        if client_name is not None:
            return list(self._tools.get(client_name, []))
        return [t for tools in self._tools.values() for t in tools]

    # ---- dispatch ----

    async def call_tool(
        self,
        client_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> CallResult:
        """Route a tool call to the named child. Never raises for runtime
        problems — every failure becomes a `CallResult(ok=False, ...)`.
        """
        cfg = self._configs.get(client_name)
        if cfg is None:
            return CallResult(
                ok=False, is_error=True, content=[], error_text=f"unknown client: {client_name}"
            )
        session = self._sessions.get(client_name)
        if session is None or self._states[client_name] is not ChildState.RUNNING:
            return CallResult(
                ok=False,
                is_error=True,
                content=[],
                error_text=f"client {client_name} not running (state={self._states[client_name]})",
            )

        effective_timeout = timeout if timeout is not None else cfg.call_timeout
        try:
            async with asyncio.timeout(effective_timeout):
                result = await session.call_tool(tool_name, arguments or {})
        except TimeoutError:
            return CallResult(
                ok=False,
                is_error=True,
                content=[],
                error_text=(
                    f"call to {client_name}.{tool_name} timed out after {effective_timeout}s"
                ),
            )
        except Exception as e:
            return CallResult(
                ok=False,
                is_error=True,
                content=[],
                error_text=f"dispatch error to {client_name}.{tool_name}: {e}",
            )
        return CallResult(
            ok=True,
            is_error=bool(result.isError),
            content=list(result.content),
            error_text=_extract_error_text(result) if result.isError else None,
        )

    # ---- internals ----

    async def _supervise_child(self, cfg: ChildConfig) -> None:
        """One child's lifetime: spawn → handshake → serve → on death,
        backoff and retry until shutdown."""
        attempt = 0
        try:
            while not self._shutdown_event.is_set():
                attempt += 1
                clean_exit = False
                try:
                    await self._run_child_once(cfg)
                    clean_exit = True
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(
                        "[client:%s] attempt %d failed: %s",
                        cfg.name,
                        attempt,
                        e,
                    )
                finally:
                    self._tools[cfg.name] = []
                    self._sessions.pop(cfg.name, None)
                    self._states[cfg.name] = ChildState.CRASHED

                if self._shutdown_event.is_set():
                    break
                if cfg.restart == "never":
                    break
                if cfg.restart == "on-failure" and clean_exit:
                    # A clean exit when restart=on-failure isn't a failure.
                    # In practice MCP children don't exit cleanly on their
                    # own; this branch mostly guards against future shapes.
                    break

                delay = _backoff_delay(attempt)
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=delay)
                    break  # shutdown fired during backoff
                except TimeoutError:
                    continue
        finally:
            self._states[cfg.name] = ChildState.STOPPED

    async def _run_child_once(self, cfg: ChildConfig) -> None:
        """Spawn the subprocess, handshake, serve calls until either the
        connection dies or shutdown is requested. Raises on handshake
        timeout or connection loss; the supervisor catches and retries.
        """
        self._states[cfg.name] = ChildState.STARTING
        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            env=cfg.env,
            cwd=cfg.cwd,
        )
        async with (
            stdio_client(params, errlog=self._errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            async with asyncio.timeout(cfg.startup_timeout):
                await session.initialize()
                tools_resp = await session.list_tools()

            prefix = cfg.effective_prefix
            self._tools[cfg.name] = [
                ToolDescriptor(
                    prefixed_name=f"{prefix}.{t.name}",
                    raw_name=t.name,
                    description=t.description or "",
                    input_schema=t.inputSchema,
                    owning_child=cfg.name,
                )
                for t in tools_resp.tools
            ]
            self._sessions[cfg.name] = session
            self._states[cfg.name] = ChildState.RUNNING
            logger.info("[client:%s] running with %d tool(s)", cfg.name, len(self._tools[cfg.name]))

            await self._park_until_done(cfg, session)

    async def _park_until_done(self, cfg: ChildConfig, session: ClientSession) -> None:
        """Block until shutdown is requested OR the session dies (ping
        round-trip fails). Either way, return so the surrounding `async
        with` blocks tear the child down cleanly.
        """
        shutdown_task = asyncio.create_task(self._shutdown_event.wait())
        health_task = asyncio.create_task(_ping_until_dead(cfg.name, session))
        try:
            done, pending = await asyncio.wait(
                {shutdown_task, health_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                if t is health_task:
                    exc = t.exception()
                    if exc is not None:
                        raise exc
        finally:
            for t in (shutdown_task, health_task):
                if not t.done():
                    t.cancel()


# ---- helpers ----


def _backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 60.0) -> float:
    """Cap exponential backoff. attempt=1 → 1s, 2 → 2s, 3 → 4s, ..., capped at 60s."""
    return min(cap, base * (2 ** (attempt - 1)))


async def _ping_until_dead(name: str, session: ClientSession, *, interval: float = 2.0) -> None:
    """Periodically ping the child. Returns only by raising — when the
    ping fails, the connection is gone and the supervisor needs to restart.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            async with asyncio.timeout(5.0):
                await session.send_ping()
        except Exception as e:
            raise ConnectionError(f"client {name} ping failed: {e}") from e


def _extract_error_text(result: Any) -> str:
    """Pull a human-readable error string out of an MCP CallToolResult
    that has isError=True. Falls back to a stringified dump."""
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            return text
    return f"tool reported error (no text block in result): {result!r}"
