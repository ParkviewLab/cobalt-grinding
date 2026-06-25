# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Walk a directory ingest target; partition files into supported /
ignored / hidden.

The orchestrator hands the resulting `DirectoryContents` to the
multi-file pipeline: supported files become section pages, ignored
files land in the source page's `ignored:` frontmatter list, and
hidden directories (`.git/`, `.obsidian/`, `node_modules/`, etc.) are
skipped wholesale.

**Skipped wholesale** (these never produce section pages or
`ignored:` entries):
  - Hidden dot-directories (`.git/`, `.obsidian/`, `.venv/`, etc.) —
    they're plumbing, not corpus content. The source-fetcher already
    captured the meaningful bits (git metadata, obsidian config).
  - Known build/cache dirs (`node_modules/`, `__pycache__/`,
    `.pytest_cache/`).
  - Anything matching `.gitignore` patterns — **deferred to a future
    milestone** (proper gitignore parsing isn't free); for now, the
    above hard-coded denylist is "good enough" for typical repos.

**Ignored within a directory** (records the filename but doesn't
process): files whose extension isn't in the M3 supported set
(`.md`/`.rtf`/`.txt`/`.py`/`.c`/`.cpp`/`.h`/`.json`/`.json5`/`.toml`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from cobalt_grinding.ingest.format_classifier import classify

logger = logging.getLogger(__name__)


# Directories never walked into. The list is hard-coded for Chunk 3;
# gitignore-aware filtering is a later improvement.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".obsidian",
        ".venv",
        "venv",
        "env",
        ".env",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "node_modules",
        "dist",
        "build",
        ".idea",
        ".vscode",
        ".DS_Store",
    }
)

# Per-ingest hard cap on the number of supported files. Beyond this we
# truncate and flag — keeps a runaway "ingest a huge monorepo" call
# from spinning up thousands of LLM calls. The orchestrator can be
# pointed at a sub-directory for the rest.
_MAX_SUPPORTED_FILES = 500


@dataclass(frozen=True)
class DirectoryContents:
    """What `walk_directory` found inside a directory ingest target.

    `supported` is the list of files the orchestrator should process
    (each becomes a section page). `ignored` is the basenames the
    source-index page should record under `ignored:`. `truncated` is
    True if the supported-files list was capped at
    `_MAX_SUPPORTED_FILES`.
    """

    root: Path
    supported: list[Path] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    truncated: bool = False


def walk_directory(
    root: Path, *, max_supported_files: int = _MAX_SUPPORTED_FILES
) -> DirectoryContents:
    """Walk `root` recursively; partition discovered files.

    Returns relative-to-root paths in `supported`; basenames-only in
    `ignored`. Skipped dirs (`.git/`, `node_modules/`, etc.) never
    surface in either list.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    supported: list[Path] = []
    ignored: list[str] = []
    truncated = False

    # Manual walk via Path.iterdir so we can prune `_SKIP_DIRS` cleanly.
    # `os.walk` is faster but the prune-on-dirnames API is clumsier; for
    # ingest, walking the file system is not the bottleneck.
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except (OSError, PermissionError) as e:
            logger.debug("skipping unreadable directory %s: %s", current, e)
            continue
        for entry in entries:
            if entry.is_symlink():
                # Skip symlinks — could be circular, or could be a
                # deliberate compose pattern (vendoring etc.). Either
                # way, don't recurse.
                continue
            if entry.is_dir():
                if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                    continue
                stack.append(entry)
                continue
            if not entry.is_file():
                continue
            cls = classify(entry)
            if cls.kind.value == "unsupported":
                ignored.append(entry.name)
                continue
            if len(supported) >= max_supported_files:
                truncated = True
                continue
            supported.append(entry)

    supported.sort(key=lambda p: p.relative_to(root).as_posix())
    return DirectoryContents(
        root=root,
        supported=supported,
        ignored=sorted(set(ignored)),
        truncated=truncated,
    )
