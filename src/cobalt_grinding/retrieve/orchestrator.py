# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""M4 retrieve orchestrator — query → smalt search + optional 1-hop graph expansion.

Public surface:
  - `search(app, query, ...)`                — hybrid retrieval + optional expansion
  - `get_page(app, page_id, ...)`            — proxy smalt.read_page
  - `traverse(app, from_id, ...)`            — proxy smalt.traverse
  - `list_gaps(app, ...)`                    — proxy ebony.list_gaps
  - `report_gap(app, query, ...)`            — proxy ebony.add_gap

Each is async, raises `RetrieveError` on dispatch/tool failure, and
returns a typed dataclass (or dict) the wiki.* tool handler renders.

**M4 scope decisions:**

- Direct hits come from `smalt.search` (which is already RRF-fused
  FTS + vector + alias retrieval). cobalt-grinding doesn't re-rank.
- Graph expansion is opt-in (`expand_hops > 0`). Default off — most
  callers want just the direct hits. M5 Converse will turn expansion
  on when it needs the neighborhood around a hit to build a citation
  context.
- Gap detection is local-only by default: zero direct hits sets
  `gap_detected=true` on the response. The caller decides whether to
  emit a gap entry via `report_gap` — we deliberately do NOT
  auto-emit, since one query that didn't match is not the same as
  "the user wants this written down."
- Property filters (the smalt.search args `glossary`, `is_domain`,
  `domain`, `fetched_at_*`, `has_aliases_containing`) are passed
  through; the wiki.search tool exposes them so external clients can
  scope the search.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cobalt_grinding.app import App

logger = logging.getLogger(__name__)


SMALT_CLIENT_NAME = "smalt-mcp"
EBONY_CLIENT_NAME = "ebony-enriching"


# ---- result types ----


@dataclass(frozen=True)
class SearchHit:
    """One result row returned by smalt.search."""

    id: str
    title: str
    type: str
    score: float
    snippet: str = ""
    aliases: list[str] = field(default_factory=list)

    @classmethod
    def from_smalt_row(cls, row: dict[str, Any]) -> SearchHit:
        return cls(
            id=str(row.get("id", "")),
            title=str(row.get("title", "")),
            type=str(row.get("type", "")),
            score=float(row.get("score", 0.0)),
            snippet=str(row.get("snippet", "")),
            aliases=list(row.get("aliases") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "score": self.score,
            "snippet": self.snippet,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class ExpansionEdge:
    """One outgoing edge surfaced via the 1-hop graph expansion pass."""

    from_id: str
    to_id: str
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"from_id": self.from_id, "to_id": self.to_id, "label": self.label}


@dataclass(frozen=True)
class SearchResult:
    """What the orchestrator returns for one `search()` call."""

    query: str
    hits: list[SearchHit] = field(default_factory=list)
    expansion_edges: list[ExpansionEdge] = field(default_factory=list)
    expanded_node_ids: list[str] = field(default_factory=list)
    gap_detected: bool = False
    truncated_expansion: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "hits": [h.to_dict() for h in self.hits],
            "expansion_edges": [e.to_dict() for e in self.expansion_edges],
            "expanded_node_ids": list(self.expanded_node_ids),
            "gap_detected": self.gap_detected,
            "truncated_expansion": self.truncated_expansion,
            "count": len(self.hits),
        }


class RetrieveError(RuntimeError):
    """Raised when a retrieve operation fails unrecoverably. Tool
    handlers catch this and return a structured error payload."""


# ---- public surface ----


# Cap on how many top hits we expand from (per call). Expansion is
# O(top_hits * edges-per-node); capping keeps a single search call from
# fanning out into hundreds of traverse calls on a dense graph.
_MAX_EXPANSION_SEEDS = 3


