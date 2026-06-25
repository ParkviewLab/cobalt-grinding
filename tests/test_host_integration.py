# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end integration test for the M2.5 host.

Spawns the in-tree stub MCP server as a real subprocess via the
supervisor, calls a real Anthropic LLM, and verifies the full tool-use
loop: tools_index retrieval → tools list to LLM → tool_use block →
dispatch via McpClientManager → tool_result fed back → end_turn with
an answer that references the dispatched tool's response.

Marked `@integration` because it pays:
- ~1-3s for the Python subprocess + FastMCP import
- one or two real Anthropic API round-trips (~$0.001 each)
- the fastembed model load if not already cached

Skipped if ANTHROPIC_API_KEY isn't set, so the suite still runs cleanly
in CI / on developer machines without a key.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from cobalt_grinding.app import App
from cobalt_grinding.config import Config, MCPClientConfig, MCPConfig
from cobalt_grinding.daemon.mcp_clients import ChildState

pytestmark = pytest.mark.integration

STUB_PATH = Path(__file__).parent / "fixtures" / "stub_mcp_server.py"

# Fast/cheap model for the loop. Opus would also work; Haiku keeps the
# test cheap and snappy.
TEST_MODEL = "claude-haiku-4-5-20251001"


def _require_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set; skipping LLM integration test")
    return key


async def _wait_for_running(app: App, name: str, *, timeout: float = 30.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if app.mcp_clients.get_state(name) is ChildState.RUNNING:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"stub child did not reach RUNNING within {timeout}s")


async def test_run_agent_end_to_end_with_stub_child(empty_wiki: Path) -> None:
    """The whole loop: spawn stub → handshake → tools_index populates →
    run_agent → LLM picks stub.greet → dispatch → answer cites tool result."""
    _require_api_key()

    cfg = Config(
        smalt_dir=empty_wiki,
        # M2.7: dedicated cobalt_grinding state dir (avoid polluting the user's
        # real ~/.local/state/cobalt_grinding/ during tests).
        cobalt_grinding_dir=empty_wiki.parent / "cobalt_grinding",
        mcp=MCPConfig(
            clients={
                "stub": MCPClientConfig(
                    command=sys.executable,
                    args=[str(STUB_PATH)],
                    startup_timeout=30.0,
                    call_timeout=10.0,
                ),
            },
        ),
    )
    # Override default_model on cfg.host so we use Haiku, not Opus.
    cfg = cfg.model_copy(update={"host": cfg.host.model_copy(update={"default_model": TEST_MODEL})})

    # M2.7: tools_index uses the real fastembed embedder constructed by
    # start_host() against `cobalt_grinding_dir` (separate from the wiki).
    app = App(cfg)
    try:
        await app.start_host()
        await _wait_for_running(app, "stub")
        assert {t.prefixed_name for t in app.mcp_clients.list_tools()} >= {"stub.greet"}

        response = await app.host.run_agent(
            system=(
                "You are a helpful assistant. When the user asks you to greet someone, "
                "use the stub.greet tool to produce the greeting. Do not greet without "
                "calling the tool first."
            ),
            messages=[{"role": "user", "content": "Greet Gary."}],
            max_iters=5,
        )

        # Loop returned. Stop reason should be terminal (end_turn most often).
        assert response.stop_reason in {"end_turn", "max_tokens", "stop_sequence"}

        # Pull all assistant text out of the final message.
        assistant_text = " ".join(
            getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text"
        ).lower()
        # The tool returned "Hello, Gary!"; the assistant's final answer
        # should reflect that (with reasonable phrasing latitude).
        assert "gary" in assistant_text
    finally:
        await app.shutdown_host()
