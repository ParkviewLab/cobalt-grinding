# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""MCP tool handlers exposed by `cobalt-grinding`.

Each tool is registered on the FastMCP server in `server.py`. Tool
handlers are thin: they read state, or they enqueue work on the
scheduler and return a `task_id`, or they proxy to an MCP child
(post-M2.7, that's how `wiki.status` and `wiki.index` work).

**M2.7 change**: cobalt-grinding no longer owns the wiki's storage layer.
`wiki.status` and `wiki.index` are now thin proxies over the `smalt-mcp`
MCP child:

- `wiki.status` → calls `smalt.status` via the MCP supervisor and
  returns its payload, overlaid with daemon-specific fields
  (uptime, mutex, cobalt-grinding-side task counts).
- `wiki.index` → calls `smalt.reindex_all` and returns smalt's
  `task_id` directly. Clients poll `wiki.task_status` (which falls
  back to `smalt.task_status` for ids it doesn't recognize) for
  progress.
- `wiki.task_status` → tries cobalt-grinding's scheduler first, falls
  back to `smalt.task_status` (so smalt-side task ids look native
  to clients).
- `wiki.task_list` / `wiki.task_cancel` → unchanged; cobalt-grinding-only.
  (smalt-side tasks are listed/cancelled via smalt directly.)

The substrate name (`smalt-mcp`) is taken from
`bootstrap.SMALT_CLIENT_NAME` so the two stay in sync.
"""

from __future__ import annotations

import json
import logging
import platform
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from mcp.server.fastmcp import FastMCP

from cobalt_grinding import __version__
from cobalt_grinding.converse import orchestrator as converse
from cobalt_grinding.daemon.bootstrap import SMALT_CLIENT_NAME
from cobalt_grinding.daemon.scheduler import TaskStatus
from cobalt_grinding.ingest.orchestrator import IngestError
from cobalt_grinding.ingest.orchestrator import ingest as run_ingest
from cobalt_grinding.retrieve import orchestrator as retrieve

if TYPE_CHECKING:
    from cobalt_grinding.app import App
    from cobalt_grinding.daemon.mcp_clients import CallResult
    from cobalt_grinding.daemon.mutex import CorpusWriteMutex
    from cobalt_grinding.daemon.scheduler import Scheduler
    from cobalt_grinding.daemon.server import StartedAt

logger = logging.getLogger(__name__)


def register_tools(
    server: FastMCP,
    *,
    app: App,
    scheduler: Scheduler,
    corpus_mutex: CorpusWriteMutex,
    started_at: StartedAt,
) -> None:
    """Attach cobalt-grinding's wiki.* tools to `server`. Called from server.py at startup."""

    @server.tool(name="wiki.status")
    async def wiki_status() -> dict[str, Any]:
        """Return daemon + wiki state. Read-only; cheap; safe to poll.

        Post-M2.7: proxies `smalt.status` via the MCP supervisor for
        wiki-side state (page counts, lance tables, etc.), and overlays
        daemon-specific fields (uptime, mutex, cobalt-grinding-side task
        counts). If the smalt-mcp child isn't reachable, the wiki-side
        fields are absent and the response includes
        `smalt_status_error` with a short reason.
        """
        # Daemon-specific overlay — always present. `smalt_dir` is
        # smalt's authoritative field (we let it pass through), so we
        # don't include it here; `cobalt_grinding_dir` IS cobalt-grinding-specific
        # so we do include it.
        daemon_overlay: dict[str, Any] = {
            "version": __version__,
            "milestone": "M2.7",
            "cobalt_grinding_dir": str(app.cobalt_grinding_dir),
            "daemon": {
                "started_at": started_at.timestamp.isoformat(),
                "uptime_seconds": (datetime.now(UTC) - started_at.timestamp).total_seconds(),
                "host": platform.node(),
                "platform": platform.platform(),
            },
            "corpus_mutex": {
                "locked": corpus_mutex.locked,
                "holder": corpus_mutex.holder,
            },
            "tasks": {
                "queued": len(scheduler.list_tasks(status=TaskStatus.QUEUED)),
                "running": len(scheduler.list_tasks(status=TaskStatus.RUNNING)),
                "succeeded": len(scheduler.list_tasks(status=TaskStatus.SUCCEEDED)),
                "failed": len(scheduler.list_tasks(status=TaskStatus.FAILED)),
                "cancelled": len(scheduler.list_tasks(status=TaskStatus.CANCELLED)),
            },
        }

        # Wiki-side state via smalt-mcp proxy.
        try:
            smalt_state = await _call_smalt(app, "status", {})
            # smalt's status payload becomes the base; daemon overlay wins
            # for any colliding keys (we want daemon-truth for daemon
            # fields).
            payload: dict[str, Any] = {**smalt_state, **daemon_overlay}
            # Compatibility shim: workshop's existing format_status() reads
            # `wiki_exists` and `pages_indexed`. Smalt's status uses
            # `smalt_exists` and tables[pages].row_count. Map them so
            # existing clients keep rendering correctly.
            if "smalt_exists" in smalt_state and "wiki_exists" not in payload:
                payload["wiki_exists"] = smalt_state["smalt_exists"]
            if "pages_indexed" not in payload:
                tables = smalt_state.get("tables", {}) or {}
                pages_tbl = tables.get("pages")
                if isinstance(pages_tbl, dict):
                    payload["pages_indexed"] = pages_tbl.get("row_count", 0)
            # Compatibility shim: workshop reads embedding.provider/model/dim.
            if "embedding" not in payload:
                emb = smalt_state.get("embedding") or {}
                payload["embedding"] = {
                    "provider": emb.get("provider", "?"),
                    "model": emb.get("model", "?"),
                    "dim": emb.get("dim", 0),
                }
            return payload
        except _SmaltUnreachable as e:
            logger.warning(
                "wiki.status: smalt-mcp unreachable (%s); returning daemon-only state", e
            )
            return {
                **daemon_overlay,
                # Surface cobalt-grinding's view of the smalt_dir as a
                # fallback since smalt's authoritative value is
                # unavailable.
                "smalt_dir": str(app.smalt_root),
                "smalt_status_error": str(e),
                "wiki_exists": False,
                "pages_indexed": 0,
                "embedding": {"provider": "?", "model": "?", "dim": 0},
            }

    @server.tool(name="wiki.index")
    async def wiki_index(full: bool = False) -> dict[str, Any]:
        """Submit an indexer run. Returns a `task_id`; poll `wiki.task_status`.

        Post-M2.7: proxies `smalt.reindex_all`. The returned `task_id`
        is smalt's task id (cobalt-grinding doesn't wrap it in its own
        scheduler). `wiki.task_status` knows to fall back to
        `smalt.task_status` for ids it doesn't recognize, so clients
        see uniform polling.

        The `full` arg is documented for forward-compat but smalt's
        `reindex_all` always does a full rebuild — there's no
        incremental option at smalt's surface yet.
        """
        try:
            smalt_resp = await _call_smalt(app, "reindex_all", {})
        except _SmaltUnreachable as e:
            return {"error": "smalt_unreachable", "message": str(e)}
        # smalt.reindex_all returns {task_id, kind, state, created_at, message}.
        # Pass through verbatim; clients use the task_id with wiki.task_status.
        return {
            **smalt_resp,
            "kind": smalt_resp.get("kind", "index"),
            "full": full,  # echoed for client-side logging
        }

    @server.tool(name="wiki.ingest")
    async def wiki_ingest(path: str, h_lang: str = "c") -> dict[str, Any]:
        """Ingest a path or git URL into the Smalt.

        Three input shapes, all auto-detected from the `path` arg:

        - **Single file** (text / code / config — see M3 supported
          list) → one SourcePage with summary, entities, glossary,
          and (for code files) a symbol outline via deco-assaying.
        - **Directory** → hybrid source layout: an index page + one
          section page per supported file. `.git/` and `.obsidian/`
          metadata captured if present. Unsupported files recorded
          in the source-index's `ignored:` list.
        - **Git URL** (`https://github.com/foo/bar`, `git@…`, etc.) →
          shallow-cloned into a tempdir, then run through the
          directory pipeline. The SourcePage's `location_uri` is
          set to the canonical git URL so re-ingest lookups work.

        Args:
          path: filesystem path OR git URL.
          h_lang: parse language for `.h` files. Either `"c"` or
            `"cpp"`. Default `"c"`.

        Returns:
          On success: the orchestrator's `IngestResult.to_dict()` —
            includes `source_id`, `page_path`, `location_uri`,
            section/entity/glossary page counts, ignored files,
            timestamps, and `skipped`/`skip_reason` for re-ingest
            short-circuit cases.
          On failure: `{error: 'ingest_error' | 'invalid_argument',
            message, path}`.

        The handler awaits the pipeline directly. For URL ingest the
        clone happens in a tempdir cleaned up after the response is
        sent; the user sees the response only when the full ingest
        is done.
        """
        if h_lang not in ("c", "cpp"):
            return {
                "error": "invalid_argument",
                "message": f"h_lang must be 'c' or 'cpp'; got {h_lang!r}",
            }
        try:
            result = await run_ingest(app, path, h_disambiguation=cast(Literal["c", "cpp"], h_lang))
        except IngestError as e:
            return {"error": "ingest_error", "message": str(e), "path": path}
        return result.to_dict()

    @server.tool(name="wiki.search")
    async def wiki_search(
        query: str,
        top_k: int = 10,
        expand_hops: int = 0,
        expand_label: str | None = None,
        glossary: bool | None = None,
        is_domain: bool | None = None,
        domain: str | None = None,
        has_aliases_containing: str | None = None,
        fetched_at_before: str | None = None,
        fetched_at_after: str | None = None,
    ) -> dict[str, Any]:
        """Search the wiki via hybrid retrieval (FTS + vector + alias)
        with optional 1-hop graph expansion.

        **M4 (first cut)**:
        - Direct hits come from `smalt.search` (RRF-fused).
        - `expand_hops > 0` adds 1-hop neighbors of the top hits via
          `smalt.traverse` — useful for "give me the cluster around
          the matched concept." Default 0 (no expansion).
        - `gap_detected` is set when zero direct hits return.
          External callers can choose to log the gap via
          `wiki.report_gap(query)`.

        Args:
          query: free-text search query.
          top_k: number of direct hits to return (default 10).
          expand_hops: 1-hop neighbors of top hits (default 0 = none).
            Capped at smalt's max (5).
          expand_label: optional label filter for expansion edges.
          glossary / is_domain / domain / has_aliases_containing /
          fetched_at_before / fetched_at_after: smalt.search property
          filters; pass through.

        Returns:
          On success: `{query, hits: [...], expansion_edges: [...],
            expanded_node_ids: [...], gap_detected, count}`.
          On failure: `{error: 'retrieve_error', message}`.
        """
        property_filters: dict[str, Any] = {}
        if glossary is not None:
            property_filters["glossary"] = glossary
        if is_domain is not None:
            property_filters["is_domain"] = is_domain
        if domain is not None:
            property_filters["domain"] = domain
        if has_aliases_containing is not None:
            property_filters["has_aliases_containing"] = has_aliases_containing
        if fetched_at_before is not None:
            property_filters["fetched_at_before"] = fetched_at_before
        if fetched_at_after is not None:
            property_filters["fetched_at_after"] = fetched_at_after
        try:
            result = await retrieve.search(
                app,
                query,
                top_k=top_k,
                expand_hops=expand_hops,
                expand_label=expand_label,
                property_filters=property_filters or None,
            )
        except retrieve.RetrieveError as e:
            return {"error": "retrieve_error", "message": str(e), "query": query}
        return result.to_dict()

    @server.tool(name="wiki.get_page")
    async def wiki_get_page(page_id: str, fuzzy: bool = True) -> dict[str, Any]:
        """Read one page by id. Proxies `smalt.read_page`.

        `page_id` may be a canonical id, an exact alias, or (with
        `fuzzy=True`, default) a fuzzy-matched alias. Returns the
        page's frontmatter + body + path, or `{error: 'not_found' |
        'ambiguous_alias', ...}` from smalt verbatim.
        """
        try:
            return await retrieve.get_page(app, page_id, fuzzy=fuzzy)
        except retrieve.RetrieveError as e:
            return {"error": "retrieve_error", "message": str(e), "page_id": page_id}

    @server.tool(name="wiki.traverse")
    async def wiki_traverse(
        from_id: str, hops: int = 1, label: str | None = None
    ) -> dict[str, Any]:
        """Multi-hop outgoing-link traversal. Proxies `smalt.traverse`.

        Useful for exploring the neighborhood around a known page
        (e.g. after `wiki.get_page` finds a starting point). For
        per-query graph expansion folded into a search, use
        `wiki.search(query, expand_hops=N)` instead.
        """
        try:
            return await retrieve.traverse(app, from_id, hops=hops, label=label)
        except retrieve.RetrieveError as e:
            return {"error": "retrieve_error", "message": str(e), "from_id": from_id}

    @server.tool(name="wiki.find_gaps")
    async def wiki_find_gaps() -> dict[str, Any]:
        """List open knowledge gaps from the lab notebook. Proxies
        `ebony.list_gaps`. Gaps are populated by `wiki.report_gap`
        and (in Phase 2) by Cogitate / Curate observation passes."""
        try:
            return await retrieve.list_gaps(app)
        except retrieve.RetrieveError as e:
            return {"error": "retrieve_error", "message": str(e)}

    @server.tool(name="wiki.report_gap")
    async def wiki_report_gap(
        query: str, why: str | None = None, source: str | None = None
    ) -> dict[str, Any]:
        """Record `query` as a knowledge gap in the lab notebook.
        Proxies `ebony.add_gap`. Idempotent at ebony's end.

        M6 Research will read these gaps and propose source-adoption
        proposals to fill them. M4 just exposes the entry point so
        external clients (or M5 Converse) can flag what's missing.
        """
        try:
            return await retrieve.report_gap(app, query, why=why, source=source)
        except retrieve.RetrieveError as e:
            return {"error": "retrieve_error", "message": str(e), "query": query}

    @server.tool(name="wiki.ask")
    async def wiki_ask(
        question: str,
        top_k: int = 8,
        expand_hops: int = 1,
        max_tokens: int = 2048,
        prior_messages: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Answer a natural-language question against the wiki, with
        citations and (optional) multi-turn context.

        - Retrieves top-K relevant pages (+ 1-hop graph neighbors).
        - Hydrates each via `wiki.get_page` to feed the LLM bodies.
        - Asks the LLM to answer from the provided excerpts ONLY,
          citing pages with `[page:<id>]` tokens.
        - Validates each citation against the context we provided —
          ids the LLM made up land in `invalid_citations`.
        - `gap_detected=true` if either no relevant pages were found
          OR every citation was invalid (LLM hallucinated; corpus
          didn't help).

        Args:
          question: free-text natural-language question.
          top_k: max pages used as LLM context (default 8).
          expand_hops: 1-hop graph expansion seeds (default 1; pass
            0 to disable).
          max_tokens: cap on the LLM's answer length.
          prior_messages: optional list of `{role: 'user'|'assistant',
            content: str}` for multi-turn conversations. The caller
            (typically the REPL) maintains the history; each entry's
            content can be either a prior question (without its
            excerpts) or a prior assistant answer. The LLM sees these
            before the current excerpt-augmented user turn, enabling
            "tell me more" / "what about X" style follow-ups.

        Returns:
          On success: `{question, answer, citations: [...],
            invalid_citations: [...], hits_used: [...],
            gap_detected, truncated_context}`.
          On failure: `{error: 'converse_error', message}`.
        """
        try:
            result = await converse.ask(
                app,
                question,
                top_k=top_k,
                expand_hops=expand_hops,
                max_tokens=max_tokens,
                prior_messages=prior_messages,
            )
        except converse.ConverseError as e:
            return {"error": "converse_error", "message": str(e), "question": question}
        return result.to_dict()

    @server.tool(name="wiki.task_status")
    async def wiki_task_status(task_id: str) -> dict[str, Any]:
        """Return the current state of a task by id.

        Tries cobalt-grinding's scheduler first. If the id isn't a cobalt-grinding
        task, falls back to `smalt.task_status` (post-M2.7 — wiki.index
        returns smalt task ids). Returns the task dict from whichever
        side owns it, or `{error: 'not_found'}` if neither side knows
        the id.
        """
        task = scheduler.get(task_id)
        if task is not None:
            return task.to_dict()
        # Fall back to smalt — wiki.index returns smalt task ids.
        try:
            return await _call_smalt(app, "task_status", {"task_id": task_id})
        except _SmaltUnreachable as e:
            return {
                "error": "not_found",
                "task_id": task_id,
                "smalt_status_error": str(e),
            }

    @server.tool(name="wiki.task_list")
    def wiki_task_list(kind: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        """Return recent cobalt-grinding-side tasks, optionally filtered.

        **cobalt-grinding-side only** — smalt-side tasks aren't included here.
        For smalt-side task listing, call `smalt.task_list` directly via
        an MCP client connected to smalt-mcp.
        """
        status_enum = TaskStatus(status) if status else None
        tasks = scheduler.list_tasks(kind=kind, status=status_enum)
        return [t.to_dict() for t in tasks]

    @server.tool(name="wiki.task_cancel")
    def wiki_task_cancel(task_id: str) -> dict[str, Any]:
        """Request cancellation of a cobalt-grinding-side task. Cooperative.

        **cobalt-grinding-side only** — to cancel a smalt-side task (e.g. one
        submitted via wiki.index), call `smalt.task_cancel` directly via
        an MCP client connected to smalt-mcp.
        """
        ok = scheduler.cancel(task_id)
        return {"task_id": task_id, "cancel_requested": ok}


# ---- helpers ----


class _SmaltUnreachable(RuntimeError):
    """Raised when the smalt-mcp child can't be reached for a tool call."""


async def _call_smalt(app: App, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a smalt-mcp tool and return its decoded JSON payload.

    Raises `_SmaltUnreachable` if dispatch failed (child not running,
    timeout, unknown tool, etc.). Returns the decoded payload on
    success; raises `_SmaltUnreachable` on tool-side errors too (the
    `wiki.*` proxies surface those as a structured error to clients,
    not as exceptions).
    """
    try:
        clients = app.mcp_clients
    except RuntimeError as e:
        raise _SmaltUnreachable(f"MCP supervisor not started: {e}") from e

    result: CallResult = await clients.call_tool(SMALT_CLIENT_NAME, tool_name, arguments)
    if not result.ok:
        raise _SmaltUnreachable(result.error_text or "(no error text)")
    payload = _decode_content(result.content)
    if result.is_error:
        # Tool-side error: surface as unreachable-style so the proxy can
        # render a single error path.
        msg = (
            payload.get("error", "(no error message)")
            if isinstance(payload, dict)
            else str(payload)
        )
        raise _SmaltUnreachable(f"smalt.{tool_name} returned error: {msg}")
    if not isinstance(payload, dict):
        raise _SmaltUnreachable(
            f"smalt.{tool_name} returned non-dict payload: {type(payload).__name__}"
        )
    return payload


def _decode_content(content: list[Any]) -> Any:
    """Extract + JSON-decode the first TextContent block of a CallResult.

    Mirrors the pattern in cogrind-workshop's client; smalt's tools
    return a single TextContent block with a JSON-encoded payload.
    """
    if not content:
        return None
    first = content[0]
    text = getattr(first, "text", None)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
