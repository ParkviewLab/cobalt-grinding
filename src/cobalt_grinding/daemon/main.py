# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Entry point for `cobalt-grinding` — the long-running cobalt-grinding MCP server.

Responsibilities, in order:

1. Load layered config.
2. Construct the App, scheduler, and MCP server.
3. Spawn supervised MCP children (smalt-mcp + any others under
   `[mcp.clients.*]`) via the M2.5 host.
4. Bootstrap substrates by calling each child's `bootstrap` tool
   (M2.7+; runs after children reach `RUNNING`).
5. Run the MCP server on the configured transport (default HTTP).
6. On shutdown, stop the scheduler cleanly + tear down child supervisors.

There is no separate `cobalt_grinding init` step — bootstrap runs every time
the daemon starts and is idempotent. `cobalt-grinding` *is* the MCP server.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import click

from cobalt_grinding import __version__
from cobalt_grinding.app import App
from cobalt_grinding.config import Config, load_config
from cobalt_grinding.daemon.bootstrap import bootstrap
from cobalt_grinding.daemon.server import build_server

logger = logging.getLogger("cobalt-grinding")


@click.command(name="cobalt-grinding")
@click.version_option(__version__, prog_name="cobalt-grinding")
@click.option(
    "--smalt",
    "smalt_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to the Smalt directory (the wiki). Overrides config and COBALT_GRINDING_SMALT_DIR.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a user-global config.toml.",
)
@click.option(
    "--transport",
    type=click.Choice(["streamable-http", "stdio"], case_sensitive=False),
    default=None,
    help="MCP transport. Defaults to the value in config (typically streamable-http for daemon use).",
)
@click.option(
    "--host",
    default=None,
    help="Bind host for HTTP transport (default 127.0.0.1).",
)
@click.option(
    "--port",
    type=int,
    default=None,
    help="Bind port for HTTP transport (default 7474).",
)
@click.option(
    "--log-level",
    type=click.Choice(["debug", "info", "warning", "error"], case_sensitive=False),
    default=None,
    help="Override log level (config default: info).",
)
def run(
    smalt_dir: Path | None,
    config_path: Path | None,
    transport: str | None,
    host: str | None,
    port: int | None,
    log_level: str | None,
) -> None:
    """Run the cobalt-grinding MCP server. The Smalt dir is auto-initialized on first run."""
    cfg = load_config(user_config_path=config_path, smalt_dir_override=smalt_dir)
    _setup_logging(log_level or cfg.logging.level)

    chosen_transport = (transport or cfg.mcp.transport).lower()
    if chosen_transport == "http":  # alias
        chosen_transport = "streamable-http"

    bind_host, bind_port = _resolve_bind(cfg, host, port)

    logger.info(
        "cobalt-grinding starting (version %s, wiki=%s, cobalt_grinding_dir=%s, transport=%s, "
        "tools_index_embed=%s)",
        __version__,
        cfg.smalt_dir,
        cfg.cobalt_grinding_dir,
        chosen_transport,
        cfg.embedding.model,
    )

    # Inject the substrate dirs into their MCP children's env if the user
    # hasn't set them explicitly. Keeps smalt_dir → SMALT_DIR and
    # ebony_dir → EBONY_ENRICHING_DIR coupled automatically — the children
    # operate on the same dirs cobalt-grinding thinks of as the wiki / lab
    # notebook. Operators wanting different dirs set env explicitly in
    # `[mcp.clients.<name>].env`. deco-assaying is stateless; no env injection.
    _inject_smalt_dir(cfg)
    _inject_ebony_dir(cfg)

    # Ensure cobalt_grinding's own state dir exists. The tools_index
    # LanceDB store lives under it; ToolsIndex construction in
    # start_host() requires the parent dir to exist (LanceDB's
    # `connect()` happily creates its own store dir, but mkdir-on-
    # construction here is cheap and avoids edge cases).
    cfg.cobalt_grinding_dir.expanduser().resolve().mkdir(parents=True, exist_ok=True)

    # Compose the daemon: App + scheduler + MCP server.
    #
    # The host (mcp_clients + tools_index + LLM provider) is async-started
    # inside FastMCP's lifespan so it runs in FastMCP's event loop. Without
    # ANTHROPIC_API_KEY the host won't start; the daemon stays up and any
    # tool calls that need the host (M3 ingest, etc.) fail with a clear
    # error at call time.
    app = App(cfg)

    @asynccontextmanager
    async def lifespan(_server: object) -> AsyncIterator[None]:
        try:
            await app.start_host()
            logger.info("host: started")
        except Exception as e:
            # Don't take down the daemon if the host can't start — log and
            # continue. Tools that need the host (M3 ingest) will fail at
            # call time with a clear error.
            logger.warning("host: failed to start (%s); daemon continues without it", e)

        # Substrate bootstrap. Must run AFTER start_host() so the MCP
        # children are spawning; bootstrap() waits for each substrate
        # child (smalt-mcp, ebony-enriching) to be RUNNING then calls
        # its `bootstrap` tool. Non-fatal on failure so the daemon stays
        # up (wiki.* tool calls will return errors).
        try:
            await bootstrap(app.mcp_clients, cobalt_grinding_dir=cfg.cobalt_grinding_dir)
            logger.info("bootstrap: complete")
        except RuntimeError as e:
            logger.warning("host: not started; skipping bootstrap (%s)", e)
        except Exception:
            logger.exception("bootstrap: failed; daemon continues but wiki tools may not work")

        try:
            yield
        finally:
            try:
                await app.shutdown_host()
                logger.info("host: stopped")
            except Exception:
                logger.exception("host: shutdown failed")

    server, scheduler, _mutex, started_at = build_server(app, lifespan=lifespan)

    # 3. Run the server. FastMCP blocks until the transport closes.
    #
    # Signal handling is left to the underlying transport: uvicorn (streamable-http)
    # installs its own SIGINT/SIGTERM handlers and returns cleanly from server.run();
    # stdio surfaces KeyboardInterrupt, which we catch below. Either way the
    # `finally` block runs, draining the scheduler and closing shared resources.
    # We intentionally do NOT install our own signal handlers — doing so would
    # tear out uvicorn's, jump out of the asyncio loop via sys.exit(), and skip
    # the cleanup we actually want.
    try:
        # Stamp the real "began accepting connections" moment just before
        # we hand control over to the transport. wiki.status reads this.
        started_at.stamp_now()
        if chosen_transport == "stdio":
            # stdio path uses FastMCP's lifespan handling directly — the
            # `lifespan=` kwarg we passed to FastMCP() gets called.
            server.run(transport="stdio")
        else:
            # HTTP path: FastMCP's `streamable_http_app()` hardcodes the
            # Starlette lifespan to its own session manager and ignores
            # the `lifespan=` we passed to FastMCP() (see FastMCP source
            # `server.py` `streamable_http_app` → `Starlette(...,
            # lifespan=lambda app: self.session_manager.run(...))`). We
            # compose: build the Starlette app, capture FastMCP's
            # lifespan, swap in our outer wrapper, then run uvicorn
            # ourselves. Outer = our lifespan (host start + substrate
            # bootstrap); inner = FastMCP's session manager. Order
            # matters: host must be up before the session manager starts
            # accepting requests, and must be torn down after the session
            # manager has drained.
            _run_streamable_http(server, lifespan, host=bind_host, port=bind_port)
    except KeyboardInterrupt:
        logger.info("received KeyboardInterrupt")
    finally:
        # wait=True so in-flight workers finish (or observe their cancel tokens)
        # before we drop the App's resources out from under them.
        logger.info("draining scheduler...")
        scheduler.shutdown(wait=True, cancel_pending=True)
        app.close()
        logger.info("cobalt-grinding stopped")


