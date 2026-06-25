# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`App` — the shared-resource container.

Every subsystem and every daemon tool handler takes an `App` instance. The
daemon constructs one at startup and reuses it for the life of the process.
External MCP clients (cogrind-workshop, Claude Desktop) never construct
an `App` — they only talk to the daemon over MCP.

`App` owns:

- `cfg` — the loaded `Config`
- `smalt_root` — resolved path to the wiki on disk (now smalt-mcp's domain)
- `cobalt_grinding_dir` — resolved path to cobalt-grinding's own state dir (M2.7+)
- `host` — the agent runtime (M2.5; built by `start_host()`)
- `mcp_clients` — supervisor for `[mcp.clients.*]` children (M2.5)
- `tools_index` — LanceDB-backed registry for tool retrieval (M2.5)

**M2.7 change**: the in-process `embedder` and `db` properties are gone.
Wiki storage now lives in the smalt-mcp child's process (accessed via
`mcp_clients.call_tool("smalt-mcp", ...)`). The tools_index keeps its own
LanceDB + embedder, but in `cobalt_grinding_dir` (cobalt-grinding's own state dir),
not under `smalt_root`. The embedder + lance-connect helper moved from
`cobalt_grinding/storage/` to `cobalt_grinding/host/` to reflect their new role:
host infrastructure, not wiki storage.

The host pieces are async-started — `mcp_clients.start()` spawns
supervisor tasks that need an event loop. The daemon plugs `start_host`
/ `shutdown_host` into FastMCP's `lifespan` context manager so they run
inside FastMCP's own asyncio loop.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from cobalt_grinding.config import Config

if TYPE_CHECKING:
    from cobalt_grinding.daemon.mcp_clients import McpClientManager
    from cobalt_grinding.host.api import Host
    from cobalt_grinding.host.provider import AnthropicProvider
    from cobalt_grinding.host.tools_index import ToolsIndex

logger = logging.getLogger(__name__)


class App:
    """Container for shared, long-lived resources.

    The host (`mcp_clients` + `tools_index` + `provider` + `host`) is
    NOT lazy — it's started explicitly via `await app.start_host()` so
    the daemon can plug it into FastMCP's lifespan and surface a clean
    error if e.g. `ANTHROPIC_API_KEY` is missing rather than blowing up
    on first agent call.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg: Config = cfg
        self.smalt_root: Path = cfg.smalt_dir.expanduser().resolve()
        self.cobalt_grinding_dir: Path = cfg.cobalt_grinding_dir.expanduser().resolve()
        # Host pieces (M2.5) — built in start_host(), torn down in shutdown_host().
        self._host: Host | None = None
        self._mcp_clients: McpClientManager | None = None
        self._tools_index: ToolsIndex | None = None
        self._provider: AnthropicProvider | None = None
        self._last_tools_snapshot: frozenset[tuple[str, str]] = frozenset()

    @property
    def host(self) -> Host:
        """The agent runtime. Available only after `start_host()`."""
        if self._host is None:
            raise RuntimeError(
                "app.host accessed before start_host(); call await app.start_host() first"
            )
        return self._host

    @property
    def mcp_clients(self) -> McpClientManager:
        if self._mcp_clients is None:
            raise RuntimeError("app.mcp_clients accessed before start_host()")
        return self._mcp_clients

    # ---- host lifecycle ----

    async def start_host(self) -> None:
        """Construct + start the host (supervisor + provider + tools index).

        Two-phase, idempotent:

          1. **Spawn MCP children** (always — no API key needed).
             After this phase, `self._mcp_clients` is non-None and
             substrate children are starting up. `daemon/bootstrap.py`
             can call `smalt.bootstrap` via the supervisor as soon as
             the smalt-mcp child reaches RUNNING.

          2. **Build the agent runtime** (LLM provider + tools_index +
             Host). Skipped if `ANTHROPIC_API_KEY` is missing — the
             daemon stays up with the supervisor running but with no
             agent runtime; tools that need the host (M3 ingest etc.)
             will fail at call time with a clear error. wiki.* proxies
             still work because they use `mcp_clients` directly.

        Calling a second time is a no-op once both phases have
        completed; if phase 1 succeeded but phase 2 was skipped, a
        retry will attempt phase 2 again (e.g. after the user exports
        the API key).
        """
        # ---- phase 1: spawn MCP children ----
        if self._mcp_clients is None:
            from cobalt_grinding.daemon.mcp_clients import ChildConfig, McpClientManager

            child_configs = [
                ChildConfig(
                    name=name,
                    command=cc.command,
                    args=cc.args,
                    env=cc.env,
                    cwd=cc.cwd,
                    tool_prefix=cc.tool_prefix,
                    autostart=cc.autostart,
                    restart=cc.restart,  # ty: ignore[invalid-argument-type]
                    call_timeout=cc.call_timeout,
                    startup_timeout=cc.startup_timeout,
                )
                for name, cc in self.cfg.mcp.clients.items()
            ]
            self._mcp_clients = McpClientManager(child_configs)
            await self._mcp_clients.start()
            logger.info(
                "host: supervisor started with %d configured child(ren)",
                len(child_configs),
            )

        # ---- phase 2: agent runtime (needs LLM API key) ----
        if self._host is not None:
            return

        api_key = os.environ.get(self.cfg.llm.api_key_env)
        if not api_key:
            logger.warning(
                "host: %s not set in environment; agent runtime not "
                "started. Substrate children (smalt-mcp etc.) are still "
                "spawned and wiki.* proxy tools work; tools that need "
                "the agent runtime (M3 ingest) will fail at call time.",
                self.cfg.llm.api_key_env,
            )
            return

        from cobalt_grinding.host import tools_index_db
        from cobalt_grinding.host.api import Host
        from cobalt_grinding.host.embedder import make_embedder
        from cobalt_grinding.host.provider import AnthropicProvider
        from cobalt_grinding.host.tools_index import ToolsIndex

        model = self.cfg.host.default_model or self.cfg.llm.model
        self._provider = AnthropicProvider(api_key=api_key, model=model)

        # M2.7: tools_index uses its OWN LanceDB store under cobalt_grinding_dir
        # (NOT the wiki's lance store — that belongs to the smalt-mcp child
        # process now). The embedder is host infrastructure, not wiki storage.
        self._tools_index = ToolsIndex(
            tools_index_db.connect(self.cobalt_grinding_dir),
            make_embedder(self.cfg),
        )
        # Initial population happens lazily on first run_agent — supervisors
        # may still be handshaking. The host's refresh callback keeps it
        # synced with whatever the supervisor reports at call time.

        self._host = Host(
            provider=self._provider,
            mcp_clients=self._mcp_clients,
            tools_index=self._tools_index,
            refresh_tools=self._refresh_tools_index_if_stale,
            default_top_k=self.cfg.host.tools_top_k,
            default_max_iters=self.cfg.host.max_iters,
        )

    async def shutdown_host(self) -> None:
        """Terminate every supervised child cleanly. Idempotent."""
        if self._mcp_clients is not None:
            await self._mcp_clients.shutdown()
        self._host = None
        self._mcp_clients = None
        self._tools_index = None
        self._provider = None

    def _refresh_tools_index_if_stale(self) -> None:
        """Compare the supervisor's current tools snapshot with what the
        index was last populated with; rebuild only on change.

        This is how a child that crashed and recovered gets its tools
        back into the index without a daemon restart. Called at the
        start of every run_agent invocation. The snapshot comparison is
        cheap (frozenset of (client, prefixed_name) pairs); rebuilds
        only happen when the supervisor reports a different set.
        """
        assert self._mcp_clients is not None
        assert self._tools_index is not None
        descriptors = self._mcp_clients.list_tools()
        snapshot = frozenset((d.owning_child, d.prefixed_name) for d in descriptors)
        if snapshot == self._last_tools_snapshot:
            return
        self._tools_index.rebuild_all(descriptors)
        self._last_tools_snapshot = snapshot
        logger.debug("host: tools_index rebuilt with %d descriptors", len(descriptors))

    def close(self) -> None:
        """Release held resources. Idempotent.

        Does NOT shut down the host — that's an async operation handled
        in `shutdown_host()` (called from the daemon's lifespan teardown).

        M2.7: no in-process LanceDB / embedder references to release
        here anymore — the tools_index owns its own resources and is
        cleaned up via shutdown_host().
        """
        # No-op post-M2.7. Kept for API stability in case future state
        # needs explicit release.
