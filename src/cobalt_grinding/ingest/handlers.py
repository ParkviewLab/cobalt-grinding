# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-file-type handlers for the M3 ingest pipeline.

A handler reads one file and returns a `RawSection` — the structured
input to the source-page writer.

**Synchronous handlers** (text / code / config): just read the file
content + hash. Called via `handle(path, classification)`. They never
need the MCP supervisor.

**Async handler** (PDF only, so far): goes through the flint-slating
MCP child to extract text from a PDF. Called via
`handle_async(app, path, classification)`. The orchestrator picks the
right entry point based on `classification.kind`.

The sync/async split is deliberate: text/code/config don't pay the
async overhead (or the supervisor-state dependency) for no benefit.
PDF needs both because pypdf-via-MCP is fundamentally an async
operation in our architecture.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cobalt_grinding.ingest import flint_client
from cobalt_grinding.ingest.format_classifier import FileClassification, FileKind

if TYPE_CHECKING:
    from cobalt_grinding.app import App

# Bytes ceiling for in-Python content reads. Beyond this we trim — the
# raw bytes aren't the value; the LLM summary (Chunk 2) is. 1 MiB is a
# generous ceiling for the kinds of source files M3 ingests; an
# 8000-line code file is roughly 200 KiB.
_MAX_READ_BYTES = 1 * 1024 * 1024


@dataclass(frozen=True)
class RawSection:
    """One file's contribution to a source page, as returned by a handler.

    In Chunk 1 the `body` is the raw file content (truncated if huge);
    in Chunk 2 the orchestrator runs `body` through the summarizer agent
    before writing to the SourcePage. The `metadata` dict carries
    per-handler context (e.g. detected language) that downstream agents
    may want.
    """

    path: Path
    classification: FileClassification
    content_hash: str
    body: str
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def handle(path: Path, classification: FileClassification) -> RawSection:
    """Dispatch sync handlers by file kind.

    For PDF files, use `handle_async` instead — PDFs need the
    flint-slating MCP child to read.
    """
    if classification.kind is FileKind.TEXT:
        return _handle_text(path, classification)
    if classification.kind is FileKind.CODE:
        return _handle_code(path, classification)
    if classification.kind is FileKind.CONFIG:
        return _handle_config(path, classification)
    if classification.kind is FileKind.PDF:
        raise ValueError(f"PDF file requires async handler (call `handle_async`): {path}")
    raise ValueError(
        f"unsupported file kind for {path}: {classification.kind} "
        f"(orchestrator should filter unsupported files before dispatch)"
    )


async def handle_async(app: App, path: Path, classification: FileClassification) -> RawSection:
    """Dispatch handler for kinds that need MCP children (PDFs, etc.).

    Falls back to the sync `handle()` for kinds that don't need
    async — keeping a single async entry point the orchestrator can
    call without branching everywhere.
    """
    if classification.kind is FileKind.PDF:
        return await _handle_pdf(app, path, classification)
    # Other kinds — sync handlers, no app needed.
    return handle(path, classification)


# ---- per-kind handlers ----


def _handle_text(path: Path, cls: FileClassification) -> RawSection:
    body, truncated = _read_text(path)
    return RawSection(
        path=path,
        classification=cls,
        content_hash=_hash(path),
        body=body,
        truncated=truncated,
    )


def _handle_code(path: Path, cls: FileClassification) -> RawSection:
    body, truncated = _read_text(path)
    return RawSection(
        path=path,
        classification=cls,
        content_hash=_hash(path),
        body=body,
        truncated=truncated,
        metadata={"language": cls.language},
    )


def _handle_config(path: Path, cls: FileClassification) -> RawSection:
    body, truncated = _read_text(path)
    return RawSection(
        path=path,
        classification=cls,
        content_hash=_hash(path),
        body=body,
        truncated=truncated,
    )


async def _handle_pdf(app: App, path: Path, cls: FileClassification) -> RawSection:
    """Read a PDF via flint-slating's `pdf_read_text` tool.

    First-cut: plain text (pypdf), no Docling-quality Markdown. The
    body is per-page text joined with `--- page N ---` separators so
    the LLM can see page boundaries.

    If flint is unavailable, fall back to an empty body with the PDF's
    sha256 hash still computed — the page still gets written, but
    without content. The sub-agent pipeline then has nothing to
    summarize / extract from, and the section page will note "no
    content available" in its body.
    """
    content_hash = _hash(path)
    # Truncation cap — apply AFTER joining all pages so we don't drop
    # whole pages mid-stream.
    try:
        payload = await flint_client.read_text(app, path=str(path))
    except flint_client.FlintUnavailable:
        # Soft failure — the rest of the pipeline can still write a
        # SourcePage with no body. The orchestrator's downstream
        # behavior already handles "empty body" gracefully.
        return RawSection(
            path=path,
            classification=cls,
            content_hash=content_hash,
            body="",
            truncated=False,
            metadata={"flint_unavailable": True, "page_count": 0},
        )
    full_text = flint_client.join_pages(payload)
    truncated = False
    if len(full_text) > _MAX_READ_BYTES:
        full_text = full_text[:_MAX_READ_BYTES]
        truncated = True
    return RawSection(
        path=path,
        classification=cls,
        content_hash=content_hash,
        body=full_text,
        truncated=truncated,
        metadata={"page_count": payload.get("page_count")},
    )


# ---- helpers ----


def _hash(path: Path) -> str:
    """SHA-256 of the file's bytes. Stored on the SourcePage as
    `source_content_hash` so re-ingest can detect changed content."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_text(path: Path) -> tuple[str, bool]:
    """Read up to `_MAX_READ_BYTES` and decode as UTF-8 with replacement
    for malformed bytes. Returns `(text, truncated_flag)`.

    Replacement (not strict) is the right default for ingest: we'd
    rather index a file with a few '?' for invalid bytes than fail the
    whole source. RTF files are read as text here — the markup stays
    inline; Chunk 2's summarizer agent strips it implicitly.
    """
    data = path.read_bytes()
    truncated = False
    if len(data) > _MAX_READ_BYTES:
        data = data[:_MAX_READ_BYTES]
        truncated = True
    return data.decode("utf-8", errors="replace"), truncated