async def search(
    app: App,
    query: str,
    *,
    top_k: int = 10,
    expand_hops: int = 0,
    expand_label: str | None = None,
    property_filters: dict[str, Any] | None = None,
) -> SearchResult:
    """Hybrid retrieval + optional 1-hop graph expansion.

    1. `smalt.search(query, top_k, ...filters...)` → direct hits.
    2. If `expand_hops > 0` and we have hits: pick the top
       `_MAX_EXPANSION_SEEDS` and call `smalt.traverse(from_id,
       hops=expand_hops, label=expand_label)` on each. Collect the
       unique `to_id`s as `expanded_node_ids`.
    3. If zero direct hits: set `gap_detected=True`. (No auto-emit;
       caller decides via `report_gap()`.)

    Returns `SearchResult`. Raises `RetrieveError` on smalt dispatch
    failure or tool-side error.
    """
    if not query or not query.strip():
        raise RetrieveError("query is required (non-whitespace)")

    smalt_args: dict[str, Any] = {"query": query, "top_k": top_k}
    if property_filters:
        smalt_args.update(property_filters)
    payload = await _call_smalt(app, "search", smalt_args)
    raw_results = payload.get("results") or []
    hits = [SearchHit.from_smalt_row(r) for r in raw_results if isinstance(r, dict)]

    expansion_edges: list[ExpansionEdge] = []
    expanded_ids: list[str] = []
    truncated_expansion = False
    if expand_hops > 0 and hits:
        seen_targets: set[str] = {h.id for h in hits}
        for hit in hits[:_MAX_EXPANSION_SEEDS]:
            traverse_args: dict[str, Any] = {"from_id": hit.id, "hops": expand_hops}
            if expand_label is not None:
                traverse_args["label"] = expand_label
            try:
                trav = await _call_smalt(app, "traverse", traverse_args)
            except RetrieveError as e:
                # Per-hit expansion failure is non-fatal; we surface
                # what we got.
                logger.warning("traverse from %s failed: %s", hit.id, e)
                continue
            if trav.get("truncated"):
                truncated_expansion = True
            for edge in trav.get("edges", []) or []:
                if not isinstance(edge, dict):
                    continue
                from_id = str(edge.get("from_id", ""))
                to_id = str(edge.get("to_id", ""))
                if not to_id:
                    continue
                label = edge.get("label")
                expansion_edges.append(ExpansionEdge(from_id=from_id, to_id=to_id, label=label))
                if to_id not in seen_targets:
                    expanded_ids.append(to_id)
                    seen_targets.add(to_id)

    return SearchResult(
        query=query,
        hits=hits,
        expansion_edges=expansion_edges,
        expanded_node_ids=expanded_ids,
        gap_detected=len(hits) == 0,
        truncated_expansion=truncated_expansion,
    )


async def get_page(app: App, page_id: str, *, fuzzy: bool = True) -> dict[str, Any]:
    """Proxy `smalt.read_page`. Returns the full payload (frontmatter +
    body + path) or a `{error: 'not_found' | 'ambiguous_alias', ...}`
    dict — the wiki.get_page tool handler passes the payload through."""
    if not page_id or not page_id.strip():
        raise RetrieveError("page_id is required")
    return await _call_smalt(app, "read_page", {"page_id": page_id, "fuzzy": fuzzy})


async def traverse(
    app: App,
    from_id: str,
    *,
    hops: int = 1,
    label: str | None = None,
) -> dict[str, Any]:
    """Proxy `smalt.traverse`. Returns the full edges + visited_nodes
    payload."""
    if not from_id or not from_id.strip():
        raise RetrieveError("from_id is required")
    arguments: dict[str, Any] = {"from_id": from_id, "hops": hops}
    if label is not None:
        arguments["label"] = label
    return await _call_smalt(app, "traverse", arguments)


async def list_gaps(app: App) -> dict[str, Any]:
    """Proxy `ebony.list_gaps`. Returns the lab notebook's open gap list."""
    return await _call_ebony(app, "list_gaps", {})


async def report_gap(
    app: App,
    query: str,
    *,
    why: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Proxy `ebony.add_gap`. Records `query` as a knowledge gap in the
    lab notebook so Research (M6) can later propose a source.

    Idempotent at the ebony side: the same query produces the same
    `gap_id`. Returns `{gap_id, position, ...}`.
    """
    if not query or not query.strip():
        raise RetrieveError("query is required (non-whitespace)")
    arguments: dict[str, Any] = {"query": query}
    if why is not None:
        arguments["why"] = why
    if source is not None:
        arguments["source"] = source
    return await _call_ebony(app, "add_gap", arguments)


# ---- internals ----


async def _call_smalt(app: App, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a smalt-mcp tool and return its decoded payload.

    Raises `RetrieveError` on dispatch or tool-side failure (the tool
    handler translates that into a structured error payload).
    """
    return await _call_child(app, SMALT_CLIENT_NAME, tool_name, arguments)


async def _call_ebony(app: App, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call an ebony-enriching tool and return its decoded payload."""
    return await _call_child(app, EBONY_CLIENT_NAME, tool_name, arguments)


async def _call_child(
    app: App, client_name: str, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    try:
        clients = app.mcp_clients
    except RuntimeError as e:
        raise RetrieveError(f"MCP supervisor not started: {e}") from e

    result = await clients.call_tool(client_name, tool_name, arguments)
    if not result.ok:
        raise RetrieveError(
            f"{client_name}.{tool_name} dispatch failed: {result.error_text or '(no error text)'}"
        )
    payload = _decode_content(result.content)
    if not isinstance(payload, dict):
        raise RetrieveError(
            f"{client_name}.{tool_name} returned non-dict payload: {type(payload).__name__}"
        )
    if result.is_error:
        msg = payload.get("message", payload.get("error", "(no error message)"))
        raise RetrieveError(f"{client_name}.{tool_name} returned error: {msg}")
    return payload


def _decode_content(content: list[Any]) -> Any:
    """Extract + JSON-decode the first TextContent block of a CallResult."""
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
