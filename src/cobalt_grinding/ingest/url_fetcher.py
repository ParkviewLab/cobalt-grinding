# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""URL → local-path materialization for the M3 ingest pipeline.

When the user runs `wiki.ingest https://github.com/foo/bar`, this
module clones the repo into a tempdir so the directory-ingest
pipeline can run against it like any local checkout. After ingest
finishes, the tempdir is cleaned up; the SourcePage's
`location_uri` is set to the original git URL (via `.git/` remote
detection by `source_fetcher.classify_directory`), so re-ingest
lookup works.

**Supported URL shapes** (anything `git clone` accepts):
- `https://github.com/owner/repo[.git]`
- `https://gitlab.com/owner/repo[.git]` (incl. nested groups)
- `git@github.com:owner/repo[.git]` (SSH)
- `ssh://git@host/repo`
- `git://host/repo`

**Clone-depth**: shallow (`--depth 1`) by default — first demo wants
the working tree, not the full history. Operators wanting deep
clones can plug in a different strategy later.

**Timeout**: 5 minutes. Large monorepos may time out; the user can
clone manually and pass the local path instead.
"""

from __future__ import annotations

import contextlib
import logging
import re
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)


_URL_PREFIXES: tuple[str, ...] = (
    "http://",
    "https://",
    "git@",
    "ssh://",
    "git://",
)


# Shallow-clone default. Deep clones are wasteful for ingest — we only
# need the working tree. If a future flow needs more git history,
# extend the API to take a `depth` arg.
_CLONE_DEPTH = 1
_CLONE_TIMEOUT_SECONDS = 300.0


class CloneError(RuntimeError):
    """Raised when `git clone` fails (or git isn't on PATH). The
    orchestrator wraps this into `IngestError`."""


def looks_like_url(path_or_url: str) -> bool:
    """Quick first-pass check: does this look like a URL we should
    clone, vs. a filesystem path? Cheap; the real validation happens
    when `git clone` runs.

    Note: `.pdf` URLs also match this — callers that want to route
    PDFs through flint-slating's url-fetch path must check
    `looks_like_pdf_url(s)` first.
    """
    if not path_or_url:
        return False
    return path_or_url.startswith(_URL_PREFIXES) or _looks_like_scp_url(path_or_url)


def looks_like_pdf_url(path_or_url: str) -> bool:
    """True if `path_or_url` looks like an HTTP(S) URL pointing at a
    PDF file (extension-based heuristic).

    Matches:
      https://example.com/paper.pdf
      https://example.com/paper.PDF
      https://example.com/dir/paper.pdf?query=string
      https://example.com/paper.pdf#fragment

    Doesn't try to be clever about Content-Type — for first cut the
    extension is enough. Users with bare URLs that serve PDFs without
    a `.pdf` extension can download manually and pass the local path.
    """
    if not looks_like_url(path_or_url):
        return False
    if not (path_or_url.startswith("http://") or path_or_url.startswith("https://")):
        return False
    # Strip query + fragment before checking the extension.
    bare = path_or_url.split("?", 1)[0].split("#", 1)[0]
    return bare.lower().endswith(".pdf")


# `git@host:path` SCP-style URLs don't have a `git@` prefix in every
# variant — some tools render them as `host:path`. We do NOT try to
# match those because they collide with absolute Windows paths. Only
# the explicit `git@` SCP form is recognized here.
_SCP_URL_RE = re.compile(r"^git@[^:]+:[^:]+$")


def _looks_like_scp_url(s: str) -> bool:
    return bool(_SCP_URL_RE.match(s))


@contextlib.contextmanager
def clone_to_tempdir(url: str) -> Iterator[Path]:
    """Clone `url` into a TemporaryDirectory; yield the path; clean
    up on exit. Raises `CloneError` on `git clone` failure (bad URL,
    auth, network, etc.) or if `git` isn't installed.

    The cloned dir has its `.git/` intact so the downstream
    `source_fetcher.classify_directory` detects it as a git source
    and sets `location_uri = git:<remote-url>` automatically.
    """
    tmp = tempfile.TemporaryDirectory(prefix="cobalt-grinding-clone-")
    try:
        target = Path(tmp.name) / "repo"
        logger.info("cloning %s → %s (depth=%d)", url, target, _CLONE_DEPTH)
        try:
            completed = subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    str(_CLONE_DEPTH),
                    url,
                    str(target),
                ],
                check=False,
                capture_output=True,
                timeout=_CLONE_TIMEOUT_SECONDS,
                text=True,
            )
        except FileNotFoundError as e:
            raise CloneError(f"`git` not on PATH; can't clone {url}: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise CloneError(
                f"`git clone {url}` timed out after {_CLONE_TIMEOUT_SECONDS:.0f}s "
                "(try cloning manually then pass the local path)"
            ) from e
        if completed.returncode != 0:
            stderr_tail = (completed.stderr or "").strip()
            raise CloneError(
                f"`git clone {url}` failed (exit {completed.returncode}): "
                f"{stderr_tail[:500] or '(no stderr)'}"
            )
        logger.info("cloned %s → %s", url, target)
        yield target
    finally:
        tmp.cleanup()
