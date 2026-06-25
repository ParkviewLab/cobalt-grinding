# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""LanceDB connection helper for cobalt-grinding's tools_index store.

The M2.5 `tools_index` (`cobalt_grinding/host/tools_index.py`) uses LanceDB
for hybrid (BM25 + vector) retrieval over MCP child tool descriptions.
This is **cobalt-grinding-internal infrastructure** — separate from the
wiki's LanceDB store, which post-M2.7 belongs to the `smalt-mcp`
child's process.

**Why a separate dir matters**: LanceDB is process-local. Two
processes opening the same store risk corruption. cobalt-grinding cannot
share `smalt-mcp`'s lance dir under SMALT_DIR; it needs its own
under `cobalt_grinding_dir` (see `Config.cobalt_grinding_dir`, default
`~/.local/state/cobalt_grinding/`).

This module replaces the storage-side `cobalt_grinding.storage.lance.connect`
(which is going away in the M2.7 cleave) for the single tools_index
use case. The full LanceDB schema/upsert machinery from
`storage/lance.py` does NOT move here — `tools_index.py` does its own
schema construction inline.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import lancedb


# Subdirectory under `cobalt_grinding_dir` where the tools_index LanceDB
# tables live. Kept as a constant so any future helper (backup,
# wipe-and-rebuild) addresses the same path.
TOOLS_INDEX_SUBDIR = "tools_index"


def connect(cobalt_grinding_dir: Path) -> lancedb.DBConnection:
    """Open (or create) the LanceDB connection for the tools_index store.

    Creates the directory if missing. Idempotent — second call returns a
    new connection against the same dir; LanceDB handles the file-level
    consistency within a single process.
    """
    import lancedb

    target = cobalt_grinding_dir / TOOLS_INDEX_SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(target))
