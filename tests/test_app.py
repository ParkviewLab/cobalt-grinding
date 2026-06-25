# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.app.App — host lifecycle.

M2.7: dropped tests for the old lazy `embedder` / `db` properties
(they no longer exist — the wiki's storage is smalt-mcp's
responsibility now; the embedder + tools_index lance store are
constructed inside `start_host()` against cobalt-grinding's own state dir).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cobalt_grinding.app import App
from cobalt_grinding.config import Config, MCPClientConfig, MCPConfig


def _bare_cfg(tmp_path: Path) -> Config:
    """Test-friendly Config: tmp wiki + cobalt_grinding dirs, no MCP children."""
    return Config(
        smalt_dir=tmp_path / "wiki",
        cobalt_grinding_dir=tmp_path / "cobalt_grinding",
        mcp=MCPConfig(clients={}),  # override default smalt-mcp entry
    )


def test_app_resolves_smalt_root_and_cobalt_grinding_dir(tmp_path: Path) -> None:
    cfg = Config(
        smalt_dir=tmp_path / "wiki" / "..." / "wiki",
        cobalt_grinding_dir=tmp_path / "cobalt_grinding",
        mcp=MCPConfig(clients={}),
    )
    app = App(cfg)
    # `.resolve()` collapses redundant segments — both resolved roots
    # should be absolute paths under tmp_path.
    assert app.smalt_root.is_absolute()
    assert app.cobalt_grinding_dir.is_absolute()
    assert (
        tmp_path in app.cobalt_grinding_dir.parents
        or app.cobalt_grinding_dir == tmp_path / "cobalt_grinding"
    )


def test_app_close_is_noop(tmp_path: Path) -> None:
    """Post-M2.7 close() has nothing to release (no held wiki-storage refs)."""
    app = App(_bare_cfg(tmp_path))
    app.close()  # should not raise


# ---- host lifecycle (M2.5 + M2.7) ----


def test_app_host_property_raises_before_start(tmp_path: Path) -> None:
    app = App(_bare_cfg(tmp_path))
    with pytest.raises(RuntimeError, match="start_host"):
        _ = app.host


def test_app_mcp_clients_property_raises_before_start(tmp_path: Path) -> None:
    app = App(_bare_cfg(tmp_path))
    with pytest.raises(RuntimeError, match="start_host"):
        _ = app.mcp_clients


async def test_start_host_phase_1_runs_without_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2.7: start_host's phase 1 (spawn MCP children) runs even when no
    LLM API key is set; phase 2 (build agent runtime) is skipped with a
    warning. This lets substrate bootstrap proceed without requiring an
    Anthropic key for cobalt-grinding to even start."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = App(_bare_cfg(tmp_path))
    try:
        await app.start_host()  # should NOT raise
        # Phase 1 ran: supervisor exists.
        assert app._mcp_clients is not None
        # Phase 2 skipped: no agent runtime.
        assert app._host is None
    finally:
        await app.shutdown_host()


async def test_start_host_constructs_host_when_key_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Avoid loading the real fastembed model in unit tests.
    _patch_fake_embedder(monkeypatch)
    app = App(_bare_cfg(tmp_path))
    try:
        await app.start_host()
        assert app._host is not None
        assert app._mcp_clients is not None
        assert app._tools_index is not None
        # No children configured, so list_tools is empty.
        assert app.mcp_clients.list_tools() == []
    finally:
        await app.shutdown_host()
        assert app._host is None


async def test_start_host_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _patch_fake_embedder(monkeypatch)
    app = App(_bare_cfg(tmp_path))
    try:
        await app.start_host()
        first_host = app._host
        await app.start_host()  # second call should be a no-op
        assert app._host is first_host
    finally:
        await app.shutdown_host()


async def test_start_host_with_mcp_client_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When [mcp.clients.*] entries exist, start_host hands them off to the
    supervisor. We point one at /usr/bin/false (autostart=False so we don't
    actually spawn it) and verify the config flow."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _patch_fake_embedder(monkeypatch)
    cfg = Config(
        smalt_dir=tmp_path / "wiki",
        cobalt_grinding_dir=tmp_path / "cobalt_grinding",
        mcp=MCPConfig(
            clients={
                "dormant": MCPClientConfig(command="/usr/bin/false", autostart=False),
            },
        ),
    )
    app = App(cfg)
    try:
        await app.start_host()
        # The supervisor knows about 'dormant' but didn't autostart it.
        assert "dormant" in app._mcp_clients._configs  # type: ignore[union-attr]
        assert "dormant" not in app._mcp_clients._tasks  # type: ignore[union-attr]
    finally:
        await app.shutdown_host()


# ---- helpers ----


def _patch_fake_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace cobalt_grinding.host.embedder.make_embedder with one that returns
    a FakeEmbedder, so unit tests don't pay the fastembed model-load cost.
    """
    from tests.conftest import FakeEmbedder

    monkeypatch.setattr(
        "cobalt_grinding.host.embedder.make_embedder",
        lambda cfg: FakeEmbedder(),
    )
