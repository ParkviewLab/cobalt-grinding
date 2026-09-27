# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cobalt Grinding — an agentic LLM-Wiki."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("cobalt-grinding")
except PackageNotFoundError:  # editable install before first build
    __version__ = "0.0.0+local"
