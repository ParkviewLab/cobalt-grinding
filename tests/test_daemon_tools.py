# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the daemon's MCP tool wiring.

M2.7: `wiki.status` and `wiki.index` are now proxies over the smalt-mcp
MCP child instead of running in-process. These tests mock the MCP
supervisor (`app.mcp_clients`) to return canned smalt responses and
verify the proxy logic. The full end-to-end path (real cobalt-grinding
subprocess + real smalt-mcp child) is covered by
`test_daemon_integration.py`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from cobalt_grinding.app import App
from cobalt_grinding.config import Config, MCPConfig
from cobalt_grinding.daemon.mcp_clients import CallResult
from cobalt_grinding.daemon.server import build_server


def _make_app(tmp_path: Path) -> App:
    cfg = Config(
        smalt_dir=tmp_path / "wiki",
        cobalt_grinding_dir=tmp_path / "cobalt_grinding",
        mcp=MCPConfig(clients={}),  # override default smalt-mcp entry
    )
    return App(cfg)


def _text_content(payload: dict | list | str) -> MagicMock:
    """Mock a TextContent block carrying a JSON-encoded payload (matching
    what smalt-mcp's tools return)."""
    block = MagicMock()
    block.text = json.dumps(payload) if not isinstance(payload, str) else payload
    return block


def _ok_result(payload: dict | list | str) -> CallResult:
    """Build a CallResult that looks like a successful smalt-mcp tool call."""
    return CallResult(ok=True, is_error=False, content=[_text_content(payload)], error_text=None)


def _err_result(error_text: str) -> CallResult:
    """Build a CallResult that looks like a failed MCP dispatch."""
    return CallResult(ok=False, is_error=True, content=[], error_text=error_text)


def _install_mock_supervisor(app: App, call_tool_mock: AsyncMock) -> None:
    """Replace app._mcp_clients with a Mock so wiki.* proxies hit the
    mock's call_tool instead of trying to dispatch to a real child."""
    fake = MagicMock()
    fake.call_tool = call_tool_mock
    fake.list_tools = MagicMock(return_value=[])
    app._mcp_clients = fake


