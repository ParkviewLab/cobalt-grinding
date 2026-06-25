# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""cobalt-grinding config loader.

Layered precedence — last wins:
  1. Built-in defaults
  2. User-global config:  ~/.config/cobalt_grinding/config.toml
  3. Per-Smalt config:    <smalt_dir>/config.toml
  4. Environment variables (COBALT_GRINDING_*)
  5. CLI flags

The Config object that callers use is the result of merging all five layers.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---- model ----


class EmbeddingConfig(BaseModel):
    provider: str = "fastembed"  # fastembed | voyage | openai
    model: str = "BAAI/bge-small-en-v1.5"
    dim: int = 384
    api_key_env: str | None = None


class LLMConfig(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-opus-4-7"
    api_key_env: str = "ANTHROPIC_API_KEY"


class MCPClientConfig(BaseModel):
    """Per-child MCP server config — one entry per `[mcp.clients.<name>]`.

    The section name becomes the default `tool_prefix` in the supervisor;
    if you set `tool_prefix` here it overrides that default. The other
    fields map straight to `cobalt_grinding.daemon.mcp_clients.ChildConfig`.
    """

    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    tool_prefix: str | None = None
    autostart: bool = True
    restart: str = "on-failure"  # on-failure | always | never
    call_timeout: float = 30.0
    startup_timeout: float = 10.0


class MCPConfig(BaseModel):
    # streamable-http is the daemon's default — that's what the CLI client
    # connects over. stdio is supported for use as a child-process MCP
    # server (e.g. spawned by Claude Desktop) and must be selected explicitly.
    transport: str = "streamable-http"  # streamable-http | stdio
    host: str = "127.0.0.1"
    port: int = 7474

    # Children cobalt-grinding spawns and supervises (M2.5+). The default ships
    # the substrate/capability children from the master plan's M0 + later
    # capability additions:
    #   - `smalt-mcp` (storage substrate — wiki.* tools proxy this child)
    #   - `ebony-enriching` (lab notebook substrate — proposals/experiments/gaps)
    #   - `deco-assaying` (stateless code-parsing capability; tree-sitter analysis)
    #   - `flint-slating` (stateless PDF-reading capability; pypdf + docling)
    # Users running cobalt-grinding without one of these on PATH can either
    # remove the entry, or set `autostart = false` to defer. Cognitive
    # systems that call a missing child get a "client not running" error.
    #
    # SMALT_DIR / EBONY_ENRICHING_DIR are NOT pre-set in the defaults —
    # `daemon/main.py` injects them at lifespan time when the user hasn't
    # set them explicitly. Keeps the *_dir → ENV mapping automatic without
    # coupling MCPClientConfig defaults to other Config fields.
    # deco-assaying and flint-slating are stateless; no env injection needed.
    clients: dict[str, MCPClientConfig] = Field(
        default_factory=lambda: {
            "smalt-mcp": MCPClientConfig(
                command="smalt-mcp",
                tool_prefix="smalt",
                autostart=True,
            ),
            "ebony-enriching": MCPClientConfig(
                command="ebony-enriching",
                tool_prefix="ebony",
                autostart=True,
            ),
            "deco-assaying": MCPClientConfig(
                command="deco-assaying",
                tool_prefix="deco",
                autostart=True,
            ),
            "flint-slating": MCPClientConfig(
                command="flint-slating",
                tool_prefix="flint",
                autostart=True,
            ),
        }
    )


class HostConfig(BaseModel):
    """Knobs for the agent runtime — `app.host.run_agent(...)`."""

    # Top-K tools the tools_index returns per agent invocation. Caller can
    # override per-call.
    tools_top_k: int = Field(default=10, ge=1, le=200)
    # Default Anthropic model. Falls back to llm.model when unset (handled
    # at construction in app.py — null here means "use llm.model").
    default_model: str | None = None
    # Iteration cap on the tool-use loop. The loop raises IterCapExceeded
    # if it can't reach end_turn within this many provider round-trips.
    max_iters: int = Field(default=20, ge=1, le=200)


class LoggingConfig(BaseModel):
    level: str = "info"  # debug | info | warn | error


class DaemonConfig(BaseModel):
    """Runtime knobs for cobalt-grinding."""

    # Worker-pool size for the task scheduler. Sized for "a few concurrent
    # ingestions on a developer laptop." Tune up on bigger machines or down
    # on resource-constrained ones (e.g. battery).
    max_workers: int = Field(default=8, ge=1, le=64)


class Config(BaseModel):
    smalt_dir: Path = Field(default_factory=lambda: Path.home() / "Documents" / "Smalt")
    # M0-master-plan: lab notebook substrate dir, passed through to the
    # `ebony-enriching` MCP child as `EBONY_ENRICHING_DIR` (mirrors how
    # `smalt_dir` flows to smalt-mcp as `SMALT_DIR`). cobalt-grinding never
    # writes here directly — ebony-enriching owns the on-disk layout.
    ebony_dir: Path = Field(default_factory=lambda: Path.home() / "Documents" / "EbonyEnriching")
    # M2.7: cobalt-grinding's own state dir (separate from smalt_dir / ebony_dir,
    # which belong to their respective MCP children's processes). Holds the
    # tools_index LanceDB store + any future cobalt-grinding-internal
    # persistent state. LanceDB is process-local; cobalt-grinding cannot
    # share the children's lance dirs. Default mirrors XDG_STATE_HOME
    # convention.
    cobalt_grinding_dir: Path = Field(
        default_factory=lambda: Path.home() / ".local" / "state" / "cobalt_grinding"
    )
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    host: HostConfig = Field(default_factory=HostConfig)


# ---- loader ----


def default_user_config_path() -> Path:
    """Return the default location of the user-global config file."""
    return Path(user_config_dir("cobalt_grinding", appauthor=False)) / "config.toml"


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Right-biased deep merge of two dicts. Lists are replaced wholesale."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_ENV_SECTIONS = ("embedding", "llm", "mcp", "logging", "daemon", "host")
_ENV_TOP_LEVEL_PATHS = ("smalt_dir", "ebony_dir", "cobalt_grinding_dir")


def _env_overrides() -> dict[str, Any]:
    """Translate COBALT_GRINDING_* env vars into the config's nested dict shape.

    Recognized forms:
      - COBALT_GRINDING_SMALT_DIR=...                   -> smalt_dir
      - COBALT_GRINDING_EBONY_DIR=...                   -> ebony_dir
      - COBALT_GRINDING_COBALT_GRINDING_DIR=...         -> cobalt_grinding_dir
      - COBALT_GRINDING_<SECTION>_<KEY>=...             -> <section>.<key>

    Where <section> is one of `embedding`, `llm`, `mcp`, `logging`, `daemon`,
    `host`.

    A `COBALT_GRINDING_*` env var that doesn't match either form is logged at
    WARNING level. Silently dropping a typo (`COBALT_GRINDING_EMBEDING_MODEL`)
    would let the user think they configured something while the daemon
    used the default — surface it instead.
    """
    out: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith("COBALT_GRINDING_"):
            continue
        rest = key.removeprefix("COBALT_GRINDING_").lower()
        if rest in _ENV_TOP_LEVEL_PATHS:
            out[rest] = value
            continue
        matched = False
        for section in _ENV_SECTIONS:
            prefix = f"{section}_"
            if rest.startswith(prefix):
                sub_key = rest.removeprefix(prefix)
                out.setdefault(section, {})[sub_key] = value
                matched = True
                break
        if not matched:
            logger.warning(
                "ignoring unknown env var %s — known sections: %s; known top-level paths: %s",
                key,
                ", ".join(_ENV_SECTIONS),
                ", ".join(f"COBALT_GRINDING_{p.upper()}" for p in _ENV_TOP_LEVEL_PATHS),
            )
    return out


def load_config(
    *,
    user_config_path: Path | None = None,
    per_smalt_config_path: Path | None = None,
    smalt_dir_override: Path | None = None,
) -> Config:
    """Load a Config by merging the layers.

    Parameters
    ----------
    user_config_path: explicit path to a user-global config; defaults to platform-default.
    per_smalt_config_path: path to a per-wiki config.toml; if None, derived from the
        merged smalt_dir after the user-global layer is applied.
    smalt_dir_override: a CLI --smalt value that overrides everything else.
    """
    # 1. defaults
    merged: dict[str, Any] = Config().model_dump(mode="python")

    # 2. user-global config
    user_path = user_config_path or default_user_config_path()
    merged = _deep_merge(merged, _read_toml(user_path))

    # 3. per-wiki config
    if per_smalt_config_path is None:
        # derive from the merged smalt_dir after layer 2
        smalt_dir_str = merged.get("smalt_dir", "")
        if smalt_dir_str:
            per_smalt_config_path = Path(str(smalt_dir_str)).expanduser() / "config.toml"
    if per_smalt_config_path is not None:
        merged = _deep_merge(merged, _read_toml(per_smalt_config_path))

    # 4. environment variables
    merged = _deep_merge(merged, _env_overrides())

    # 5. CLI flag overrides (just smalt_dir for now)
    if smalt_dir_override is not None:
        merged["smalt_dir"] = str(smalt_dir_override)

    # Path expansion — Pydantic accepts strings for Path fields, but we want ~ expanded.
    for field_name in _ENV_TOP_LEVEL_PATHS:
        value = merged.get(field_name)
        if isinstance(value, str):
            merged[field_name] = Path(value).expanduser()

    return Config.model_validate(merged)
