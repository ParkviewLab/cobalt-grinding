# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.ingest.format_classifier."""

from __future__ import annotations

from pathlib import Path

import pytest

from cobalt_grinding.ingest.format_classifier import (
    FileKind,
    classify,
    is_supported,
)


@pytest.mark.parametrize(
    "name,expected_kind,expected_lang",
    [
        ("notes.md", FileKind.TEXT, None),
        ("README.txt", FileKind.TEXT, None),
        ("note.rtf", FileKind.TEXT, None),
        ("script.py", FileKind.CODE, "python"),
        ("driver.c", FileKind.CODE, "c"),
        ("driver.cpp", FileKind.CODE, "cpp"),
        ("driver.cc", FileKind.CODE, "cpp"),
        ("header.hpp", FileKind.CODE, "cpp"),
        ("settings.toml", FileKind.CONFIG, None),
        ("config.json", FileKind.CONFIG, None),
        ("pyproject.toml", FileKind.CONFIG, None),
    ],
)
def test_supported_extensions_classify(
    name: str, expected_kind: FileKind, expected_lang: str | None
) -> None:
    cls = classify(Path(name))
    assert cls.kind is expected_kind
    assert cls.language == expected_lang


def test_h_disambiguation_defaults_to_c() -> None:
    cls = classify(Path("header.h"))
    assert cls.kind is FileKind.CODE
    assert cls.language == "c"


def test_h_disambiguation_can_be_overridden_to_cpp() -> None:
    cls = classify(Path("header.h"), h_disambiguation="cpp")
    assert cls.kind is FileKind.CODE
    assert cls.language == "cpp"


@pytest.mark.parametrize(
    "name",
    [
        "image.png",
        "binary.so",
        "archive.tar.gz",
        "no-extension",
        "Makefile",  # no .extension
    ],
)
def test_unsupported_extensions(name: str) -> None:
    cls = classify(Path(name))
    assert cls.kind is FileKind.UNSUPPORTED
    assert cls.language is None
    assert not is_supported(Path(name))


def test_pdf_classifies_as_pdf() -> None:
    """PDFs gained support when the flint-slating sibling MCP server
    landed. Text comes from `flint.pdf_read_text` (per the PDF
    handler), not from raw bytes."""
    cls = classify(Path("doc.pdf"))
    assert cls.kind is FileKind.PDF
    assert cls.language is None
    assert is_supported(Path("doc.pdf"))


def test_extension_is_case_insensitive() -> None:
    """File systems are case-insensitive on macOS / Windows; we don't
    want a `.MD` file to fail classification just because the case
    differs from our lowercase map."""
    cls = classify(Path("NOTES.MD"))
    assert cls.kind is FileKind.TEXT


def test_is_supported_helper_matches_classify() -> None:
    assert is_supported(Path("foo.py")) is True
    assert is_supported(Path("foo.unknown")) is False
