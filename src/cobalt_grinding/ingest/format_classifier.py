# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""File-type classification for the M3 ingest pipeline.

Maps a file path's extension to one of four handler categories:
- `text` — `.md`, `.rtf`, `.txt`
- `code` — `.py`, `.c`, `.cpp`, `.h` (the latter disambiguated to C or C++)
- `config` — `.json`, `.json5`, `.toml`
- `pdf` — `.pdf` (text extracted via the `flint-slating` MCP child)

Anything else returns `UNSUPPORTED`. The orchestrator decides what to do
with an unsupported file (skip silently in directory mode; hard-error if
the file was named explicitly as a single-file ingest target).

HTML / docx / epub / images / OCR are out of scope — each would need
its own MCP-child extractor. PDFs gained support when the
`flint-slating` sibling repo landed (pypdf + Docling under the hood).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal


class FileKind(StrEnum):
    TEXT = "text"
    CODE = "code"
    CONFIG = "config"
    PDF = "pdf"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class FileClassification:
    """The classifier's verdict on one file."""

    kind: FileKind
    # For CODE files, the parsing language. `.py` → "python"; `.c` → "c";
    # `.cpp` → "cpp"; `.h` → either "c" or "cpp" depending on
    # `h_disambiguation`. None for non-code files.
    language: str | None = None


# Extension → (kind, optional language) lookup. Lowercased keys; the
# classifier lowercases the input suffix before lookup.
_EXT_MAP: dict[str, tuple[FileKind, str | None]] = {
    # text
    ".md": (FileKind.TEXT, None),
    ".rtf": (FileKind.TEXT, None),
    ".txt": (FileKind.TEXT, None),
    # code
    ".py": (FileKind.CODE, "python"),
    ".c": (FileKind.CODE, "c"),
    ".cpp": (FileKind.CODE, "cpp"),
    ".cc": (FileKind.CODE, "cpp"),
    ".cxx": (FileKind.CODE, "cpp"),
    ".hpp": (FileKind.CODE, "cpp"),
    ".hh": (FileKind.CODE, "cpp"),
    # config
    ".json": (FileKind.CONFIG, None),
    ".json5": (FileKind.CONFIG, None),
    ".toml": (FileKind.CONFIG, None),
    # PDFs — text extracted via the flint-slating MCP child.
    ".pdf": (FileKind.PDF, None),
}


def classify(
    path: Path,
    *,
    h_disambiguation: Literal["c", "cpp"] = "c",
) -> FileClassification:
    """Classify one file by extension.

    `.h` is ambiguous between C and C++; the caller passes
    `h_disambiguation` to lock the choice for this run (per the plan:
    one decision per directory ingest, not per file). The orchestrator
    is responsible for deciding the value (CLI override, sibling-file
    heuristic, etc.); the classifier just applies it.
    """
    suffix = path.suffix.lower()
    if suffix == ".h":
        return FileClassification(kind=FileKind.CODE, language=h_disambiguation)
    if suffix in _EXT_MAP:
        kind, language = _EXT_MAP[suffix]
        return FileClassification(kind=kind, language=language)
    return FileClassification(kind=FileKind.UNSUPPORTED)


def is_supported(path: Path) -> bool:
    """True if the file's extension is in the M3 supported set."""
    return classify(path).kind is not FileKind.UNSUPPORTED
