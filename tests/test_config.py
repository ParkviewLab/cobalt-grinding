# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.config — layered TOML loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from cobalt_grinding.config import Config, load_config


def test_defaults_when_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the user-config path to a non-existent file in a clean tmp dir.
    monkeypatch.delenv("COBALT_GRINDING_SMALT_DIR", raising=False)
    cfg = load_config(
        user_config_path=tmp_path / "no-such-config.toml",
        per_smalt_config_path=tmp_path / "no-such-per-wiki.toml",
    )
    assert isinstance(cfg, Config)
    assert cfg.embedding.provider == "fastembed"
    assert cfg.embedding.model == "BAAI/bge-small-en-v1.5"
    assert cfg.embedding.dim == 384
    assert cfg.llm.provider == "anthropic"
    # streamable-http is the daemon's default — that's what the CLI client connects to.
    # stdio is used only as an explicit child-process MCP server (e.g. for Claude Desktop).
    assert cfg.mcp.transport == "streamable-http"
    assert cfg.mcp.host == "127.0.0.1"
    assert cfg.mcp.port == 7474


def test_user_global_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COBALT_GRINDING_SMALT_DIR", raising=False)
    user_cfg = tmp_path / "user.toml"
    user_cfg.write_text(
        """
smalt_dir = "/tmp/some-wiki"

[embedding]
provider = "voyage"
model = "voyage-3-large"
dim = 1024
api_key_env = "VOYAGE_API_KEY"
"""
    )
    cfg = load_config(
        user_config_path=user_cfg,
        per_smalt_config_path=tmp_path / "missing.toml",
    )
    assert str(cfg.smalt_dir) == "/tmp/some-wiki"
    assert cfg.embedding.provider == "voyage"
    assert cfg.embedding.model == "voyage-3-large"
    assert cfg.embedding.dim == 1024
    assert cfg.embedding.api_key_env == "VOYAGE_API_KEY"


def test_per_wiki_overrides_user_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COBALT_GRINDING_SMALT_DIR", raising=False)
    user_cfg = tmp_path / "user.toml"
    user_cfg.write_text(
        """
[embedding]
provider = "voyage"
model = "voyage-3-large"
"""
    )
    per_wiki = tmp_path / "wiki-config.toml"
    per_wiki.write_text(
        """
[embedding]
model = "voyage-3"
"""
    )
    cfg = load_config(user_config_path=user_cfg, per_smalt_config_path=per_wiki)
    assert cfg.embedding.provider == "voyage"  # from user-global
    assert cfg.embedding.model == "voyage-3"  # overridden by per-wiki


def test_env_overrides_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_cfg = tmp_path / "user.toml"
    user_cfg.write_text(
        """
[embedding]
model = "BAAI/bge-small-en-v1.5"
"""
    )
    monkeypatch.setenv("COBALT_GRINDING_EMBEDDING_MODEL", "BAAI/bge-large-en-v1.5")
    monkeypatch.setenv("COBALT_GRINDING_SMALT_DIR", str(tmp_path / "env-wiki"))
    cfg = load_config(
        user_config_path=user_cfg,
        per_smalt_config_path=tmp_path / "missing.toml",
    )
    assert cfg.embedding.model == "BAAI/bge-large-en-v1.5"
    assert cfg.smalt_dir == tmp_path / "env-wiki"


def test_cli_flag_beats_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COBALT_GRINDING_SMALT_DIR", "/tmp/from-env")
    cfg = load_config(
        user_config_path=tmp_path / "missing.toml",
        per_smalt_config_path=tmp_path / "missing-too.toml",
        smalt_dir_override=Path("/tmp/from-cli"),
    )
    assert cfg.smalt_dir == Path("/tmp/from-cli")


def test_invalid_port_fails_at_config_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-int mcp.port in TOML must be rejected by Pydantic at load time
    with a clear error — not propagate as a ValueError deep inside cobalt-grinding
    startup."""
    monkeypatch.delenv("COBALT_GRINDING_SMALT_DIR", raising=False)
    user_cfg = tmp_path / "user.toml"
    user_cfg.write_text(
        """
[mcp]
port = "not-a-port"
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_config(
            user_config_path=user_cfg,
            per_smalt_config_path=tmp_path / "missing.toml",
        )
    # Pydantic's ValidationError mentions both the field and the type.
    msg = str(exc_info.value)
    assert "port" in msg.lower()


def test_unknown_env_var_warns_instead_of_silent_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A typo'd COBALT_GRINDING_* var must emit a warning so the user notices."""
    monkeypatch.delenv("COBALT_GRINDING_SMALT_DIR", raising=False)
    monkeypatch.setenv(
        "COBALT_GRINDING_EMBEDING_MODEL", "would-be-typo"
    )  # missing 'D' on 'EMBEDDING'

    with caplog.at_level("WARNING", logger="cobalt_grinding.config"):
        load_config(
            user_config_path=tmp_path / "missing.toml",
            per_smalt_config_path=tmp_path / "missing.toml",
        )

    assert any(
        "COBALT_GRINDING_EMBEDING_MODEL" in record.getMessage()
        and "ignoring" in record.getMessage()
        for record in caplog.records
    ), "no warning for the typo'd env var"


def test_path_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COBALT_GRINDING_SMALT_DIR", raising=False)
    user_cfg = tmp_path / "user.toml"
    user_cfg.write_text('smalt_dir = "~/some/wiki"\n')
    cfg = load_config(
        user_config_path=user_cfg,
        per_smalt_config_path=tmp_path / "missing.toml",
    )
    assert "~" not in str(cfg.smalt_dir)
    assert str(cfg.smalt_dir).endswith("/some/wiki")
