# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""cobalt-grinding's MCP host: agent runtime + tool dispatch + LLM provider.

cobalt-grinding is an MCP host both ways: it serves `wiki.*` tools to outside
clients (Claude Desktop, the cobalt_grinding CLI, future web UIs), AND it
spawns its own MCP children to reach external capabilities (parsers,
extractors, fetchers). This package is the latter — the in-process
agent runtime that runs the LLM tool-use loop on behalf of cobalt_grinding's
own subsystems (Ingest, Retrieve, Converse, ...).

The public surface agents see is a single coroutine:

    response = await app.host.run_agent(
        system="...",
        messages=[{"role": "user", "content": "..."}],
    )

The host handles tool selection (hybrid retrieval over `tools_index`),
dispatches `tool_use` blocks to the owning MCP child via
`McpClientManager`, feeds `tool_result` back into the loop, and returns
the final assistant message. Agents never touch MCP plumbing.
"""
