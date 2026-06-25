# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Thin async helpers for the smalt-mcp MCP child.

The orchestrator and (later) other ingest passes talk to smalt through
these helpers rather than building MCP `call_tool` invocations
themselves. Three responsibilities:

  - Decode smalt's CallResult/CallTool wire shape into Python values.
  - Translate dispatch / tool-side errors into `SmaltClientError`
    (which the orchestrator surfaces as `IngestError`).
  - Keep the orchestrator at the level of "find this source / write
    this entity page / link these targets" rather than "marshal MCP
    arguments and parse content blocks."

Helpers added in Chunk 2:
  - `find_source_by_location_uri` — re-ingest detection
  - `write_source_page` (create or update)
  - `write_entity_page`
  - `write_glossary_page`
  - `add_outgoing_links` (batch via smalt.add_links)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from cobalt_grinding.app import App

logger = logging.getLogger(__name__)


SMALT_CLIENT_NAME = "smalt-mcp"


class SmaltClientError(RuntimeError):
    """Raised for any unrecoverable smalt-mcp interaction (dispatch
    failure, validation error, tool-side error). The orchestrator wraps
    these into IngestError so the wiki.ingest handler can return a
    structured payload."""


@dataclass(frozen=True)
class ExistingSource:
    """What we learned from a successful re-ingest lookup."""

    canonical_id: str
    location_uri: str
    source_content_hash: str | None


# ---- source-page operations ----


async def find_source_by_location_uri(app: App, *, location_uri: str) -> ExistingSource | None:
    """Look up an already-ingested source page by its `location_uri`.

    cobalt-grinding stores the location_uri as one of the source page's
    aliases when it first writes the page (see
    `_build_source_frontmatter` in orchestrator). `smalt.find_by_alias`
    is the natural way to find a page given that alias.

    Returns `None` if no page is indexed at that location_uri. Returns
    the most recently-updated match if multiple pages happen to share
    the alias (shouldn't happen in normal operation — the location_uri
    is unique per filesystem path — but we're defensive).

    Falls back to `None` if smalt is unreachable or the call errors —
    treating it as "no existing source found" is the right semantics
    for an ingest pipeline: a failed lookup means we re-create rather
    than skip.
    """
    arguments = {"alias": location_uri, "fuzzy": False}
    try:
        payload = await _call_smalt(app, "find_by_alias", arguments)
    except SmaltClientError as e:
        logger.warning(
            "find_by_alias(%s) failed; treating as no-existing-source: %s", location_uri, e
        )
        return None
    matches = payload.get("matches") or []
    if not matches:
        return None
    # Pick the first match (find_by_alias returns them sorted; in
    # practice there will be exactly one for our location_uri aliases).
    match = matches[0]
    canonical_id = match.get("id")
    if not canonical_id:
        return None
    # find_by_alias returns minimal metadata; we don't get the content
    # hash here. Read the page to learn its hash.
    try:
        page_payload = await _call_smalt(app, "read_page", {"page_id": canonical_id})
    except SmaltClientError as e:
        logger.warning("read_page(%s) failed during re-ingest lookup: %s", canonical_id, e)
        return ExistingSource(
            canonical_id=canonical_id, location_uri=location_uri, source_content_hash=None
        )
    frontmatter = page_payload.get("frontmatter") or {}
    return ExistingSource(
        canonical_id=canonical_id,
        location_uri=location_uri,
        source_content_hash=frontmatter.get("source_content_hash"),
    )


async def write_source_page(
    app: App,
    *,
    frontmatter: dict[str, Any],
    body: str,
    mode: Literal["create", "update"] = "create",
) -> dict[str, Any]:
    """Write a source page via `smalt.write_page`.

    Returns smalt's response dict: `{id, path, type, mode}` (and
    possibly `original_id` for create).
    """
    arguments = {"frontmatter": frontmatter, "body": body, "mode": mode}
    return await _call_smalt(app, "write_page", arguments)


