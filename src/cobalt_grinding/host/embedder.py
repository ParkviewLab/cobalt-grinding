# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Embedding-model abstraction — host infrastructure.

Used by the M2.5 `tools_index` for hybrid (BM25 + vector) retrieval over
MCP child tool descriptions. The wiki's own embeddings now belong to
`smalt-mcp` (the storage substrate); this embedder is *cogbgrindd's own
internal* embedder for routing decisions, not for wiki content.

Relocated from `cobalt_grinding/storage/embedder.py` in the M2.7 storage cleave
(Step 2). The class shape is unchanged — only the import path moved.

Currently only the local `fastembed` provider is implemented; hosted
providers (`voyage`, `openai`) are sketched as placeholders for later
milestones.

The `Embedder` protocol is what the rest of cobalt-grinding talks to for
embeddings, so swapping providers is a single-point change. The
`tools_index` takes an `Embedder` instance — it doesn't construct one
— so the daemon builds a single Embedder at startup and reuses it
across the lifetime of the process.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

import numpy as np

if TYPE_CHECKING:
    from cobalt_grinding.config import Config


class Embedder(Protocol):
    """The contract cobalt-grinding's tools_index talks to for embeddings."""

    @property
    def dim(self) -> int: ...

    @property
    def model_version(self) -> str:
        """A stable identifier of the model — `<provider>:<model>` — stored alongside vectors so we know what produced them."""
        ...

    def embed(self, texts: Iterable[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns one float-list per input."""
        ...


class FastembedEmbedder:
    """Local ONNX-backed embedder via the fastembed library."""

    def __init__(self, model_name: str, *, dim: int) -> None:
        # Lazy import so test environments without fastembed installed
        # don't pay the import cost just to import this module.
        from fastembed import TextEmbedding

        self._model_name = model_name
        self._dim = dim
        self._impl = TextEmbedding(model_name)

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_version(self) -> str:
        return f"fastembed:{self._model_name}"

    def embed(self, texts: Iterable[str]) -> list[list[float]]:
        # fastembed accepts any iterable and yields numpy arrays one at a
        # time. We materialize the input only once (when fastembed iterates
        # it internally), and convert each output array to a plain list so
        # downstream consumers (LanceDB, JSON, tests) don't pull in numpy.
        return [np.asarray(v, dtype=np.float32).tolist() for v in self._impl.embed(texts)]


def make_embedder(cfg: Config) -> Embedder:
    """Construct the embedder configured by `cfg.embedding`. Single source
    of truth for which provider gets used."""
    provider = cfg.embedding.provider.lower()
    if provider == "fastembed":
        return FastembedEmbedder(cfg.embedding.model, dim=cfg.embedding.dim)
    if provider in ("voyage", "openai"):
        raise NotImplementedError(
            f"embedding provider {provider!r} is supported in config schema "
            f"but not yet wired up — only `fastembed` is implemented."
        )
    raise ValueError(f"unknown embedding provider: {provider!r}")