def test_build_server_registers_expected_tools(tmp_path: Path) -> None:
    """`build_server` registers the wiki.* tool set on the FastMCP server."""
    app = _make_app(tmp_path)
    server, scheduler, _mutex, _started = build_server(app)
    try:
        tools = asyncio.run(server.list_tools())
        tool_names = {t.name for t in tools}
        expected = {
            "wiki.status",
            "wiki.index",
            "wiki.ingest",
            "wiki.search",
            "wiki.get_page",
            "wiki.traverse",
            "wiki.find_gaps",
            "wiki.report_gap",
            "wiki.ask",
            "wiki.task_status",
            "wiki.task_list",
            "wiki.task_cancel",
        }
        assert expected.issubset(tool_names), f"missing: {expected - tool_names}"
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_status_proxies_to_smalt(tmp_path: Path) -> None:
    """`wiki.status` calls `smalt.status` via the supervisor and overlays
    daemon-specific fields on top of smalt's payload."""
    app = _make_app(tmp_path)
    smalt_status_payload = {
        "smalt_dir": "/tmp/smalt",
        "smalt_exists": True,
        "tables": {"pages": {"row_count": 42}},
        "embedding": {"provider": "fastembed", "model": "bge-small", "dim": 384},
    }
    call_tool = AsyncMock(return_value=_ok_result(smalt_status_payload))
    _install_mock_supervisor(app, call_tool)

    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.status", {})

        # Smalt's payload is included.
        assert result["smalt_dir"] == "/tmp/smalt"
        # Compatibility shims: workshop reads wiki_exists / pages_indexed.
        assert result["wiki_exists"] is True
        assert result["pages_indexed"] == 42
        assert result["embedding"]["provider"] == "fastembed"
        # Daemon overlay is present.
        assert result["milestone"] == "M2.7"
        assert "daemon" in result
        assert "corpus_mutex" in result
        assert "tasks" in result
        # Confirms the proxy actually called smalt.
        call_tool.assert_awaited_once_with("smalt-mcp", "status", {})
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_status_gracefully_handles_smalt_unreachable(tmp_path: Path) -> None:
    """When smalt is unreachable, wiki.status returns daemon-only state
    plus a `smalt_status_error` field — doesn't raise."""
    app = _make_app(tmp_path)
    call_tool = AsyncMock(return_value=_err_result("client smalt-mcp not running"))
    _install_mock_supervisor(app, call_tool)

    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.status", {})
        assert "smalt_status_error" in result
        assert "not running" in result["smalt_status_error"]
        # Daemon-side fields still present.
        assert result["milestone"] == "M2.7"
        # Stub values for wiki-side fields.
        assert result["wiki_exists"] is False
        assert result["pages_indexed"] == 0
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_index_proxies_to_smalt_reindex_all(tmp_path: Path) -> None:
    """`wiki.index` calls `smalt.reindex_all` and returns smalt's task_id."""
    app = _make_app(tmp_path)
    smalt_reindex_resp = {
        "task_id": "smalt-task-abc123",
        "kind": "reindex_all",
        "state": "running",
        "created_at": "2026-05-17T12:00:00Z",
    }
    call_tool = AsyncMock(return_value=_ok_result(smalt_reindex_resp))
    _install_mock_supervisor(app, call_tool)

    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.index", {})
        assert result["task_id"] == "smalt-task-abc123"
        assert result["kind"] == "reindex_all"
        call_tool.assert_awaited_once_with("smalt-mcp", "reindex_all", {})
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_index_returns_error_when_smalt_unreachable(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    call_tool = AsyncMock(return_value=_err_result("client smalt-mcp not running"))
    _install_mock_supervisor(app, call_tool)

    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.index", {})
        assert result["error"] == "smalt_unreachable"
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_ingest_writes_source_page_via_smalt(tmp_path: Path) -> None:
    """`wiki.ingest` runs the orchestrator against a single file and
    proxies to `smalt.write_page`. Chunk 2 also calls
    `smalt.find_by_alias` first for re-ingest detection."""
    app = _make_app(tmp_path)
    f = tmp_path / "hello.md"
    f.write_text("# Hello\n", encoding="utf-8")

    async def dispatch(_client: str, tool: str, args: dict) -> CallResult:  # type: ignore[type-arg]
        if tool == "find_by_alias":
            return _ok_result({"matches": [], "count": 0})
        if tool == "write_page":
            return _ok_result(
                {
                    "id": "file-hello-md__abc",
                    "path": "pages/sources/file-hello-md__abc.md",
                    "type": "source",
                    "mode": "create",
                }
            )
        if tool == "add_links":
            return _ok_result({"page_id": args["page_id"], "added": 0, "results": []})
        raise AssertionError(f"unexpected tool: {tool}")

    call_tool = AsyncMock(side_effect=dispatch)
    _install_mock_supervisor(app, call_tool)

    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.ingest", {"path": str(f)})
        assert result["source_id"] == "file-hello-md__abc"
        assert result["page_path"] == "pages/sources/file-hello-md__abc.md"
        assert result["files_ingested"] == 1
        assert result["location_kind"] == "file"
        # Smalt was called: find_by_alias + write_page.
        tools_called = [c.args[1] for c in call_tool.await_args_list]
        assert "find_by_alias" in tools_called
        assert "write_page" in tools_called
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_ingest_returns_error_payload_for_missing_path(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    call_tool = AsyncMock()  # should never be awaited
    _install_mock_supervisor(app, call_tool)

    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool(
            "wiki.ingest", {"path": str(tmp_path / "nope.md")}
        )
        assert result["error"] == "ingest_error"
        assert "does not exist" in result["message"]
        call_tool.assert_not_awaited()
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_ingest_returns_error_payload_for_unsupported_type(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG")
    call_tool = AsyncMock()
    _install_mock_supervisor(app, call_tool)

    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.ingest", {"path": str(f)})
        assert result["error"] == "ingest_error"
        assert "not supported" in result["message"]
        call_tool.assert_not_awaited()
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_ingest_rejects_invalid_h_lang(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    f = tmp_path / "header.h"
    f.write_text("/* hi */\n", encoding="utf-8")
    call_tool = AsyncMock()
    _install_mock_supervisor(app, call_tool)

    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.ingest", {"path": str(f), "h_lang": "rust"})
        assert result["error"] == "invalid_argument"
        call_tool.assert_not_awaited()
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_search_returns_hits(tmp_path: Path) -> None:
    """`wiki.search` proxies `smalt.search` and returns hits in the
    retrieve-orchestrator shape."""
    app = _make_app(tmp_path)

    async def dispatch(_client: str, tool: str, _args: dict) -> CallResult:  # type: ignore[type-arg]
        if tool == "search":
            return _ok_result(
                {
                    "count": 1,
                    "results": [
                        {
                            "id": "x",
                            "title": "X",
                            "type": "concept",
                            "score": 1.0,
                            "snippet": "snip",
                            "aliases": [],
                        }
                    ],
                }
            )
        raise AssertionError(f"unexpected tool: {tool}")

    call_tool = AsyncMock(side_effect=dispatch)
    _install_mock_supervisor(app, call_tool)

    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.search", {"query": "anything"})
        assert result["count"] == 1
        assert result["hits"][0]["id"] == "x"
        assert result["gap_detected"] is False
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_search_marks_gap_detected_on_empty_hits(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    call_tool = AsyncMock(return_value=_ok_result({"count": 0, "results": []}))
    _install_mock_supervisor(app, call_tool)
    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.search", {"query": "nope"})
        assert result["gap_detected"] is True
        assert result["hits"] == []
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_search_returns_error_payload_on_smalt_failure(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    call_tool = AsyncMock(return_value=_err_result("smalt-mcp not running"))
    _install_mock_supervisor(app, call_tool)
    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.search", {"query": "x"})
        assert result["error"] == "retrieve_error"
        assert "smalt-mcp" in result["message"]
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_get_page_proxies_smalt(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    call_tool = AsyncMock(
        return_value=_ok_result({"id": "x", "title": "X", "body": "body", "frontmatter": {}})
    )
    _install_mock_supervisor(app, call_tool)
    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.get_page", {"page_id": "x"})
        assert result["id"] == "x"
        call_tool.assert_awaited_once_with(
            "smalt-mcp", "read_page", {"page_id": "x", "fuzzy": True}
        )
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_traverse_proxies_smalt(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    call_tool = AsyncMock(return_value=_ok_result({"edges": [], "visited_nodes": ["x"]}))
    _install_mock_supervisor(app, call_tool)
    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.traverse", {"from_id": "x", "hops": 2})
        assert result["visited_nodes"] == ["x"]
        call_tool.assert_awaited_once_with("smalt-mcp", "traverse", {"from_id": "x", "hops": 2})
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_find_gaps_proxies_ebony(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    call_tool = AsyncMock(return_value=_ok_result({"gaps": [{"id": "a"}]}))
    _install_mock_supervisor(app, call_tool)
    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.find_gaps", {})
        assert result["gaps"][0]["id"] == "a"
        call_tool.assert_awaited_once_with("ebony-enriching", "list_gaps", {})
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_report_gap_proxies_ebony(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    call_tool = AsyncMock(return_value=_ok_result({"gap_id": "abc", "position": 1}))
    _install_mock_supervisor(app, call_tool)
    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool(
            "wiki.report_gap", {"query": "what is X", "why": "user asked"}
        )
        assert result["gap_id"] == "abc"
        call_tool.assert_awaited_once_with(
            "ebony-enriching",
            "add_gap",
            {"query": "what is X", "why": "user asked"},
        )
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_ask_returns_answer_with_citations(tmp_path: Path) -> None:
    """`wiki.ask` runs the converse orchestrator: search → hydrate →
    LLM-answer → validate citations."""
    app = _make_app(tmp_path)

    async def dispatch(_client: str, tool: str, args: dict) -> CallResult:  # type: ignore[type-arg]
        if tool == "search":
            return _ok_result(
                {
                    "count": 1,
                    "results": [
                        {
                            "id": "concept-mcp__1",
                            "title": "MCP",
                            "type": "concept",
                            "score": 1.0,
                            "snippet": "MCP is...",
                            "aliases": [],
                        }
                    ],
                }
            )
        if tool == "traverse":
            return _ok_result({"edges": [], "visited_nodes": ["concept-mcp__1"]})
        if tool == "read_page":
            return _ok_result(
                {
                    "id": args["page_id"],
                    "title": "MCP",
                    "type": "concept",
                    "body": "Model Context Protocol is a tool-use protocol.",
                    "frontmatter": {},
                }
            )
        raise AssertionError(f"unexpected tool: {tool}")

    call_tool = AsyncMock(side_effect=dispatch)
    _install_mock_supervisor(app, call_tool)

    # Mock LLM provider: returns a cited answer.
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "MCP is the Model Context Protocol [page:concept-mcp__1]."
    msg = MagicMock()
    msg.content = [text_block]
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(return_value=msg)
    fake_host = MagicMock()
    fake_host.provider = fake_provider
    app._host = fake_host

    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.ask", {"question": "What is MCP?"})
        assert "Model Context Protocol" in result["answer"]
        assert len(result["citations"]) == 1
        assert result["citations"][0]["page_id"] == "concept-mcp__1"
        assert result["citations"][0]["valid"] is True
        assert result["invalid_citations"] == []
        assert result["gap_detected"] is False
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_ask_returns_error_when_no_llm(tmp_path: Path) -> None:
    """Without an LLM provider configured, wiki.ask returns a
    structured error (no graceful degradation possible)."""
    app = _make_app(tmp_path)
    _install_mock_supervisor(app, AsyncMock())
    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.ask", {"question": "x"})
        assert result["error"] == "converse_error"
        assert "LLM provider" in result["message"]
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_ask_rejects_empty_question(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    _install_mock_supervisor(app, AsyncMock())
    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.ask", {"question": "  "})
        assert result["error"] == "converse_error"
        assert "question is required" in result["message"]
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_task_status_falls_back_to_smalt(tmp_path: Path) -> None:
    """`wiki.task_status` tries cobalt-grinding's scheduler first; for unknown
    ids it falls back to `smalt.task_status` (so smalt-side ingest task
    ids look native to clients)."""
    app = _make_app(tmp_path)
    smalt_task_payload = {
        "task_id": "smalt-task-xyz",
        "state": "succeeded",
        "result": {"pages_indexed": 100},
    }
    call_tool = AsyncMock(return_value=_ok_result(smalt_task_payload))
    _install_mock_supervisor(app, call_tool)

    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.task_status", {"task_id": "smalt-task-xyz"})
        assert result["task_id"] == "smalt-task-xyz"
        assert result["state"] == "succeeded"
        call_tool.assert_awaited_once_with(
            "smalt-mcp", "task_status", {"task_id": "smalt-task-xyz"}
        )
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


async def test_wiki_task_status_returns_not_found_when_neither_side_knows(
    tmp_path: Path,
) -> None:
    app = _make_app(tmp_path)
    # Smalt also doesn't know — returns a tool-side error.
    call_tool = AsyncMock(return_value=_err_result("task not found"))
    _install_mock_supervisor(app, call_tool)

    server, scheduler, _mutex, _started = build_server(app)
    try:
        _content, result = await server.call_tool("wiki.task_status", {"task_id": "nonexistent"})
        assert result["error"] == "not_found"
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)


def test_wiki_task_list_returns_cobalt_grinding_tasks_only(tmp_path: Path) -> None:
    """task_list / task_cancel are cobalt-grinding-only — no smalt fallback.
    Operators wanting smalt-side task listing call smalt directly."""
    app = _make_app(tmp_path)
    server, scheduler, _mutex, _started = build_server(app)
    try:
        # With no tasks queued, list is empty — proves the handler is wired
        # and doesn't try to consult smalt. FastMCP returns
        # `(content_blocks, structured_output)`; structured_output for a
        # list-returning tool wraps it under `result`.
        _content, structured = asyncio.run(server.call_tool("wiki.task_list", {}))
        assert structured == {"result": []}
    finally:
        scheduler.shutdown(wait=False, cancel_pending=True)
