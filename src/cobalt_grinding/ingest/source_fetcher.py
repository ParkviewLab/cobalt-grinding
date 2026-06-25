# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Classify a directory ingest target and capture its metadata.

For directory ingests M3 wants to know:
  - Is it a git repo? (`.git/` present)
  - Is it an Obsidian vault? (`.obsidian/` present)
  - Are both true? (a vault tracked in git — both metadata blocks attach)
  - Or just a plain directory?

Per the master plan's M3 section, the source-id depends on what the
directory is:
  - `.git/` present → `git:<origin-remote-url>` (or `dir:<path>` if
    no remote configured)
  - `.obsidian/` present → `obsidian:<path>`
  - Both → both flags set; both metadata blocks captured
  - Neither → `dir:<path>`

Git metadata is captured best-effort via `git` subprocess calls.
Missing `git` or non-zero exit codes are NOT errors here — they
result in fields being omitted from the SourcePage frontmatter.
Same for Obsidian config files (missing files → field omitted).
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SourceKind(StrEnum):
    """How a directory ingest target should be addressed."""

    DIR = "dir"
    GIT = "git"
    OBSIDIAN = "obsidian"


@dataclass(frozen=True)
class DirSource:
    """Result of classifying a directory ingest target.

    The `kind` field captures the primary classification (git takes
    precedence over obsidian when both are present, since the git
    remote URL is the more portable identifier). `obsidian` flag and
    metadata still populate when applicable.
    """

    path: Path
    kind: SourceKind
    location_uri: str
    git: dict[str, Any] = field(default_factory=dict)
    obsidian: dict[str, Any] = field(default_factory=dict)
    is_git: bool = False
    is_obsidian: bool = False


def classify_directory(path: Path) -> DirSource:
    """Inspect `path` (which must be a directory) and capture
    git/.obsidian metadata.

    Precedence: if both `.git/` and `.obsidian/` are present, `kind`
    is `git` (the more portable identifier). The obsidian metadata
    still attaches alongside.
    """
    path = path.expanduser().resolve()
    is_git = (path / ".git").is_dir()
    is_obsidian = (path / ".obsidian").is_dir()

    git_meta: dict[str, Any] = {}
    obsidian_meta: dict[str, Any] = {}

    if is_git:
        git_meta = _capture_git_metadata(path)
    if is_obsidian:
        obsidian_meta = _capture_obsidian_metadata(path)

    if is_git:
        # Prefer the origin URL as the source-id key — that's the
        # portable identifier for a git repo. Falls back to the dir
        # path if no origin remote is configured.
        origin = git_meta.get("remotes", {}).get("origin")
        if origin:
            kind = SourceKind.GIT
            location_uri = f"git:{origin}"
        else:
            kind = SourceKind.DIR
            location_uri = f"dir:{path}"
    elif is_obsidian:
        kind = SourceKind.OBSIDIAN
        location_uri = f"obsidian:{path}"
    else:
        kind = SourceKind.DIR
        location_uri = f"dir:{path}"

    return DirSource(
        path=path,
        kind=kind,
        location_uri=location_uri,
        git=git_meta,
        obsidian=obsidian_meta,
        is_git=is_git,
        is_obsidian=is_obsidian,
    )


# ---- git ----


def _capture_git_metadata(repo: Path) -> dict[str, Any]:
    """Best-effort git metadata capture. Every step is optional —
    missing `git`, non-zero exit codes, malformed output → field is
    omitted. The caller (orchestrator) decides what's required."""
    out: dict[str, Any] = {}
    remotes = _git_remotes(repo)
    if remotes:
        out["remotes"] = remotes
    head_sha = _run_git(repo, ["rev-parse", "HEAD"])
    if head_sha:
        out["head_sha"] = head_sha
    branch = _run_git(repo, ["branch", "--show-current"])
    if branch:
        out["branch"] = branch
    head_log = _git_head_commit(repo)
    if head_log:
        out["head"] = head_log
    dirty = _git_dirty(repo)
    if dirty is not None:
        out["dirty"] = dirty
    return out


def _run_git(repo: Path, args: list[str], *, timeout: float = 5.0) -> str | None:
    """Run a git command in `repo`; return stripped stdout or None on
    failure. Doesn't raise — git not installed / non-repo / non-zero
    exit are all treated as "this field is unavailable."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug("git %s failed: %s", args, e)
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _git_remotes(repo: Path) -> dict[str, str]:
    """Parse `git remote -v` into `{name: url}`. Empty dict on failure."""
    output = _run_git(repo, ["remote", "-v"])
    if not output:
        return {}
    out: dict[str, str] = {}
    for line in output.splitlines():
        # Format: "<name>\t<url> (fetch)" / "<name>\t<url> (push)" —
        # we just keep the first occurrence of each name (fetch URL).
        parts = line.split()
        if len(parts) < 2:
            continue
        name, url = parts[0], parts[1]
        out.setdefault(name, url)
    return out


def _git_head_commit(repo: Path) -> dict[str, str] | None:
    """Parse `git log -1 --format=...` into a dict. None on failure."""
    output = _run_git(
        repo,
        ["log", "-1", "--format=%H%n%an%n%ae%n%aI%n%s"],
    )
    if not output:
        return None
    lines = output.splitlines()
    if len(lines) < 5:
        return None
    return {
        "sha": lines[0],
        "author": lines[1],
        "email": lines[2],
        "date": lines[3],
        "subject": lines[4],
    }


def _git_dirty(repo: Path) -> bool | None:
    """True if `git status --porcelain` returned anything (working tree
    is dirty). None if the call failed."""
    completed: subprocess.CompletedProcess[str] | None
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=False,
            capture_output=True,
            timeout=5.0,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return bool(completed.stdout.strip())


# ---- obsidian ----


def _capture_obsidian_metadata(vault: Path) -> dict[str, Any]:
    """Best-effort Obsidian vault metadata capture. Reads a few
    standard config files; missing files → that key is omitted."""
    out: dict[str, Any] = {"vault_name": vault.name}
    config_dir = vault / ".obsidian"

    # Read core-plugins.json for the enabled-plugins list (if it exists).
    core_plugins = _read_json(config_dir / "core-plugins.json")
    if core_plugins is not None:
        out["core_plugins"] = core_plugins

    community_plugins = _read_json(config_dir / "community-plugins.json")
    if community_plugins is not None:
        out["community_plugins"] = community_plugins

    # app.json — vault settings (theme, default view, etc.). Big; only
    # capture a small subset to avoid blowing out the source frontmatter.
    app_settings = _read_json(config_dir / "app.json")
    if isinstance(app_settings, dict):
        out["app_settings"] = {
            k: app_settings[k]
            for k in ("attachmentFolderPath", "newFileLocation", "alwaysUpdateLinks")
            if k in app_settings
        }
    return out


def _read_json(path: Path) -> Any:
    """Return the parsed JSON content of `path`, or None on any
    file-not-found / parse error."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("could not read %s: %s", path, e)
        return None