def _resolve_bind(cfg: Config, host: str | None, port: int | None) -> tuple[str, int]:
    """CLI flag > config > built-in default. Each layer validated separately."""
    return host or cfg.mcp.host, port or cfg.mcp.port


def _inject_smalt_dir(cfg: Config) -> None:
    """Couple the smalt-mcp child's `SMALT_DIR` env to `cfg.smalt_dir`.

    M2.7: the smalt-mcp child is supposed to index the same directory
    cobalt-grinding thinks of as the wiki. If the user hasn't set `SMALT_DIR`
    explicitly in `[mcp.clients.smalt-mcp].env`, we set it to
    `cfg.smalt_dir`. Explicit user env overrides everything (operators
    who want smalt-mcp to point at a different dir can set it).

    No-op if the smalt-mcp child isn't configured (operator removed it).
    """
    _inject_env_dir(cfg, client_name="smalt-mcp", env_var="SMALT_DIR", path=cfg.smalt_dir)


def _inject_ebony_dir(cfg: Config) -> None:
    """Couple the ebony-enriching child's `EBONY_ENRICHING_DIR` env to `cfg.ebony_dir`.

    Mirrors `_inject_smalt_dir`. The ebony-enriching child operates on the
    same dir cobalt-grinding thinks of as the lab notebook. If the user
    hasn't set `EBONY_ENRICHING_DIR` explicitly in
    `[mcp.clients.ebony-enriching].env`, we set it to `cfg.ebony_dir`.

    No-op if the ebony-enriching child isn't configured.
    """
    _inject_env_dir(
        cfg, client_name="ebony-enriching", env_var="EBONY_ENRICHING_DIR", path=cfg.ebony_dir
    )


