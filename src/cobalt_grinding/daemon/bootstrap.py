# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Substrate bootstrap, called by `cobalt-grinding` after the MCP host is up.

**M2.7 cleave**: the wiki and lab-notebook layouts are no longer
cobalt-grinding's responsibility — they belong to the `smalt-mcp` and
`ebony-enriching` MCP children's processes. This module delegates: it
waits for each substrate child to reach `RUNNING`, then calls
`<prefix>.bootstrap` via the MCP client. Both bootstrap tools are
idempotent.

It also ensures cobalt-grinding's *own* state dir exists (`cobalt_grinding_dir`,
default `~/.local/state/cobalt_grinding/`). This is where the tools_index
LanceDB store lives — separate from the substrate children's lance dirs
(LanceDB is process-local; two processes can't share a store).

Called from `daemon/main.py`'s lifespan, AFTER `await app.start_host()`
so the MCP children are already spawning.

Pre-cleave this module ran synchronously in `main.py` and used
in-process `lance.ensure_tables()`. Post-cleave the calls to
`mcp_clients.call_tool` require an event loop, so this module is
async — the lifespan calls it via `await`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from cobalt_grinding.daemon.mcp_clients import McpClientManager

logger = logging.getLogger(__name__)


# Names of the substrate children in the [mcp.clients.*] config. Defaults
# match `Config.mcp.clients` in `cobalt_grinding/config.py`. Operators who
# renamed a child in their config also need to rename here.
SMALT_CLIENT_NAME = "smalt-mcp"
EBONY_CLIENT_NAME = "ebony-enriching"


@dataclass(frozen=True)
class _Substrate:
    """One substrate to bootstrap. The bootstrap tool is always named
    `bootstrap` on the child side; the prefix is the MCP client name."""

    client_name: str
    label: str  # short human-readable; appears in logs


_SUBSTRATES: tuple[_Substrate, ...] = (
    _Substrate(client_name=SMALT_CLIENT_NAME, label="smalt"),
    _Substrate(client_name=EBONY_CLIENT_NAME, label="ebony"),
)


async def bootstrap(
    mcp_clients: McpClientManager,
    *,
    cobalt_grinding_dir: Path,
    substrate_wait_timeout: float = 30.0,
) -> None:
    """Bring all configured substrates up to a usable state.

    Steps:
      1. `cobalt_grinding_dir.mkdir()` — cobalt-grinding's own state dir (idempotent).
      2. For each substrate child (smalt-mcp, ebony-enriching):
         a. Wait for the child to be `RUNNING`.
         b. Call `<prefix>.bootstrap` via MCP (idempotent at the child's end).

    Per-substrate behavior is independent: if smalt-mcp isn't configured
    we still try ebony-enriching, and vice versa. A child that's
    configured but raises an error during bootstrap aborts that
    substrate's bootstrap but doesn't stop the others.

    Raises:
      `RuntimeError` if any configured substrate's bootstrap tool returns
        an error (after all substrates have been attempted).

    No-op for a substrate path if the child isn't configured — logs a
    warning instead of raising, since cobalt-grinding can still run agents
    against whatever other children are configured.
    """
    cobalt_grinding_dir = cobalt_grinding_dir.expanduser().resolve()
    cobalt_grinding_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("bootstrap: cobalt_grinding_dir at %s", cobalt_grinding_dir)

    errors: list[str] = []
    for sub in _SUBSTRATES:
        try:
            await _bootstrap_one(mcp_clients, sub, timeout=substrate_wait_timeout)
        except (RuntimeError, TimeoutError) as e:
            # Per-substrate error — record and continue so a flaky/missing
            # one substrate doesn't block the other. TimeoutError comes
            # from `wait_running` when an autostart child takes too long
            # to handshake (e.g. binary missing from PATH).
            logger.error("bootstrap: %s failed: %s", sub.label, e)
            errors.append(f"{sub.label}: {e}")

    if errors:
        raise RuntimeError("substrate bootstrap errors: " + "; ".join(errors))

    logger.info(
        "bootstrap: complete (substrates ready, cobalt_grinding_dir at %s)",
        cobalt_grinding_dir,
    )


async def _bootstrap_one(mcp_clients: McpClientManager, sub: _Substrate, *, timeout: float) -> None:
    """Bootstrap a single substrate. Logs warning + returns if the child
    isn't configured (or has autostart=false). Raises RuntimeError if
    the bootstrap call fails or the child never reaches RUNNING."""
    if not mcp_clients.is_configured(sub.client_name):
        logger.warning(
            "bootstrap: no %r MCP client configured; %s-* tools will return errors "
            "until you add a [mcp.clients.%s] block to your cobalt_grinding config.",
            sub.client_name,
            sub.label,
            sub.client_name,
        )
        return
    if not mcp_clients.is_autostart(sub.client_name):
        # Explicitly disabled by the operator (autostart=false). Skip the
        # wait-for-RUNNING — there's no child to wait for. Tools that
        # need this substrate will return clear errors at call time.
        logger.info(
            "bootstrap: skipping %s (client %r has autostart=false)",
            sub.label,
            sub.client_name,
        )
        return

    logger.info("bootstrap: waiting for %r child to be ready", sub.client_name)
    await mcp_clients.wait_running(sub.client_name, timeout=timeout)
    logger.info(
        "bootstrap: %r child is RUNNING; calling %s.bootstrap",
        sub.client_name,
        sub.label,
    )

    result = await mcp_clients.call_tool(sub.client_name, "bootstrap", {})
    if not result.ok or result.is_error:
        raise RuntimeError(
            f"{sub.label}.bootstrap failed: {result.error_text or '(no error text)'}"
        )
