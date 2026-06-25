# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FastMCP server construction for the daemon.

Composes the App, the scheduler, the corpus-write mutex, and registers
all the wiki.* tool handlers. Returned `FastMCP` instance is started by
`main.py` with the configured transport (HTTP for daemon clients,
stdio for child-process clients).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from mcp.server.fastmcp import FastMCP

from cobalt_grinding.app import App
from cobalt_grinding.daemon.mutex import CorpusWriteMutex
from cobalt_grinding.daemon.scheduler import Scheduler
from cobalt_grinding.daemon.tools import register_tools

SERVER_NAME = "cobalt_grinding"

LifespanFactory = Callable[[FastMCP], AbstractAsyncContextManager[None] | AsyncIterator[None]]


@dataclass
class StartedAt:
    """Mutable timestamp container so main.py can stamp the *real* start
    moment (just before server.run()) and tools can read it later.

    The wiki.status uptime metric is meant to reflect "time since the daemon
    began accepting connections," not "time since FastMCP was constructed."
    Build-and-run typically run within milliseconds of each other so the
    difference is small, but it's nice to be honest.
    """

    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def stamp_now(self) -> None:
        self.timestamp = datetime.now(UTC)


def build_server(
    app: App,
    *,
    lifespan: LifespanFactory | None = None,
) -> tuple[FastMCP, Scheduler, CorpusWriteMutex, StartedAt]:
    """Construct the MCP server with all M2 tools wired up.

    `lifespan` is an optional async context manager factory that
    FastMCP runs around the server's main loop. The daemon uses it
    (M2.5) to start/shutdown the agent host (mcp_clients + tools_index).

    Returns
    -------
    server : the FastMCP instance, ready to run.
    scheduler : the task scheduler (callers may want to shut it down on exit).
    corpus_mutex : the single-writer mutex (exposed for tests).
    started_at : a mutable StartedAt the caller should stamp just before
        running the server, so wiki.status reports honest uptime.
    """
    if lifespan is not None:
        server = FastMCP(SERVER_NAME, lifespan=lifespan)  # ty: ignore[invalid-argument-type]
    else:
        server = FastMCP(SERVER_NAME)
    scheduler = Scheduler(max_workers=app.cfg.daemon.max_workers)
    corpus_mutex = CorpusWriteMutex()
    started_at = StartedAt()

    register_tools(
        server,
        app=app,
        scheduler=scheduler,
        corpus_mutex=corpus_mutex,
        started_at=started_at,
    )

    return server, scheduler, corpus_mutex, started_at
