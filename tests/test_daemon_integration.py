# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration test: real cobalt-grinding subprocess + bare MCP client over HTTP.

Starts `cobalt-grinding` as a child process pointed at a temp wiki, then
exercises the `wiki.status` and `wiki.index` MCP tools using the
official `mcp` SDK directly. Verifies daemon auto-bootstrap, end-to-end
MCP plumbing, and basic tool dispatch.

Pre-cleave this test imported `cobalt_grinding.cli.client.call_tool` (a thin
wrapper around `mcp.ClientSession` + `streamable_http_client`). The CLI
has moved to cogrind-workshop; this test now uses the bare SDK
directly via a small local helper. Behavioral coverage is unchanged.

Slow: pays daemon startup + fastembed model load. Marked @integration
so the fast suite stays fast.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.integration


# ---- bare MCP-client helper (replaces the old cobalt_grinding.cli.client.call_tool) ----


class _DaemonUnreachable(RuntimeError):
    """Raised when no cobalt-grinding is reachable at the configured URL."""


def _is_connection_error(exc: BaseException) -> bool:
    """Walk an exception chain / group to recognize 'connection refused' shapes."""
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | ConnectionError | OSError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_connection_error(e) for e in exc.exceptions)
    cause = exc.__cause__ or exc.__context__
    return cause is not None and _is_connection_error(cause)


@asynccontextmanager
async def _session(host: str, port: int):
    url = f"http://{host}:{port}/mcp"
    try:
        async with (
            streamable_http_client(url) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session
    except BaseException as e:
        if _is_connection_error(e):
            raise _DaemonUnreachable(
                f"could not reach cobalt-grinding at {url} — start one with: cobalt-grinding"
            ) from e
        raise


def _decode(result: Any) -> Any:
    """Pull the first text content block out of a tool-call result and JSON-decode it."""
    content = getattr(result, "content", None)
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


def _call_tool(name: str, arguments: dict[str, Any] | None = None, *, host: str, port: int) -> Any:
    """Synchronous one-shot tool call. Opens a fresh MCP session each time."""

    async def _run() -> Any:
        async with _session(host, port) as session:
            result = await session.call_tool(name, arguments or {})
            return _decode(result)

    return asyncio.run(_run())


# ---- daemon subprocess fixture ----


def _free_port() -> int:
    """Grab an unused localhost port for the daemon to bind to."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(host: str, port: int, *, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect((host, port))
                return
            except OSError:
                time.sleep(0.1)
    raise TimeoutError(f"daemon did not bind to {host}:{port} within {timeout}s")


def _write_config_without_substrates(tmp_path: Path, cobalt_grinding_dir: Path) -> Path:
    """Write a config.toml that explicitly disables all four default MCP
    children (smalt-mcp, ebony-enriching, deco-assaying, flint-slating),
    so the integration test doesn't require any of them installed.

    The default `Config.mcp.clients` autospawns all four. Without them
    on PATH this would log spawn-failure warnings (supervisor retries
    with backoff; cobalt-grinding stays up). Disabling explicitly keeps
    test output clean.

    Wiki-* tools that need smalt return graceful `smalt_unreachable` /
    `smalt_status_error` payloads — those error paths are what these
    integration tests now verify.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'cobalt_grinding_dir = "{cobalt_grinding_dir}"\n'
        "\n"
        "[mcp.clients.smalt-mcp]\n"
        'command = "/usr/bin/false"\n'  # no-op; never actually starts
        "autostart = false\n"
        "\n"
        "[mcp.clients.ebony-enriching]\n"
        'command = "/usr/bin/false"\n'
        "autostart = false\n"
        "\n"
        "[mcp.clients.deco-assaying]\n"
        'command = "/usr/bin/false"\n'
        "autostart = false\n"
        "\n"
        "[mcp.clients.flint-slating]\n"
        'command = "/usr/bin/false"\n'
        "autostart = false\n"
    )
    return config_path


