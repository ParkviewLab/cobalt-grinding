# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.ingest.structure_extractor — directory walker."""

from __future__ import annotations

from pathlib import Path

import pytest

from cobalt_grinding.ingest.structure_extractor import walk_directory


def test_walk_empty_directory(tmp_path: Path) -> None:
    contents = walk_directory(tmp_path)
    assert contents.supported == []
    assert contents.ignored == []
    assert contents.truncated is False


def test_walk_categorizes_supported_and_ignored(tmp_path: Path) -> None:
    # Supported files
    (tmp_path / "readme.md").write_text("hi", encoding="utf-8")
    (tmp_path / "script.py").write_text("pass", encoding="utf-8")
    (tmp_path / "config.toml").write_text("[x]", encoding="utf-8")
    # Ignored files (unsupported extensions)
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "binary.so").write_bytes(b"\x7fELF")
    (tmp_path / "Makefile").write_text("all:\n\techo hi", encoding="utf-8")

    contents = walk_directory(tmp_path)
    supported_names = {p.name for p in contents.supported}
    assert supported_names == {"readme.md", "script.py", "config.toml"}
    ignored_set = set(contents.ignored)
    assert "image.png" in ignored_set
    assert "binary.so" in ignored_set
    assert "Makefile" in ignored_set


def test_walk_recurses_into_subdirs(tmp_path: Path) -> None:
    sub = tmp_path / "src" / "deep"
    sub.mkdir(parents=True)
    (tmp_path / "top.md").write_text("top", encoding="utf-8")
    (sub / "deep.py").write_text("pass", encoding="utf-8")

    contents = walk_directory(tmp_path)
    rels = sorted(str(p.relative_to(tmp_path)) for p in contents.supported)
    assert "top.md" in rels
    assert "src/deep/deep.py" in rels


def test_walk_skips_hidden_dot_directories(tmp_path: Path) -> None:
    """`.git/` etc. must not produce section pages — those are plumbing."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\nrepositoryformatversion = 0\n", encoding="utf-8")
    obs_dir = tmp_path / ".obsidian"
    obs_dir.mkdir()
    (obs_dir / "core-plugins.json").write_text("[]", encoding="utf-8")
    (tmp_path / "real.md").write_text("real content", encoding="utf-8")

    contents = walk_directory(tmp_path)
    names = {p.name for p in contents.supported}
    assert names == {"real.md"}
    # And the dot-dir contents do NOT show up in `ignored` either.
    assert all(
        not n.startswith("config") and not n.startswith("core-plugins") for n in contents.ignored
    )


def test_walk_skips_known_build_dirs(tmp_path: Path) -> None:
    for skip_dir in ("node_modules", "__pycache__", ".pytest_cache", "dist", "build"):
        d = tmp_path / skip_dir
        d.mkdir()
        (d / "should-be-skipped.py").write_text("nope", encoding="utf-8")
    (tmp_path / "kept.py").write_text("yes", encoding="utf-8")

    contents = walk_directory(tmp_path)
    names = {p.name for p in contents.supported}
    assert names == {"kept.py"}


def test_walk_truncates_at_cap(tmp_path: Path) -> None:
    for i in range(15):
        (tmp_path / f"f{i}.md").write_text(f"file {i}", encoding="utf-8")
    contents = walk_directory(tmp_path, max_supported_files=10)
    assert len(contents.supported) == 10
    assert contents.truncated is True


def test_walk_ignored_basenames_are_deduped(tmp_path: Path) -> None:
    """Two .png files with the same name in different subdirs should
    appear once in `ignored` (it's a basename list, not full paths)."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "icon.png").write_bytes(b"\x89PNG")
    (sub / "icon.png").write_bytes(b"\x89PNG")
    contents = walk_directory(tmp_path)
    assert contents.ignored.count("icon.png") == 1


def test_walk_rejects_non_directory(tmp_path: Path) -> None:
    f = tmp_path / "notadir.md"
    f.write_text("file", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        walk_directory(f)


def test_walk_skips_symlinks(tmp_path: Path) -> None:
    """Symlinks could be circular or vendoring; either way, don't recurse."""
    target = tmp_path / "real.md"
    target.write_text("real", encoding="utf-8")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this filesystem")
    contents = walk_directory(tmp_path)
    names = {p.name for p in contents.supported}
    # Real file is found; symlink to it is not double-counted.
    assert "real.md" in names
    assert "link.md" not in names
