# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""In-tree stub MCP server used by host integration tests.

Spawned as a subprocess via stdio transport. Exposes a small, deterministic
tool surface so the host's tool-use loop has something to dispatch against
without depending on an external child like `parkview-codeparse-server`.

Behavior toggles via environment variables (used by supervisor failure-mode
tests):

  STUB_HANG_STARTUP=1  — sleep forever before calling server.run(), so the
                         host's startup_timeout fires.
  STUB_HANG_TOOL=1     — make `slow_greet` block forever, so the host's
                         call_timeout fires on a tool call.

Run directly: `python tests/fixtures/stub_mcp_server.py`.
"""

from __future__ import annotations

import os
import time

from mcp.server.fastmcp import FastMCP

server = FastMCP("stub")


@server.tool(name="greet")
def greet(name: str) -> str:
    """Return a friendly greeting for the named person."""
    return f"Hello, {name}!"


@server.tool(name="slow_greet")
def slow_greet(name: str) -> str:
    """Greet the named person — but block forever when STUB_HANG_TOOL=1."""
    if os.environ.get("STUB_HANG_TOOL") == "1":
        while True:
            time.sleep(3600)
    return f"Hi (eventually), {name}!"


def main() -> None:
    if os.environ.get("STUB_HANG_STARTUP") == "1":
        while True:
            time.sleep(3600)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