@pytest.fixture
def running_daemon(tmp_path: Path) -> Iterator[tuple[str, int, Path]]:
    """Spawn `cobalt-grinding` against a temp wiki; yield (host, port, smalt_root).

    On any failure, dumps the daemon's stderr so the test output reveals
    *why* the daemon misbehaved instead of leaving the test author to guess.

    M2.7: smalt-mcp is NOT spawned in this test (the default config
    block is overridden). The daemon runs in "no substrates configured"
    mode; wiki.* tools that proxy to smalt return graceful errors.
    Full-stack tests against a real smalt-mcp child are deferred to a
    follow-up that installs smalt-mcp in the test env.
    """
    wiki = tmp_path / "wiki"
    cobalt_grinding_dir = tmp_path / "cobalt_grinding"
    host, port = "127.0.0.1", _free_port()
    config_path = _write_config_without_substrates(tmp_path, cobalt_grinding_dir)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cobalt_grinding.daemon.main",
            "--smalt",
            str(wiki),
            "--config",
            str(config_path),
            "--transport",
            "streamable-http",
            "--host",
            host,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env={**os.environ, "COBALT_GRINDING_SMALT_DIR": str(wiki)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def _dump_logs(reason: str) -> None:
        # Read whatever's available without blocking; drain after process exit.
        try:
            stdout, stderr = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        sys.stderr.write(f"\n--- cobalt-grinding {reason} ---\n")
        if stdout:
            sys.stderr.write(f"[stdout]\n{stdout.decode(errors='replace')}\n")
        if stderr:
            sys.stderr.write(f"[stderr]\n{stderr.decode(errors='replace')}\n")

    try:
        try:
            _wait_for_port(host, port)
        except TimeoutError:
            _dump_logs("never bound the port")
            raise
        yield host, port, wiki
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        # If the daemon exited unexpectedly, surface logs.
        if proc.returncode not in (0, -15, 143):
            _dump_logs(f"exited with code {proc.returncode}")


# ---- tests ----


def test_daemon_creates_cobalt_grinding_dir(running_daemon: tuple[str, int, Path]) -> None:
    """M2.7: cobalt-grinding creates its OWN state dir (cobalt_grinding_dir) on startup.
    The wiki dir is smalt-mcp's responsibility — without smalt-mcp
    configured (this test disables it), the wiki dir stays empty."""
    _host, _port, wiki = running_daemon
    # cobalt_grinding_dir is configured at tmp_path/cobalt_grinding via the test's
    # config TOML.
    cobalt_grinding_dir = wiki.parent / "cobalt_grinding"
    assert cobalt_grinding_dir.is_dir(), (
        f"cobalt_grinding_dir was not created: {cobalt_grinding_dir}"
    )
    # The wiki dir is NOT bootstrapped by cobalt-grinding anymore; without
    # smalt-mcp configured/running, the wiki layout stays empty.
    # (cobalt-grinding's bootstrap waits for smalt-mcp to be RUNNING; with
    # smalt-mcp disabled in this test's config, bootstrap skips with
    # a warning and the wiki dir is untouched.)


def test_wiki_status_tool_returns_daemon_state_when_smalt_unreachable(
    running_daemon: tuple[str, int, Path],
) -> None:
    """M2.7: wiki.status proxies to smalt.status. Without smalt-mcp
    configured (this test disables it), wiki.status returns daemon-only
    state plus a `smalt_status_error` field — gracefully degrades, no
    crash."""
    host, port, _wiki = running_daemon
    result = _call_tool("wiki.status", host=host, port=port)
    assert isinstance(result, dict)
    # Daemon overlay fields are always present.
    assert result["milestone"] == "M2.7"
    assert "daemon" in result
    assert result["daemon"]["uptime_seconds"] >= 0
    assert "corpus_mutex" in result
    assert "tasks" in result
    # smalt-mcp is disabled in this test's config, so the proxy can't
    # reach it — error field is surfaced, wiki_exists is False, etc.
    assert "smalt_status_error" in result
    assert result["wiki_exists"] is False
    assert result["pages_indexed"] == 0


def test_wiki_index_returns_smalt_unreachable_error(
    running_daemon: tuple[str, int, Path],
) -> None:
    """M2.7: wiki.index proxies to smalt.reindex_all. Without smalt-mcp,
    returns a structured error (no crash)."""
    host, port, _wiki = running_daemon
    result = _call_tool("wiki.index", {"full": False}, host=host, port=port)
    assert result["error"] == "smalt_unreachable"


def test_mcp_unreachable_when_no_daemon(tmp_path: Path) -> None:
    """A connection attempt against a non-listening port raises a clear error.

    Pre-cleave this test imported `cobalt_grinding.cli.client.DaemonUnreachable`;
    we now raise our own local equivalent (`_DaemonUnreachable`) since
    the test is what cares — the daemon side doesn't need to expose
    a typed connection-error class to its callers.
    """
    port = _free_port()  # nothing is listening here
    with pytest.raises(_DaemonUnreachable):
        _call_tool("wiki.status", host="127.0.0.1", port=port)


def test_daemon_shuts_down_cleanly_on_sigterm(tmp_path: Path) -> None:
    """SIGTERM during normal serving must exit cleanly within a reasonable timeout.

    Regression for an earlier bug where the daemon installed its own signal
    handler that called sys.exit(0) — that bypassed FastMCP's clean shutdown
    and could leave worker threads orphaned. The fix delegates signal handling
    to the underlying transport (uvicorn / KeyboardInterrupt) and runs cleanup
    in a `finally`. This test asserts the daemon stops promptly.
    """
    wiki = tmp_path / "wiki"
    cobalt_grinding_dir = tmp_path / "cobalt_grinding"
    config_path = _write_config_without_substrates(tmp_path, cobalt_grinding_dir)
    host, port = "127.0.0.1", _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cobalt_grinding.daemon.main",
            "--smalt",
            str(wiki),
            "--config",
            str(config_path),
            "--transport",
            "streamable-http",
            "--host",
            host,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env={**os.environ, "COBALT_GRINDING_SMALT_DIR": str(wiki)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_port(host, port)
        # Confirm it's actually serving before we kill it. M2.7: with
        # smalt-mcp disabled, wiki_exists is False (correctly), but the
        # daemon-overlay fields are still present, proving the server
        # is responsive.
        status = _call_tool("wiki.status", host=host, port=port)
        assert "daemon" in status

        proc.terminate()  # SIGTERM
        # Should exit well within 10s; if it hangs, that's the bug we're guarding against.
        rc = proc.wait(timeout=10)
        # POSIX: terminated by SIGTERM yields negative exit code -15;
        # a clean exit through finally yields 0. Either is acceptable.
        assert rc in (0, -15, 143), f"unexpected exit code: {rc}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
