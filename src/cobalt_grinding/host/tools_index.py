# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""LanceDB-backed registry of tools the host's agents may invoke.

The host populates this index at startup (and refreshes on supervisor
events): each tool exposed by an `[mcp.clients.*]` child becomes one
row carrying `{prefixed_name, raw_name, description, input_schema,
owning_child, vector, model_version}`. Hybrid retrieval (BM25 over
description + vector similarity) picks the top-K candidates per
agent invocation.

Pattern matches the wiki-page retrieval cobalt_grinding will ship in M4 —
same LanceDB + fastembed plumbing, different table. Building it once
for tools means M4 inherits the muscle.

cobalt-grinding's own `wiki.*` MCP server-side tools are NOT indexed here:
those are served to outside clients (Claude Desktop, the cobalt_grinding CLI).
The tools index covers child capabilities the host's *own* agents may
reach for via `app.host.run_agent(...)`.

This module is sync — LanceDB's API is sync, fastembed is sync, and
the host calls `search()` from inside the loop without contention. Add
a thread-pool wrapper if it ever becomes a bottleneck.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import pyarrow as pa

from cobalt_grinding.daemon.mcp_clients import ToolDescriptor

if TYPE_CHECKING:
    import lancedb

logger = logging.getLogger(__name__)

TABLE_TOOLS_INDEX = "tools_index"
DEFAULT_TOP_K = 10
RRF_K = 60  # standard Reciprocal Rank Fusion smoothing constant


