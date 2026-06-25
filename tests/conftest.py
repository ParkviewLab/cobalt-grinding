# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared pytest fixtures."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

import pytest


class FakeEmbedder:
    """Deterministic, fast, no model load.

    Produces a fixed-dim vector keyed off a hash of the input text — same
    text → same vector — so tests can assert reproducibility without paying
    fastembed's import + model-download cost.
    """

    def __init__(self, dim: int = 384, model_version: str = "fake:test") -> None:
        self._dim = dim
        self._model_version = model_version

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_version(self) -> str:
        return self._model_version

    def embed(self, texts: Iterable[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            # Stretch the 32-byte digest into a `dim`-length float vector
            # by tiling and normalising into [-1, 1].
            vec = [(digest[i % len(digest)] - 128) / 128.0 for i in range(self._dim)]
            out.append(vec)
        return out


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def empty_wiki(tmp_path: Path) -> Path:
    """An empty scratch dir tests can treat as either a wiki root OR a
    cobalt_grinding state dir, depending on what they need.

    Pre-M2.7 this called the in-process bootstrap (which created
    canonical wiki dirs + LanceDB tables). That bootstrap is gone (it's
    now smalt-mcp's responsibility, called via MCP). The fixture is
    kept as a simple `mkdir` because the few tests that still use it
    only need a writable directory path — they don't need the full
    bootstrap shape.

    Tests that need a fully-bootstrapped wiki must spawn a real
    smalt-mcp child via the MCP supervisor (see
    `tests/test_daemon_integration.py` for the pattern).
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    return wiki


def write_page(smalt_root: Path, rel_path: str, content: str) -> Path:
    """Write a markdown page under the wiki and return its path."""
    target = smalt_root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