async def write_section_page(
    app: App,
    *,
    section_id: str,
    parent_source_id: str,
    title: str,
    body: str,
    aliases: list[str] | None = None,
    location_uri: str | None = None,
    source_content_hash: str | None = None,
    extra_frontmatter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a section page (a SourcePage with a `<source-id>::<rel-path>`
    id and `parent_source` set to the parent source-index page).

    Section-id pattern (smalt's C-4): the `::` triggers smalt's
    "upsert" path — re-ingest overwrites the same section page rather
    than mangling the id with a new UUID suffix. That preserves the
    section's stable identity across re-ingests.
    """
    frontmatter: dict[str, Any] = {
        "id": section_id,
        "type": "source",
        "title": title,
        "aliases": list(aliases or []),
        "tags": [],
        "location_kind": "file",
        "parent_source": parent_source_id,
        "domains": [],
    }
    if location_uri:
        frontmatter["location_uri"] = location_uri
    if source_content_hash:
        frontmatter["source_content_hash"] = source_content_hash
    if extra_frontmatter:
        frontmatter.update(extra_frontmatter)
    arguments = {"frontmatter": frontmatter, "body": body, "mode": "create"}
    return await _call_smalt(app, "write_page", arguments)


# ---- entity-page operations ----


async def write_entity_page(
    app: App,
    *,
    name: str,
    aliases: list[str],
    kind: str,
    mentioned_in_source_id: str,
    snippet: str = "",
) -> dict[str, Any]:
    """Write an EntityPage for one extracted entity.

    Always uses `mode='create'` — smalt will mangle the slug with a
    UUID suffix, producing a canonical id like `<slug>__<UUID>`. If the
    same entity appears in multiple sources, this creates duplicate
    pages; Curate (M8) will dedupe them. For Chunk 2 simplicity, dedup
    is out of scope.
    """
    slug = _slugify(f"{kind}-{name}", max_len=60) or "entity"
    frontmatter: dict[str, Any] = {
        "id": slug,
        "type": "entity",
        "title": name,
        "aliases": [name, *aliases],
        "tags": [],
        "entity_kind": kind,
        "domains": [],
        "links_out": [
            {
                "target": mentioned_in_source_id,
                "label": "mentioned_in",
                "via_source": mentioned_in_source_id,
            }
        ],
    }
    body_lines = [
        f"# {name}",
        "",
        f"Entity kind: {kind}",
    ]
    if aliases:
        body_lines += ["", f"Aliases: {', '.join(aliases)}"]
    if snippet:
        body_lines += ["", "## Evidence", "", f"> {snippet}"]
    body = "\n".join(body_lines)
    return await _call_smalt(
        app, "write_page", {"frontmatter": frontmatter, "body": body, "mode": "create"}
    )


# ---- glossary (ConceptPage with glossary=True) operations ----


async def write_glossary_page(
    app: App,
    *,
    term: str,
    definition: str,
    mentioned_in_source_id: str,
    snippet: str = "",
) -> dict[str, Any]:
    """Write a ConceptPage with `glossary: True` for one glossary entry."""
    slug = _slugify(f"glossary-{term}", max_len=60) or "term"
    evidence = [{"source_id": mentioned_in_source_id, "snippet": snippet}] if snippet else []
    frontmatter: dict[str, Any] = {
        "id": slug,
        "type": "concept",
        "title": term,
        "aliases": [term],
        "tags": [],
        "glossary": True,
        "is_domain": False,
        "domains": [],
        "parents": [],
        "evidence": evidence,
        "links_out": [
            {
                "target": mentioned_in_source_id,
                "label": "defined_in",
                "via_source": mentioned_in_source_id,
            }
        ],
    }
    body = f"# {term}\n\n{definition}\n"
    return await _call_smalt(
        app, "write_page", {"frontmatter": frontmatter, "body": body, "mode": "create"}
    )


# ---- link operations ----


async def add_outgoing_links(
    app: App, *, page_id: str, targets: list[dict[str, Any]]
) -> dict[str, Any]:
    """Append outgoing links from `page_id` in one batch.

    Each entry in `targets` is `{target, label?, via_source?}`. Returns
    smalt's response dict (per-item results + count).

    No-op (returns an empty success payload) if `targets` is empty —
    the orchestrator doesn't have to filter empty lists.
    """
    if not targets:
        return {"page_id": page_id, "added": 0, "results": []}
    return await _call_smalt(app, "add_links", {"page_id": page_id, "links": targets})


# ---- internals ----


async def _call_smalt(app: App, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a smalt-mcp tool and return its decoded payload.

    Raises `SmaltClientError` on dispatch or tool-side failure. The
    orchestrator translates that into `IngestError`.
    """
    try:
        clients = app.mcp_clients
    except RuntimeError as e:
        raise SmaltClientError(f"MCP supervisor not started: {e}") from e

    result = await clients.call_tool(SMALT_CLIENT_NAME, tool_name, arguments)
    if not result.ok:
        raise SmaltClientError(
            f"smalt.{tool_name} dispatch failed: {result.error_text or '(no error text)'}"
        )
    payload = _decode_content(result.content)
    if not isinstance(payload, dict):
        raise SmaltClientError(
            f"smalt.{tool_name} returned non-dict payload: {type(payload).__name__}"
        )
    if result.is_error:
        msg = payload.get("message", payload.get("error", "(no error message)"))
        raise SmaltClientError(f"smalt.{tool_name} returned error: {msg}")
    return payload


def _decode_content(content: list[Any]) -> Any:
    """Extract + JSON-decode the first TextContent block of a CallResult."""
    if not content:
        return None
    first = content[0]
    text = getattr(first, "text", None)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


_SLUG_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")


def _slugify(value: str, *, max_len: int = 80) -> str:
    """Turn an arbitrary string into a smalt-compatible slug.

    Same pattern as orchestrator's `_make_source_id`: replace
    disallowed chars with `-`, collapse runs, strip edges, cap length.
    """
    slug = _SLUG_INVALID_CHARS.sub("-", value)
    slug = re.sub(r"-+", "-", slug).strip("-").lower()
    if not slug or not slug[0].isalnum():
        slug = f"x-{slug}".strip("-")
    return slug[:max_len]


# ---- utility re-exports ----

# Re-export so the orchestrator can `from .smalt_client import ...`
# without pulling datetime in twice.
__all__ = [
    "ExistingSource",
    "SmaltClientError",
    "add_outgoing_links",
    "find_source_by_location_uri",
    "write_entity_page",
    "write_glossary_page",
    "write_source_page",
]