class _Embedder(Protocol):
    """Duck-type matching cobalt_grinding's existing fastembed wrapper."""

    @property
    def dim(self) -> int: ...

    @property
    def model_version(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _existing(db: lancedb.DBConnection) -> set[str]:
    """Names of tables currently present in the LanceDB connection.

    Mirrors `cobalt_grinding.storage.lance._existing_table_names` — the SDK's
    `list_tables()` returns a `ListTablesResponse` whose `.tables`
    attribute is the actual list. The shorter `db.table_names()` is
    deprecated as of lancedb >=0.30.
    """
    return {str(t) for t in db.list_tables().tables}


def tools_index_schema(dim: int) -> pa.Schema:
    return pa.schema(
        [
            pa.field("prefixed_name", pa.string(), nullable=False),
            pa.field("raw_name", pa.string(), nullable=False),
            pa.field("description", pa.string()),
            pa.field("input_schema_json", pa.string()),
            pa.field("owning_child", pa.string(), nullable=False),
            pa.field("vector", pa.list_(pa.float32(), dim)),
            pa.field("model_version", pa.string()),
        ]
    )


@dataclass(frozen=True)
class _Hit:
    rank: int
    descriptor: ToolDescriptor


class ToolsIndex:
    """LanceDB-backed tools registry with hybrid retrieval.

    State lives in one LanceDB table; the index is rebuilt incrementally
    via `add_for_client` / `remove_for_client` as the supervisor reports
    children coming up or crashing. `rebuild_all` is the bulk path used
    at host startup.
    """

    def __init__(self, db: lancedb.DBConnection, embedder: _Embedder) -> None:
        self._db = db
        self._embedder = embedder

    # ---- table management ----

    def _ensure_table(self) -> Any:
        names = _existing(self._db)
        if TABLE_TOOLS_INDEX in names:
            return self._db.open_table(TABLE_TOOLS_INDEX)
        return self._db.create_table(
            TABLE_TOOLS_INDEX,
            schema=tools_index_schema(self._embedder.dim),
        )

    def _refresh_fts(self, table: Any) -> None:
        # FTS index needs at least one row to exist; LanceDB also throws
        # if the index already exists and we don't ask to replace.
        with contextlib.suppress(Exception):  # pragma: no cover — best effort
            table.create_fts_index("description", replace=True)

    # ---- writes ----

    def rebuild_all(self, descriptors: list[ToolDescriptor]) -> None:
        """Drop everything; insert the given descriptors; refresh FTS.

        Called once at host startup once every autostart child has
        finished its handshake (or has been deemed un-handshakeable).
        """
        table = self._ensure_table()
        # Empty table by deleting all rows; LanceDB's `delete("true")` works.
        if table.count_rows() > 0:
            table.delete("true")
        if not descriptors:
            return
        self._insert(table, descriptors)
        self._refresh_fts(table)

    def add_for_client(self, descriptors: list[ToolDescriptor]) -> None:
        """Insert rows for one child's tools (called when a child reaches
        RUNNING). No-op for empty input. Idempotent in spirit: if rows
        for this client already exist they're removed first, so a child
        that flaps doesn't end up with duplicate entries.
        """
        if not descriptors:
            return
        client = descriptors[0].owning_child
        table = self._ensure_table()
        # Belt-and-braces dedup: if previous entries for this client
        # leaked through (e.g. supervisor restart without a remove call),
        # clear them before inserting the fresh set.
        self._delete_for_client(table, client)
        self._insert(table, descriptors)
        self._refresh_fts(table)

    def remove_for_client(self, client_name: str) -> None:
        """Drop every tool row owned by `client_name` (called on crash)."""
        if TABLE_TOOLS_INDEX not in _existing(self._db):
            return
        table = self._db.open_table(TABLE_TOOLS_INDEX)
        self._delete_for_client(table, client_name)

    @staticmethod
    def _delete_for_client(table: Any, client_name: str) -> None:
        # SQL string injection isn't a real risk here — `client_name`
        # comes from our own config — but quote anyway for hygiene.
        safe = client_name.replace("'", "''")
        with contextlib.suppress(Exception):  # table might be empty
            table.delete(f"owning_child = '{safe}'")

    def _insert(self, table: Any, descriptors: list[ToolDescriptor]) -> None:
        descriptions = [d.description for d in descriptors]
        vectors = self._embedder.embed(descriptions)
        rows = [
            {
                "prefixed_name": d.prefixed_name,
                "raw_name": d.raw_name,
                "description": d.description,
                "input_schema_json": json.dumps(d.input_schema),
                "owning_child": d.owning_child,
                "vector": vec,
                "model_version": self._embedder.model_version,
            }
            for d, vec in zip(descriptors, vectors, strict=True)
        ]
        table.add(rows)

    # ---- reads ----

    def count(self) -> int:
        if TABLE_TOOLS_INDEX not in _existing(self._db):
            return 0
        return int(self._db.open_table(TABLE_TOOLS_INDEX).count_rows())

    def search(self, query: str, *, top_k: int = DEFAULT_TOP_K) -> list[ToolDescriptor]:
        """Return the top-K relevant tools for `query`.

        Hybrid: BM25 over description + vector similarity over the
        description embedding, fused via RRF (k=60). Tools appearing in
        both rankers' top lists naturally rise to the top.
        """
        if TABLE_TOOLS_INDEX not in _existing(self._db):
            return []
        table = self._db.open_table(TABLE_TOOLS_INDEX)
        if table.count_rows() == 0:
            return []

        candidate_pool = max(top_k * 3, 10)
        bm25_hits = _bm25_search(table, query, candidate_pool)
        vector_hits = _vector_search(table, self._embedder, query, candidate_pool)
        return _rrf_fuse(bm25_hits, vector_hits, top_k=top_k)


# ---- helpers ----


def _bm25_search(table: Any, query: str, limit: int) -> list[_Hit]:
    """BM25 / FTS rankers. Returns ranked hits or [] on any FTS failure
    (no FTS index, empty query, etc.). Vector path can still carry the
    search."""
    try:
        arrow = table.search(query, query_type="fts").limit(limit).to_arrow()
    except Exception:
        return []
    return _rows_to_hits(arrow)


def _vector_search(table: Any, embedder: _Embedder, query: str, limit: int) -> list[_Hit]:
    embedding = embedder.embed([query])[0]
    try:
        arrow = table.search(embedding, vector_column_name="vector").limit(limit).to_arrow()
    except Exception:
        return []
    return _rows_to_hits(arrow)


def _rows_to_hits(arrow: Any) -> list[_Hit]:
    rows = arrow.to_pylist()
    hits: list[_Hit] = []
    for rank, row in enumerate(rows, start=1):
        hits.append(_Hit(rank=rank, descriptor=_row_to_descriptor(row)))
    return hits


def _row_to_descriptor(row: dict[str, Any]) -> ToolDescriptor:
    schema = row.get("input_schema_json") or "{}"
    try:
        input_schema = json.loads(schema)
    except json.JSONDecodeError:
        input_schema = {}
    return ToolDescriptor(
        prefixed_name=row["prefixed_name"],
        raw_name=row["raw_name"],
        description=row.get("description") or "",
        input_schema=input_schema,
        owning_child=row["owning_child"],
    )


def _rrf_fuse(rankers_hits: list[_Hit], *more: list[_Hit], top_k: int) -> list[ToolDescriptor]:
    """Reciprocal Rank Fusion. Combines arbitrary ranker outputs into a
    single list ranked by sum of `1 / (RRF_K + rank)`. Stable for ties
    (first-seen wins — the order rankers were passed in matters slightly).
    """
    scores: dict[str, float] = defaultdict(float)
    descriptors: dict[str, ToolDescriptor] = {}
    for hits in (rankers_hits, *more):
        for hit in hits:
            key = hit.descriptor.prefixed_name
            scores[key] += 1.0 / (RRF_K + hit.rank)
            descriptors.setdefault(key, hit.descriptor)
    ranked = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
    return [descriptors[k] for k in ranked[:top_k]]
