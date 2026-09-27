# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.__version__ — read from package metadata."""

from __future__ import annotations

from importlib.metadata import version

from cobalt_grinding import __version__


def test_version_matches_package_metadata() -> None:
    assert __version__ == version("cobalt-grinding")
