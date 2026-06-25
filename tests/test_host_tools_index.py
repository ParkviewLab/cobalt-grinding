# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for `cobalt_grinding.host.tools_index`.

Uses the existing FakeEmbedder + empty_wiki fixtures (see conftest.py)
so tests stay fast — no real fastembed model loaded, real LanceDB.
The hybrid retrieval path is exercised end-to-end against a small,
hand-built tools set so we can assert ordering and dedup behaviour
deterministically.
"""

from __future__ import annotations

from pathlib import Path

from cobalt_grinding.daemon.mcp_clients import ToolDescriptor
from cobalt_grinding.host import tools_index_db
from cobalt_grinding.host.tools_index import (
    TABLE_TOOLS_INDEX,
    ToolsIndex,
    _Hit,
    _rrf_fuse,
)
from tests.conftest import FakeEmbedder


def _idx(empty_wiki: Path) -> ToolsIndex:
    # M2.7: tools_index_db.connect replaces the old cobalt_grinding.storage.lance.connect.
    # `empty_wiki` here is used as a scratch dir for the tools_index LanceDB
    # store (which under the new layout lives under `cobalt_grinding_dir/tools_index/`).
    db = tools_index_db.connect(empty_wiki)
    return ToolsIndex(db, FakeEmbedder())


def _td(
    prefixed: str, *, child: str | None = None, desc: str = "tool description"
) -> ToolDescriptor:
    raw = prefixed.split(".", 1)[1] if "." in prefixed else prefixed
    return ToolDescriptor(
        prefixed_name=prefixed,
        raw_name=raw,
        description=desc,
        input_schema={"type": "object", "properties": {}},
        owning_child=child or prefixed.split(".", 1)[0],
    )


# ---- writes ----


def test_rebuild_all_creates_table_and_inserts(empty_wiki: Path) -> None:
    idx = _idx(empty_wiki)
    descriptors = [
        _td("codeparse.parse_file", desc="parse a source file"),
        _td("codeparse.supported_languages", desc="list languages"),
        _td("pdf.extract_text", desc="pull text from a pdf"),
    ]
    idx.rebuild_all(descriptors)
    assert idx.count() == 3
    # Verify the table was created in the tools_index LanceDB store.
    # `list_tables()` returns a ListTablesResponse with a `.tables` attr
    # (since lancedb>=0.30 — see the dep comment in pyproject.toml).
    db = tools_index_db.connect(empty_wiki)
    assert TABLE_TOOLS_INDEX in db.list_tables().tables


def test_rebuild_all_with_empty_input_creates_no_table(empty_wiki: Path) -> None:
    idx = _idx(empty_wiki)
    idx.rebuild_all([])
    # No table created when there's nothing to insert.
    assert idx.count() == 0


def test_rebuild_all_replaces_previous_contents(empty_wiki: Path) -> None:
    idx = _idx(empty_wiki)
    idx.rebuild_all([_td("a.first"), _td("b.second")])
    assert idx.count() == 2
    idx.rebuild_all([_td("c.third")])
    assert idx.count() == 1


def test_add_for_client_dedups_repeat_inserts(empty_wiki: Path) -> None:
    idx = _idx(empty_wiki)
    one = [_td("c.one"), _td("c.two")]
    idx.add_for_client(one)
    assert idx.count() == 2
    # Second add for same client shouldn't double-up.
    idx.add_for_client(one)
    assert idx.count() == 2


def test_remove_for_client_drops_only_that_clients_rows(empty_wiki: Path) -> None:
    idx = _idx(empty_wiki)
    idx.rebuild_all(
        [
            _td("alpha.one", child="alpha"),
            _td("alpha.two", child="alpha"),
            _td("beta.one", child="beta"),
        ]
    )
    assert idx.count() == 3
    idx.remove_for_client("alpha")
    assert idx.count() == 1


def test_remove_for_client_when_no_table_is_noop(empty_wiki: Path) -> None:
    idx = _idx(empty_wiki)
    # Table never created — remove must not raise.
    idx.remove_for_client("anything")
    assert idx.count() == 0


# ---- reads ----


def test_search_returns_relevant_results(empty_wiki: Path) -> None:
    idx = _idx(empty_wiki)
    descriptors = [
        _td("codeparse.parse_file", desc="parse Python or C source code into symbols and imports"),
        _td("pdf.extract_text", desc="extract text content from PDF documents"),
        _td("web.fetch_url", desc="fetch the body of an HTTP URL"),
    ]
    idx.rebuild_all(descriptors)

    hits = idx.search("parse python", top_k=2)
    # `parse python` should match the codeparse description by BM25;
    # vector path adds tiebreak. Top hit should be parse_file.
    assert len(hits) <= 2
    assert hits[0].prefixed_name == "codeparse.parse_file"


def test_search_top_k_limits_result_count(empty_wiki: Path) -> None:
    idx = _idx(empty_wiki)
    idx.rebuild_all([_td(f"c.tool{i}", desc=f"tool number {i}") for i in range(8)])
    assert len(idx.search("tool", top_k=3)) <= 3
    assert len(idx.search("tool", top_k=10)) <= 8


def test_search_on_empty_index_returns_empty_list(empty_wiki: Path) -> None:
    idx = _idx(empty_wiki)
    assert idx.search("anything") == []


def test_search_returns_descriptors_with_full_shape(empty_wiki: Path) -> None:
    idx = _idx(empty_wiki)
    descriptors = [_td("codeparse.parse_file", desc="parse code")]
    idx.rebuild_all(descriptors)
    hits = idx.search("code", top_k=5)
    assert len(hits) == 1
    h = hits[0]
    assert h.prefixed_name == "codeparse.parse_file"
    assert h.raw_name == "parse_file"
    assert h.owning_child == "codeparse"
    assert h.input_schema == {"type": "object", "properties": {}}


# ---- RRF fusion (pure logic) ----


def test_rrf_fuse_prefers_items_appearing_in_both_rankers() -> None:
    a = _td("client.a")
    b = _td("client.b")
    c = _td("client.c")
    bm25 = [_Hit(rank=1, descriptor=a), _Hit(rank=2, descriptor=b)]
    vector = [_Hit(rank=2, descriptor=a), _Hit(rank=1, descriptor=c)]

    fused = _rrf_fuse(bm25, vector, top_k=3)
    # `a` appears in both rankers near the top → highest fused score.
    assert fused[0].prefixed_name == "client.a"
    assert {d.prefixed_name for d in fused} == {"client.a", "client.b", "client.c"}


def test_rrf_fuse_top_k_truncates() -> None:
    descriptors = [_td(f"c.t{i}") for i in range(6)]
    bm25 = [_Hit(rank=i + 1, descriptor=d) for i, d in enumerate(descriptors)]
    fused = _rrf_fuse(bm25, top_k=3)
    assert len(fused) == 3
    # Best rank (1) wins.
    assert fused[0].prefixed_name == "c.t0"


def test_rrf_fuse_deduplicates_same_descriptor_in_one_ranker() -> None:
    a = _td("c.one")
    bm25 = [_Hit(rank=1, descriptor=a), _Hit(rank=2, descriptor=a)]
    fused = _rrf_fuse(bm25, top_k=5)
    # One result even though hit appears twice in input.
    assert len(fused) == 1
    assert fused[0].prefixed_name == "c.one"