def _inject_env_dir(cfg: Config, *, client_name: str, env_var: str, path: Path) -> None:
    """Shared body for `_inject_*_dir`: set `<client>.env[<env_var>]` to
    `path` (expanduser+resolve) if the user hasn't set it. No-op if the
    client isn't configured.
    """
    child_cfg = cfg.mcp.clients.get(client_name)
    if child_cfg is None:
        return
    env = dict(child_cfg.env or {})
    if env_var not in env:
        env[env_var] = str(path.expanduser().resolve())
        cfg.mcp.clients[client_name] = child_cfg.model_copy(update={"env": env})
        logger.debug("%s: injected env var %s", client_name, env_var)


def _run_streamable_http(
    server: object,
    outer_lifespan: object,
    *,
    host: str,
    port: int,
) -> None:
    """Run FastMCP's streamable-HTTP transport with our lifespan composed
    around FastMCP's hardcoded session-manager lifespan.

    Why this exists: `FastMCP.streamable_http_app()` returns a Starlette
    instance with `lifespan=lambda app: self.session_manager.run(...)`
    hardcoded — the `lifespan=` kwarg passed to `FastMCP()` is dropped
    on the HTTP path. Without composition, our `start_host()` +
    `bootstrap()` would never run for HTTP transport. We rebuild the
    Starlette lifespan as an outer-then-inner stack:

        async with outer_lifespan(app):     # cobalt-grinding: host + bootstrap
            async with inner_lifespan(app): # FastMCP: session manager
                yield                        # serve requests

    Startup order: our host comes up + substrates bootstrap before
    FastMCP starts accepting connections. Shutdown order: FastMCP
    drains its sessions, then we tear down the host.
    """
    import contextlib

    import uvicorn

    starlette_app = server.streamable_http_app()  # ty: ignore[unresolved-attribute]
    inner_lifespan = starlette_app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def composed(app: object):
        async with outer_lifespan(app), inner_lifespan(app):  # ty: ignore[call-non-callable]
            yield

    starlette_app.router.lifespan_context = composed

    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="info",
    )
    uvicorn.Server(config).run()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":
    run()
