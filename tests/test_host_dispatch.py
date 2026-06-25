# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for `cobalt_grinding.host.dispatch`.

Pure-logic — these never spawn a subprocess. The supervisor's behaviour
is exercised in `test_daemon_mcp_clients.py` (with the real stub). Here
we only verify the dispatch layer's name-splitting and result-mapping
logic, against a fake `McpClientManager` that records calls and
returns canned `CallResult`s.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cobalt_grinding.daemon.mcp_clients import CallResult
from cobalt_grinding.host.dispatch import DispatchResult, dispatch


@dataclass
class _FakeManager:
    """Records dispatch calls; returns whatever `to_return` is set to."""

    to_return: CallResult
    calls: list[tuple[str, str, dict[str, Any] | None, float | None]]

    async def call_tool(
        self,
        client_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> CallResult:
        self.calls.append((client_name, tool_name, arguments, timeout))
        return self.to_return


def _make_fake(result: CallResult) -> _FakeManager:
    return _FakeManager(to_return=result, calls=[])


# ---- happy path ----


async def test_dispatch_splits_prefix_and_routes() -> None:
    fake = _make_fake(CallResult(ok=True, is_error=False, content=["ok"], error_text=None))

    result = await dispatch(
        "codeparse.parse_file",
        {"path": "/tmp/x.py"},
        mcp_clients=fake,  # type: ignore[arg-type]
    )

    assert isinstance(result, DispatchResult)
    assert result.ok is True
    assert result.content == ["ok"]
    assert fake.calls == [("codeparse", "parse_file", {"path": "/tmp/x.py"}, None)]


async def test_dispatch_passes_through_timeout() -> None:
    fake = _make_fake(CallResult(ok=True, is_error=False, content=[], error_text=None))

    await dispatch("c.t", {}, mcp_clients=fake, timeout=2.5)  # type: ignore[arg-type]

    assert fake.calls[0][3] == 2.5


# ---- error mapping ----


async def test_dispatch_propagates_supervisor_error() -> None:
    fake = _make_fake(
        CallResult(ok=False, is_error=True, content=[], error_text="client codeparse not running")
    )

    result = await dispatch("codeparse.parse_file", {}, mcp_clients=fake)  # type: ignore[arg-type]

    assert result.ok is False
    assert result.is_error is True
    assert result.error_text == "client codeparse not running"


async def test_dispatch_propagates_tool_reported_error() -> None:
    fake = _make_fake(
        CallResult(ok=True, is_error=True, content=["error blob"], error_text="tool said no")
    )

    result = await dispatch("c.t", {}, mcp_clients=fake)  # type: ignore[arg-type]

    assert result.ok is True  # dispatch succeeded
    assert result.is_error is True  # the tool reported failure
    assert result.error_text == "tool said no"


# ---- malformed names ----


async def test_dispatch_rejects_name_with_no_dot() -> None:
    fake = _make_fake(CallResult(ok=True, is_error=False, content=[], error_text=None))

    result = await dispatch("greet", {}, mcp_clients=fake)  # type: ignore[arg-type]

    assert result.ok is False
    assert result.is_error is True
    assert "malformed" in (result.error_text or "")
    # Supervisor was never called.
    assert fake.calls == []


async def test_dispatch_rejects_name_with_empty_tool() -> None:
    fake = _make_fake(CallResult(ok=True, is_error=False, content=[], error_text=None))

    result = await dispatch("client.", {}, mcp_clients=fake)  # type: ignore[arg-type]

    assert result.ok is False
    assert "malformed" in (result.error_text or "")
    assert fake.calls == []


async def test_dispatch_rejects_name_with_empty_client() -> None:
    fake = _make_fake(CallResult(ok=True, is_error=False, content=[], error_text=None))

    result = await dispatch(".tool", {}, mcp_clients=fake)  # type: ignore[arg-type]

    assert result.ok is False
    assert "malformed" in (result.error_text or "")
    assert fake.calls == []
