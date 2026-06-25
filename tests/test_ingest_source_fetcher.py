# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.ingest.source_fetcher.

Most paths run subprocess `git` calls — we use real git inside a tmp
dir for the happy path. Some tests just verify the classifier picks
the right kind based on which dot-dirs are present, without invoking
git.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cobalt_grinding.ingest.source_fetcher import (
    SourceKind,
    classify_directory,
)

GIT_AVAILABLE = shutil.which("git") is not None


# ---- classification (no git/.obsidian required) ----


def test_plain_directory_classifies_as_dir(tmp_path: Path) -> None:
    d = tmp_path / "plain"
    d.mkdir()
    src = classify_directory(d)
    assert src.kind is SourceKind.DIR
    assert src.location_uri == f"dir:{d.resolve()}"
    assert src.is_git is False
    assert src.is_obsidian is False
    assert src.git == {}
    assert src.obsidian == {}


def test_directory_with_obsidian_only(tmp_path: Path) -> None:
    d = tmp_path / "vault"
    d.mkdir()
    (d / ".obsidian").mkdir()
    src = classify_directory(d)
    assert src.kind is SourceKind.OBSIDIAN
    assert src.location_uri == f"obsidian:{d.resolve()}"
    assert src.is_obsidian is True
    assert src.is_git is False
    assert src.obsidian.get("vault_name") == d.name


def test_obsidian_with_config_files(tmp_path: Path) -> None:
    d = tmp_path / "vault"
    d.mkdir()
    config = d / ".obsidian"
    config.mkdir()
    (config / "core-plugins.json").write_text('["backlink", "search"]', encoding="utf-8")
    (config / "app.json").write_text(
        '{"attachmentFolderPath": "attachments", "newFileLocation": "current"}',
        encoding="utf-8",
    )
    src = classify_directory(d)
    assert src.is_obsidian
    assert src.obsidian["core_plugins"] == ["backlink", "search"]
    assert src.obsidian["app_settings"]["attachmentFolderPath"] == "attachments"


def test_obsidian_with_malformed_json_skips_field(tmp_path: Path) -> None:
    """Malformed config JSON should not crash the classifier — the
    field is just omitted."""
    d = tmp_path / "broken-vault"
    d.mkdir()
    config = d / ".obsidian"
    config.mkdir()
    (config / "core-plugins.json").write_text("{not valid json}", encoding="utf-8")
    src = classify_directory(d)
    assert src.is_obsidian
    assert "core_plugins" not in src.obsidian


# ---- git (only if git is installed) ----


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not on PATH")
def test_directory_with_git_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    # No commits → head_sha is unavailable; that's fine — we still
    # classify as DIR (no origin remote, no head commit).
    src = classify_directory(repo)
    # No origin remote configured → falls back to dir.
    assert src.is_git
    # Without remotes the kind falls back to DIR.
    assert src.kind is SourceKind.DIR


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not on PATH")
def test_git_with_origin_remote_classifies_as_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/x/y.git"],
        check=True,
        capture_output=True,
    )
    src = classify_directory(repo)
    assert src.kind is SourceKind.GIT
    assert src.location_uri == "git:https://github.com/x/y.git"
    assert src.git["remotes"]["origin"] == "https://github.com/x/y.git"


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not on PATH")
def test_git_head_metadata_captured(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    # Need at least one commit for HEAD metadata.
    (repo / "README.md").write_text("hi", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "first commit"],
        check=True,
        capture_output=True,
    )
    src = classify_directory(repo)
    assert src.is_git
    assert "head_sha" in src.git
    assert src.git.get("branch") == "main"
    assert src.git.get("head", {}).get("subject") == "first commit"
    # Clean working tree.
    assert src.git.get("dirty") is False


def test_git_metadata_skipped_when_git_unavailable_monkeypatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `git` is missing from PATH at classify time, every git
    metadata field is just omitted — classification still succeeds."""
    d = tmp_path / "fakerepo"
    d.mkdir()
    (d / ".git").mkdir()  # simulate a git repo
    # Force `git` lookups to fail with FileNotFoundError.
    import cobalt_grinding.ingest.source_fetcher as sf

    monkeypatch.setattr(sf, "_run_git", lambda *a, **kw: None)
    monkeypatch.setattr(sf, "_git_dirty", lambda _: None)
    src = classify_directory(d)
    assert src.is_git
    # Without git metadata, the classifier falls back to DIR.
    assert src.kind is SourceKind.DIR
    assert src.git == {}
