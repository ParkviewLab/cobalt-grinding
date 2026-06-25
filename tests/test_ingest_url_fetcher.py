# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.ingest.url_fetcher."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cobalt_grinding.ingest.url_fetcher import (
    CloneError,
    clone_to_tempdir,
    looks_like_pdf_url,
    looks_like_url,
)

GIT_AVAILABLE = shutil.which("git") is not None


# ---- looks_like_url ----


@pytest.mark.parametrize(
    "candidate,expected",
    [
        ("https://github.com/foo/bar", True),
        ("https://github.com/foo/bar.git", True),
        ("https://gitlab.com/group/sub/repo", True),
        ("http://example.com/repo", True),
        ("git@github.com:foo/bar", True),
        ("git@github.com:foo/bar.git", True),
        ("ssh://git@example.com/repo.git", True),
        ("git://example.com/repo.git", True),
        # Filesystem paths (NOT URLs)
        ("/tmp/some/path", False),
        ("./relative/path", False),
        ("../up/path", False),
        ("readme.md", False),
        ("", False),
        # Windows absolute paths look like SCP but should NOT match
        # (we don't recognize bare `host:path` SCP form).
        ("C:/some/win/path", False),
    ],
)
def test_looks_like_url(candidate: str, expected: bool) -> None:
    assert looks_like_url(candidate) is expected


# ---- looks_like_pdf_url ----


@pytest.mark.parametrize(
    "candidate,expected",
    [
        ("https://example.com/paper.pdf", True),
        ("https://example.com/dir/paper.pdf", True),
        ("https://example.com/Paper.PDF", True),  # case-insensitive
        ("http://example.com/paper.pdf", True),  # http counts too
        ("https://example.com/paper.pdf?download=1", True),  # query stripped
        ("https://example.com/paper.pdf#page=3", True),  # fragment stripped
        # NOT PDF URLs
        ("https://github.com/foo/bar", False),
        ("https://github.com/foo/bar.git", False),
        ("https://example.com/paper", False),  # no extension
        ("https://example.com/paper.html", False),
        # Non-HTTP shapes
        ("git@github.com:foo/bar.pdf", False),  # SCP-style, not HTTP
        ("file:///tmp/x.pdf", False),  # `file://` not http(s)
        ("/local/path.pdf", False),  # filesystem path
        ("", False),
    ],
)
def test_looks_like_pdf_url(candidate: str, expected: bool) -> None:
    assert looks_like_pdf_url(candidate) is expected


def test_looks_like_pdf_url_is_subset_of_looks_like_url() -> None:
    """Every PDF URL must also be detected as a URL (the routing logic
    checks `looks_like_pdf_url` first, but `looks_like_url` is the
    superset; one shouldn't be true without the other for HTTP forms)."""
    for url in (
        "https://example.com/a.pdf",
        "http://x.org/b.PDF",
        "https://x/y/z.pdf?q=1",
    ):
        assert looks_like_pdf_url(url) is True
        assert looks_like_url(url) is True


# ---- clone_to_tempdir ----


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not on PATH")
def test_clone_to_tempdir_real_git_clone(tmp_path: Path) -> None:
    """Round-trip test using a real local git repo as the 'remote'.
    Verifies the clone produces a directory with a working `.git/`
    and the tempdir is cleaned up after the with-block exits."""
    # Create a small bare-ish source repo to clone from.
    src = tmp_path / "source-repo"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(src), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(src), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (src / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "-C", str(src), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(src), "commit", "-m", "first"],
        check=True,
        capture_output=True,
    )

    captured: list[Path] = []
    with clone_to_tempdir(str(src)) as cloned:
        captured.append(cloned)
        assert cloned.exists()
        assert (cloned / ".git").is_dir()
        assert (cloned / "README.md").read_text(encoding="utf-8") == "hello"

    # After the with-block, the tempdir is gone.
    assert not captured[0].exists()


def test_clone_to_tempdir_raises_on_bad_url() -> None:
    """An obviously bogus URL → git clone exits non-zero → CloneError."""
    # Skip if git not on path; the FileNotFoundError branch is tested separately.
    if not GIT_AVAILABLE:
        pytest.skip("git not on PATH")
    with (
        pytest.raises(CloneError, match=r"(failed|fatal|repository)"),
        clone_to_tempdir("https://github.invalid-tld/no/such/repo.git"),
    ):
        pass


def test_clone_to_tempdir_raises_when_git_missing() -> None:
    """If `git` isn't on PATH, FileNotFoundError → CloneError."""
    fake_completed = MagicMock()
    with (
        patch(
            "cobalt_grinding.ingest.url_fetcher.subprocess.run",
            side_effect=FileNotFoundError("no such file"),
        ),
        pytest.raises(CloneError, match="not on PATH"),
        clone_to_tempdir("https://example.com/repo.git"),
    ):
        pass
    _ = fake_completed  # silence unused-var


def test_clone_to_tempdir_raises_on_timeout() -> None:
    """A clone that exceeds the timeout → CloneError with a helpful
    message."""
    timeout_exc = subprocess.TimeoutExpired(cmd="git clone", timeout=300.0)
    with (
        patch("cobalt_grinding.ingest.url_fetcher.subprocess.run", side_effect=timeout_exc),
        pytest.raises(CloneError, match="timed out"),
        clone_to_tempdir("https://example.com/slow-repo.git"),
    ):
        pass


def test_clone_to_tempdir_cleans_up_on_error() -> None:
    """If the clone subprocess fails, the tempdir is still cleaned up
    (the contextmanager's finally block must run)."""
    captured_tmps: list[Path] = []

    real_tempdir = __import__("tempfile").TemporaryDirectory

    def tracking_tempdir(*args, **kwargs):  # type: ignore[no-untyped-def]
        td = real_tempdir(*args, **kwargs)
        captured_tmps.append(Path(td.name))
        return td

    completed = MagicMock()
    completed.returncode = 1
    completed.stderr = "fatal: repository not found"
    with (
        patch(
            "cobalt_grinding.ingest.url_fetcher.tempfile.TemporaryDirectory",
            tracking_tempdir,
        ),
        patch("cobalt_grinding.ingest.url_fetcher.subprocess.run", return_value=completed),
        pytest.raises(CloneError, match="repository not found"),
        clone_to_tempdir("https://example.com/missing.git"),
    ):
        pass

    # The tempdir was created and cleaned up.
    assert len(captured_tmps) == 1
    assert not captured_tmps[0].exists()
