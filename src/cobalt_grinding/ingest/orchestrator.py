# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""M3 ingest orchestrator — the pipeline driver.

Entry point: `await ingest(app, path)`. Dispatches to single-file or
directory ingest based on `path`.

**Chunk 1 scope (PR #9)**: single-file ingest skeleton — classify,
hash, write a placeholder SourcePage.

**Chunk 2 additions (PR #10)**:
- Re-ingest detection via `smalt.find_by_alias`.
- Sub-agent extraction (summarizer / entity_extractor /
  glossary_extractor) over `app.host.provider.complete()`.
- EntityPages + glossary ConceptPages + `mentions`/`defines` links.
- Graceful degradation when the LLM host isn't available.

**Chunk 3 additions (this PR)**:
- Directory ingest with hybrid source-page layout (source-index page
  + per-file section pages with `<source-id>::<rel-path>` ids).
- Git / Obsidian metadata capture via `source_fetcher`.
- Section pages run the same sub-agent pipeline as single-file ingest,
  producing per-section entity / glossary pages.
- Source-overview synthesis from section summaries (one extra LLM
  call at the source level).
- Source-index → section page `contains` links.
- Hard cap of 500 supported files per ingest (configurable via
  `structure_extractor`); truncation flagged in the result.
- Unsupported files in the directory are recorded in the source-index
  page's `ignored` frontmatter list.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from cobalt_grinding.ingest import agents, deco_client, flint_client, url_fetcher
from cobalt_grinding.ingest.format_classifier import FileClassification, FileKind, classify
from cobalt_grinding.ingest.handlers import RawSection, handle_async
from cobalt_grinding.ingest.smalt_client import (
    ExistingSource,
    SmaltClientError,
    add_outgoing_links,
    find_source_by_location_uri,
    write_entity_page,
    write_glossary_page,
    write_section_page,
    write_source_page,
)
from cobalt_grinding.ingest.source_fetcher import DirSource, classify_directory
from cobalt_grinding.ingest.structure_extractor import (
    DirectoryContents,
    walk_directory,
)

if TYPE_CHECKING:
    from cobalt_grinding.app import App
    from cobalt_grinding.host.provider import AnthropicProvider

logger = logging.getLogger(__name__)


SMALT_CLIENT_NAME = "smalt-mcp"


@dataclass(frozen=True)
class IngestResult:
    """What the orchestrator returns to the wiki.ingest tool handler."""

    source_id: str
    source_path: str
    page_path: str
    location_uri: str
    location_kind: str
    content_hash: str
    pages_written: list[str] = field(default_factory=list)
    section_pages_written: list[str] = field(default_factory=list)
    entity_pages_written: list[str] = field(default_factory=list)
    glossary_pages_written: list[str] = field(default_factory=list)
    links_added: int = 0
    files_ingested: int = 1
    files_ignored: list[str] = field(default_factory=list)
    truncated: bool = False
    started_at: str = ""
    finished_at: str = ""
    skipped: bool = False
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_path": self.source_path,
            "page_path": self.page_path,
            "location_uri": self.location_uri,
            "location_kind": self.location_kind,
            "content_hash": self.content_hash,
            "pages_written": list(self.pages_written),
            "section_pages_written": list(self.section_pages_written),
            "entity_pages_written": list(self.entity_pages_written),
            "glossary_pages_written": list(self.glossary_pages_written),
            "links_added": self.links_added,
            "files_ingested": self.files_ingested,
            "files_ignored": list(self.files_ignored),
            "truncated": self.truncated,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }


class IngestError(RuntimeError):
    """Raised for any unrecoverable ingest failure (path not found,
    file type not supported, smalt-mcp unreachable, smalt rejected the
    write). The wiki.ingest tool handler catches this and returns a
    structured error payload."""


async def ingest(
    app: App,
    path: str | Path,
    *,
    h_disambiguation: Literal["c", "cpp"] = "c",
    force: bool = False,
) -> IngestResult:
    """Ingest one path or URL. Dispatches to:

    - PDF URL (`https://…/paper.pdf`) → `flint.pdf_read_text` over
      the URL; written as a single SourcePage with
      `location_uri=url:<url>`. Checked BEFORE the git-URL route
      because `.pdf` URLs match both detectors.
    - Other URL (https://github.com/foo/bar, git@…) → `git clone`
      into a tempdir, then run the directory pipeline. The cloned
      `.git/` lets `source_fetcher.classify_directory` capture the
      remote URL as the SourcePage's `location_uri`; the tempdir is
      deleted after ingest finishes.
    - File path → single-file pipeline.
    - Directory path → multi-file directory pipeline.

    Args:
      path: filesystem path, git URL, or PDF URL.
      h_disambiguation: `.h` parse language (`c` or `cpp`).
      force: skip re-ingest detection; always write a fresh page.

    Raises:
      `IngestError` for: path missing, unsupported file type, clone
      failure (URL path), flint failure (PDF URL path), smalt
      dispatch failure, or smalt rejecting the source-page write.
    """
    started_at = datetime.now(UTC)
    path_str = str(path)

    if url_fetcher.looks_like_pdf_url(path_str):
        return await _ingest_pdf_url(app, path_str, force=force, started_at=started_at)

    if url_fetcher.looks_like_url(path_str):
        try:
            with url_fetcher.clone_to_tempdir(path_str) as cloned:
                return await _ingest_directory(
                    app,
                    cloned,
                    h_disambiguation=h_disambiguation,
                    force=force,
                    started_at=started_at,
                )
        except url_fetcher.CloneError as e:
            raise IngestError(str(e)) from e

    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise IngestError(f"path does not exist: {resolved}")
    if resolved.is_file():
        return await _ingest_single_file(
            app, resolved, h_disambiguation=h_disambiguation, force=force, started_at=started_at
        )
    if resolved.is_dir():
        return await _ingest_directory(
            app, resolved, h_disambiguation=h_disambiguation, force=force, started_at=started_at
        )
    raise IngestError(f"path is neither a file nor a directory: {resolved}")


# ---- single-file pipeline ----


async def _ingest_single_file(
    app: App,
    resolved: Path,
    *,
    h_disambiguation: Literal["c", "cpp"],
    force: bool,
    started_at: datetime,
) -> IngestResult:
    """Process one file: classify, hash, sub-agents, write SourcePage +
    entity / glossary pages, link them. Body of the source page is the
    summarizer output + raw content for traceability."""
    classification = classify(resolved, h_disambiguation=h_disambiguation)
    if classification.kind is FileKind.UNSUPPORTED:
        raise IngestError(
            f"file type not supported: {resolved.suffix!r} (supported: text / code / config / pdf)"
        )

    section = await handle_async(app, resolved, classification)
    location_uri = f"file:{resolved}"

    existing: ExistingSource | None = None
    if not force:
        try:
            existing = await find_source_by_location_uri(app, location_uri=location_uri)
        except SmaltClientError as e:
            raise IngestError(str(e)) from e
        if existing and existing.source_content_hash == section.content_hash:
            finished_at = datetime.now(UTC)
            return IngestResult(
                source_id=existing.canonical_id,
                source_path=str(resolved),
                page_path="",
                location_uri=location_uri,
                location_kind="file",
                content_hash=section.content_hash,
                pages_written=[],
                files_ingested=1,
                started_at=started_at.isoformat(),
                finished_at=finished_at.isoformat(),
                skipped=True,
                skip_reason="content_hash_matches_existing",
            )

    summary, entities, glossary = await _run_sub_agents(app, section, resolved)
    symbol_outline = await _get_symbol_outline_or_none(app, section)

    body = _build_source_body(section, summary, symbol_outline=symbol_outline)
    frontmatter = _build_source_frontmatter(
        source_id=_make_source_id(resolved),
        section=section,
        resolved_path=resolved,
        started_at=started_at,
    )
    try:
        write_result = await write_source_page(
            app, frontmatter=frontmatter, body=body, mode="create"
        )
    except SmaltClientError as e:
        raise IngestError(str(e)) from e
    canonical_id = write_result.get("id", frontmatter["id"])
    page_path = write_result.get("path", "")

    entity_page_ids = await _write_entity_pages(app, entities, parent_id=canonical_id)
    glossary_page_ids = await _write_glossary_pages(app, glossary, parent_id=canonical_id)
    links_added_count = await _link_source_to_extracted(
        app, source_page_id=canonical_id, entities=entity_page_ids, glossary=glossary_page_ids
    )

    finished_at = datetime.now(UTC)
    return IngestResult(
        source_id=canonical_id,
        source_path=str(resolved),
        page_path=page_path,
        location_uri=location_uri,
        location_kind="file",
        content_hash=section.content_hash,
        pages_written=[canonical_id],
        entity_pages_written=entity_page_ids,
        glossary_pages_written=glossary_page_ids,
        links_added=links_added_count,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
    )


# ---- PDF-URL pipeline ----


async def _ingest_pdf_url(
    app: App,
    url: str,
    *,
    force: bool,
    started_at: datetime,
) -> IngestResult:
    """Ingest a remote PDF via `flint.pdf_read_text` (URL source).

    Mirrors `_ingest_single_file`'s shape but skips the local
    classify/handle steps — flint streams the PDF from the URL, and
    we use its `pdf_info` for sha256 / page_count. The SourcePage's
    `location_uri` is `url:<url>`; `location_kind` is `url`.
    """
    # Get sha256 + page count up front. Lets re-ingest detection work
    # via the `find_by_alias` lookup against `url:<url>` plus the
    # content_hash compare.
    try:
        info = await flint_client.pdf_info(app, url=url)
    except flint_client.FlintUnavailable as e:
        raise IngestError(str(e)) from e
    content_hash = info.get("sha256") or ""
    page_count = info.get("page_count")

    location_uri = f"url:{url}"

    # Re-ingest detection: same shape as single-file ingest. If the
    # remote PDF's sha256 matches what we previously stored, skip.
    existing: ExistingSource | None = None
    if not force:
        try:
            existing = await find_source_by_location_uri(app, location_uri=location_uri)
        except SmaltClientError as e:
            raise IngestError(str(e)) from e
        if existing and content_hash and existing.source_content_hash == content_hash:
            finished_at = datetime.now(UTC)
            return IngestResult(
                source_id=existing.canonical_id,
                source_path=url,
                page_path="",
                location_uri=location_uri,
                location_kind="url",
                content_hash=content_hash,
                pages_written=[],
                files_ingested=1,
                started_at=started_at.isoformat(),
                finished_at=finished_at.isoformat(),
                skipped=True,
                skip_reason="content_hash_matches_existing",
            )

    # Fetch the text. flint hits its content-addressed cache from the
    # pdf_info call above — same URL → same blob → no redownload.
    try:
        payload = await flint_client.read_text_url(app, url=url)
    except flint_client.FlintUnavailable as e:
        raise IngestError(str(e)) from e
    body_text = flint_client.join_pages(payload)

    # Build a RawSection-equivalent so `_run_sub_agents` and the body
    # builder can reuse the existing summarizer / extractor flow. The
    # synthetic Path is just a label (`_run_sub_agents` uses it as
    # `source_label` for the agent prompts) — never opened from disk.
    # Classification is pinned to PDF so the body builder uses the
    # right fenced-block language and the deco-symbols-outline path
    # stays off (it only fires for code files).
    synthetic_path = Path(url)
    classification = FileClassification(kind=FileKind.PDF, language=None)

    section = RawSection(
        path=synthetic_path,
        classification=classification,
        content_hash=content_hash,
        body=body_text,
        truncated=False,
        metadata={"page_count": page_count, "fetched_from_url": True},
    )

    summary, entities, glossary = await _run_sub_agents(app, section, synthetic_path)

    frontmatter = _build_pdf_url_frontmatter(
        url=url,
        section=section,
        info=info,
        started_at=started_at,
    )
    body = _build_source_body(section, summary)

    try:
        write_result = await write_source_page(
            app, frontmatter=frontmatter, body=body, mode="create"
        )
    except SmaltClientError as e:
        raise IngestError(str(e)) from e
    canonical_id = write_result.get("id", frontmatter["id"])
    page_path = write_result.get("path", "")

    entity_page_ids = await _write_entity_pages(app, entities, parent_id=canonical_id)
    glossary_page_ids = await _write_glossary_pages(app, glossary, parent_id=canonical_id)
    links_added_count = await _link_source_to_extracted(
        app,
        source_page_id=canonical_id,
        entities=entity_page_ids,
        glossary=glossary_page_ids,
    )

    finished_at = datetime.now(UTC)
    return IngestResult(
        source_id=canonical_id,
        source_path=url,
        page_path=page_path,
        location_uri=location_uri,
        location_kind="url",
        content_hash=content_hash,
        pages_written=[canonical_id],
        entity_pages_written=entity_page_ids,
        glossary_pages_written=glossary_page_ids,
        links_added=links_added_count,
        files_ingested=1,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
    )


def _build_pdf_url_frontmatter(
    *,
    url: str,
    section: RawSection,
    info: dict[str, Any],
    started_at: datetime,
) -> dict[str, Any]:
    """SourcePage frontmatter for a PDF-URL ingest."""
    title = info.get("metadata", {}).get("title") or _filename_from_url(url) or url
    # Slug from a meaningful tail of the URL — strip query/fragment.
    bare = url.split("?", 1)[0].split("#", 1)[0]
    slug = _make_url_pdf_id(bare)
    fm: dict[str, Any] = {
        "id": slug,
        "type": "source",
        "title": str(title),
        "aliases": [f"url:{url}"],
        "tags": [],
        "location_uri": f"url:{url}",
        "location_kind": "url",
        "source_content_hash": section.content_hash,
        "fetched_at": started_at.isoformat(),
        "domains": [],
    }
    # PDF metadata, if flint surfaced any.
    pdf_meta = info.get("metadata") or {}
    if isinstance(pdf_meta, dict):
        for k in ("author", "subject", "creator", "producer", "created", "modified"):
            v = pdf_meta.get(k)
            if v:
                fm[f"pdf_{k}"] = v
    if section.metadata.get("page_count"):
        fm["pdf_page_count"] = section.metadata["page_count"]
    return fm


def _filename_from_url(url: str) -> str | None:
    """Pull the trailing path component out of a URL: `https://x.com/dir/y.pdf` → `y.pdf`."""
    bare = url.split("?", 1)[0].split("#", 1)[0]
    tail = bare.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


def _make_url_pdf_id(url_no_query: str) -> str:
    """Derive a smalt-compatible slug from a PDF URL.

    Drops the protocol, replaces non-alphanumerics with `-`, caps
    length. Smalt's mode='create' will add a UUID suffix for
    structural uniqueness.
    """
    after_proto = url_no_query.split("://", 1)[-1]
    slug = f"url-{after_proto}"
    slug = _SLUG_INVALID_CHARS.sub("-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-").lower()
    if not slug or not slug[0].isalnum():
        slug = f"url-{slug or 'unnamed'}"
    return slug[:80]


# ---- directory pipeline (Chunk 3) ----


async def _ingest_directory(
    app: App,
    resolved: Path,
    *,
    h_disambiguation: Literal["c", "cpp"],
    force: bool,
    started_at: datetime,
) -> IngestResult:
    """Process a directory: classify as git / obsidian / dir; walk
    supported files; each becomes a section page with sub-agent
    extraction; the directory itself becomes a source-index page with
    a synthesized overview, TOC, and ignored-files list.

    Re-ingest detection at the directory level uses the source-index's
    `location_uri` alias. Section-level re-ingest detection is deferred
    — sections are re-written each run (smalt's section-id upsert path
    handles this cleanly; per-section hash-skip is a future
    optimization).
    """
    dir_source = classify_directory(resolved)
    contents = walk_directory(resolved)
    location_uri = dir_source.location_uri
    location_kind = dir_source.kind.value

    # Re-ingest detection at the directory level. The `location_uri`
    # for a directory doesn't have a content hash (the dir doesn't
    # *have* a hash — its files do). Skip-on-hash-match doesn't make
    # sense at the dir level, but we still look up an existing
    # source-index id so re-ingests update the same index page rather
    # than creating new ones via mangling. (Smalt's section-id upsert
    # handles section pages; the source-index uses mangling — for
    # Chunk 3 we accept that re-ingest creates new source-index pages
    # and leaves the old one as an orphan. Curate / M8 dedupes.)
    if not force:
        try:
            await find_source_by_location_uri(app, location_uri=location_uri)
        except SmaltClientError as e:
            raise IngestError(str(e)) from e

    # Process each supported file as a section. We do this BEFORE
    # writing the source-index page so the index body can incorporate
    # the section summaries.
    section_records: list[_SectionRecord] = []
    entity_page_ids: list[str] = []
    glossary_page_ids: list[str] = []
    links_added_count = 0

    source_index_slug = _make_dir_source_id(dir_source)
    # We use a STABLE slug for the section pages' parent reference. The
    # source-index page's canonical id (with mangling) is only known
    # AFTER write_page returns; section ids reference the slug-prefix
    # we used in the create call — but the canonical id is what
    # references will actually need. We resolve this by writing the
    # source-index FIRST, then sections; sections reference the
    # source-index's canonical id.
    #
    # The section-id pattern `<source-id>::<rel-path>` requires the
    # `<source-id>` part to be the canonical source page id (since
    # readers will follow `parent_source` back to it). So:
    #   1. Write a placeholder source-index page first (so we get its
    #      canonical id).
    #   2. Process each section file using that canonical id as the
    #      section-id prefix.
    #   3. Update the source-index body afterwards once we have all
    #      section summaries.
    #
    # That "update afterwards" step is awkward — smalt's `mode='update'`
    # requires the canonical id (which we have) but the body has to
    # be rewritten on the same page. For Chunk 3 we do an "update"
    # pass at the end.
    placeholder_body = _build_index_placeholder_body(dir_source, contents)
    index_fm = _build_source_index_frontmatter(
        source_id=source_index_slug,
        dir_source=dir_source,
        contents=contents,
        started_at=started_at,
    )
    try:
        index_write = await write_source_page(
            app, frontmatter=index_fm, body=placeholder_body, mode="create"
        )
    except SmaltClientError as e:
        raise IngestError(str(e)) from e
    index_canonical_id = index_write.get("id", source_index_slug)
    index_path = index_write.get("path", "")

    # Process each supported file as a section.
    for file_path in contents.supported:
        rel_path = file_path.relative_to(resolved)
        try:
            section_record = await _ingest_one_section(
                app,
                file_path=file_path,
                rel_path=rel_path,
                parent_source_id=index_canonical_id,
                h_disambiguation=h_disambiguation,
            )
        except SmaltClientError:
            logger.exception("section ingest failed for %s; skipping", rel_path)
            continue
        section_records.append(section_record)
        entity_page_ids.extend(section_record.entity_page_ids)
        glossary_page_ids.extend(section_record.glossary_page_ids)
        links_added_count += section_record.links_added

    # Synthesize the source overview from section summaries.
    overview = await _synthesize_overview(app, section_records, resolved)
    final_body = _build_source_index_body(
        dir_source=dir_source,
        contents=contents,
        section_records=section_records,
        overview=overview,
    )
    # Re-write the source-index page with the synthesized body.
    update_fm = dict(index_fm)
    update_fm["id"] = index_canonical_id
    try:
        await write_source_page(app, frontmatter=update_fm, body=final_body, mode="update")
    except SmaltClientError:
        logger.exception(
            "failed to update source-index body for %s; placeholder body remains",
            index_canonical_id,
        )

    # Link source-index → each section page (`contains` label).
    contains_targets = [{"target": rec.section_id, "label": "contains"} for rec in section_records]
    if contains_targets:
        try:
            link_result = await add_outgoing_links(
                app, page_id=index_canonical_id, targets=contains_targets
            )
            results = link_result.get("results") or []
            links_added_count += sum(1 for r in results if isinstance(r, dict) and r.get("added"))
        except SmaltClientError:
            logger.exception(
                "failed to add contains links from %s to %d sections",
                index_canonical_id,
                len(contains_targets),
            )

    finished_at = datetime.now(UTC)
    return IngestResult(
        source_id=index_canonical_id,
        source_path=str(resolved),
        page_path=index_path,
        location_uri=location_uri,
        location_kind=location_kind,
        content_hash="",  # directories have no content hash; sections do
        pages_written=[index_canonical_id],
        section_pages_written=[rec.section_id for rec in section_records],
        entity_pages_written=entity_page_ids,
        glossary_pages_written=glossary_page_ids,
        links_added=links_added_count,
        files_ingested=len(section_records),
        files_ignored=list(contents.ignored),
        truncated=contents.truncated,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
    )


@dataclass
class _SectionRecord:
    """Per-section bookkeeping during a directory ingest."""

    rel_path: str
    section_id: str
    summary: str
    entity_page_ids: list[str]
    glossary_page_ids: list[str]
    links_added: int
    content_hash: str


async def _ingest_one_section(
    app: App,
    *,
    file_path: Path,
    rel_path: Path,
    parent_source_id: str,
    h_disambiguation: Literal["c", "cpp"],
) -> _SectionRecord:
    """Process one file as a section of a multi-file source.

    Section page id: `<parent_source_id>::<rel-path-slugified>` —
    smalt's section-id pattern (the `::` triggers the upsert path so
    re-ingests overwrite the section cleanly).
    """
    classification = classify(file_path, h_disambiguation=h_disambiguation)
    if classification.kind is FileKind.UNSUPPORTED:
        # Shouldn't happen — walk_directory filters these out.
        raise SmaltClientError(f"unsupported file made it through structure_extractor: {file_path}")

    section = await handle_async(app, file_path, classification)
    rel_str = rel_path.as_posix()
    section_id = _make_section_id(parent_source_id, rel_str)

    summary, entities, glossary = await _run_sub_agents(app, section, file_path)
    symbol_outline = await _get_symbol_outline_or_none(app, section)
    body = _build_section_body(section, summary, rel_str, symbol_outline=symbol_outline)

    location_uri = f"file:{file_path}"
    try:
        write_result = await write_section_page(
            app,
            section_id=section_id,
            parent_source_id=parent_source_id,
            title=rel_str,
            body=body,
            aliases=[location_uri],
            location_uri=location_uri,
            source_content_hash=section.content_hash,
            extra_frontmatter={
                "fetched_at": datetime.now(UTC).isoformat(),
                "language": classification.language,
            },
        )
    except SmaltClientError:
        raise
    # Section ids are upsert ids — smalt returns the same id we sent.
    canonical_section_id = write_result.get("id", section_id)

    entity_ids = await _write_entity_pages(app, entities, parent_id=canonical_section_id)
    glossary_ids = await _write_glossary_pages(app, glossary, parent_id=canonical_section_id)
    links_added = await _link_source_to_extracted(
        app,
        source_page_id=canonical_section_id,
        entities=entity_ids,
        glossary=glossary_ids,
    )

    return _SectionRecord(
        rel_path=rel_str,
        section_id=canonical_section_id,
        summary=summary,
        entity_page_ids=entity_ids,
        glossary_page_ids=glossary_ids,
        links_added=links_added,
        content_hash=section.content_hash,
    )


async def _synthesize_overview(app: App, sections: list[_SectionRecord], resolved: Path) -> str:
    """Run the source-overview agent against the section summaries."""
    provider = _get_provider_or_none(app)
    if provider is None or not sections:
        return ""
    return await agents.synthesize_source_overview(
        provider,
        section_summaries=[(s.rel_path, s.summary) for s in sections],
        source_label=str(resolved),
    )


# ---- shared sub-agent helpers ----


async def _run_sub_agents(
    app: App, section: RawSection, resolved: Path
) -> tuple[str, list[agents.ExtractedEntity], list[agents.ExtractedGlossaryTerm]]:
    """Drive the summarizer + entity + glossary agents.

    Skips the whole pipeline gracefully when the host doesn't have an
    LLM provider. Each agent runs independently; one failing doesn't
    take the others down.
    """
    provider = _get_provider_or_none(app)
    if provider is None:
        logger.warning(
            "ingest: LLM provider not available; skipping sub-agent pipeline "
            "(set ANTHROPIC_API_KEY to enable)"
        )
        return "", [], []

    source_label = str(resolved)
    summary = await agents.summarize(provider, body=section.body, source_label=source_label)
    entities = await agents.extract_entities(provider, body=section.body, source_label=source_label)
    glossary = await agents.extract_glossary(provider, body=section.body, source_label=source_label)
    return summary, entities, glossary


async def _write_entity_pages(
    app: App, entities: list[agents.ExtractedEntity], *, parent_id: str
) -> list[str]:
    """Write one EntityPage per extracted entity; return their canonical
    ids. Failures are logged and skipped — partial ingest beats abort."""
    out: list[str] = []
    for entity in entities:
        try:
            result = await write_entity_page(
                app,
                name=entity.name,
                aliases=entity.aliases,
                kind=entity.kind,
                mentioned_in_source_id=parent_id,
                snippet=entity.snippet,
            )
            ent_id = result.get("id")
            if ent_id:
                out.append(ent_id)
        except SmaltClientError:
            logger.exception("failed to write entity page for %r", entity.name)
    return out


async def _write_glossary_pages(
    app: App, glossary: list[agents.ExtractedGlossaryTerm], *, parent_id: str
) -> list[str]:
    """Write one glossary ConceptPage per term; return their canonical
    ids. Failures are logged and skipped."""
    out: list[str] = []
    for term in glossary:
        try:
            result = await write_glossary_page(
                app,
                term=term.term,
                definition=term.definition,
                mentioned_in_source_id=parent_id,
                snippet=term.snippet,
            )
            gid = result.get("id")
            if gid:
                out.append(gid)
        except SmaltClientError:
            logger.exception("failed to write glossary page for %r", term.term)
    return out


async def _get_symbol_outline_or_none(app: App, section: RawSection) -> str | None:
    """Call deco-assaying for code files; format the response as a
    Markdown symbol outline. Returns None when:

    - the file isn't code, or
    - the language isn't supported by deco, or
    - the deco child isn't reachable / errored, or
    - deco's response was empty (no symbols found).

    Failures degrade silently — a missing symbol outline doesn't
    block the section page from being written.
    """
    if section.classification.kind is not FileKind.CODE:
        return None
    language = section.classification.language
    if not deco_client.is_supported_language(language):
        return None
    try:
        payload = await deco_client.analyze_file(
            app,
            content=section.body,
            filename=section.path.name,
            language=language or "",
        )
    except deco_client.DecoUnavailable as e:
        logger.warning(
            "deco.analyze_file unavailable for %s; skipping symbol outline: %s",
            section.path,
            e,
        )
        return None
    rendered = deco_client.render_symbol_outline(payload)
    return rendered or None


async def _link_source_to_extracted(
    app: App, *, source_page_id: str, entities: list[str], glossary: list[str]
) -> int:
    """Add `mentions`/`defines` links from `source_page_id` to each
    entity/glossary page. Returns the count of links actually added
    (duplicates are reported by smalt and don't increment)."""
    targets: list[dict[str, Any]] = []
    for ent_id in entities:
        targets.append({"target": ent_id, "label": "mentions"})
    for gid in glossary:
        targets.append({"target": gid, "label": "defines"})
    if not targets:
        return 0
    try:
        result = await add_outgoing_links(app, page_id=source_page_id, targets=targets)
    except SmaltClientError:
        logger.exception(
            "failed to add cross-page links from %s to %d targets",
            source_page_id,
            len(targets),
        )
        return 0
    results = result.get("results") or []
    return sum(1 for r in results if isinstance(r, dict) and r.get("added"))


# ---- frontmatter / body builders ----


def _build_source_frontmatter(
    *,
    source_id: str,
    section: RawSection,
    resolved_path: Path,
    started_at: datetime,
) -> dict[str, Any]:
    """SourcePage frontmatter for a single-file ingest."""
    location_uri = f"file:{resolved_path}"
    return {
        "id": source_id,
        "type": "source",
        "title": resolved_path.name,
        "aliases": [location_uri],
        "tags": [],
        "location_uri": location_uri,
        "location_kind": "file",
        "source_content_hash": section.content_hash,
        "fetched_at": started_at.isoformat(),
        "domains": [],
    }


def _build_source_body(
    section: RawSection, summary: str, *, symbol_outline: str | None = None
) -> str:
    """Single-file SourcePage body.

    For code files, `symbol_outline` (from deco-assaying) is inserted
    between the summary and the raw-content block. None → no symbols
    section.
    """
    fence = "```" + (section.classification.language or "")
    if summary:
        parts = [
            "## Summary",
            "",
            summary,
            "",
        ]
        if symbol_outline:
            parts += ["## Symbols", "", symbol_outline, ""]
        parts += [
            "## Source content",
            "",
            f"- file: `{section.path}`",
            f"- sha256: `{section.content_hash}`",
            f"- truncated: {section.truncated}",
            "",
            fence,
            section.body,
            "```",
        ]
    else:
        parts = [
            "## Content (no LLM summary available)",
            "",
        ]
        if symbol_outline:
            parts += ["## Symbols", "", symbol_outline, ""]
        parts += [
            f"- file: `{section.path}`",
            f"- sha256: `{section.content_hash}`",
            f"- truncated: {section.truncated}",
            "",
            fence,
            section.body,
            "```",
        ]
    return "\n".join(parts)


def _build_source_index_frontmatter(
    *,
    source_id: str,
    dir_source: DirSource,
    contents: DirectoryContents,
    started_at: datetime,
) -> dict[str, Any]:
    """SourcePage frontmatter for a directory's source-index page."""
    fm: dict[str, Any] = {
        "id": source_id,
        "type": "source",
        "title": dir_source.path.name,
        "aliases": [dir_source.location_uri],
        "tags": [],
        "location_uri": dir_source.location_uri,
        "location_kind": dir_source.kind.value,
        "fetched_at": started_at.isoformat(),
        "domains": [],
        "ignored": list(contents.ignored),
    }
    if dir_source.is_git and dir_source.git:
        # Spread the git metadata onto the frontmatter using smalt's
        # documented field names (see SourcePage in smalt-mcp/schema.py).
        remotes = dir_source.git.get("remotes") or {}
        head = dir_source.git.get("head") or {}
        if remotes:
            fm["git_remotes"] = remotes
            origin = remotes.get("origin")
            if origin:
                fm["git_remote"] = origin
        if dir_source.git.get("branch"):
            fm["git_branch"] = dir_source.git["branch"]
        if dir_source.git.get("head_sha"):
            fm["git_head_sha"] = dir_source.git["head_sha"]
        if dir_source.git.get("dirty") is not None:
            fm["git_dirty"] = dir_source.git["dirty"]
        if head:
            if "sha" in head:
                fm["git_head_sha"] = head["sha"]
            if "author" in head:
                fm["git_head_author"] = head["author"]
            if "email" in head:
                fm["git_head_email"] = head["email"]
            if "date" in head:
                fm["git_head_date"] = head["date"]
            if "subject" in head:
                fm["git_head_subject"] = head["subject"]
    if dir_source.is_obsidian and dir_source.obsidian:
        if dir_source.obsidian.get("vault_name"):
            fm["obsidian_vault_name"] = dir_source.obsidian["vault_name"]
        # Drop any oversize blobs; keep only the small subset captured
        # by source_fetcher.
        config_subset = {
            k: dir_source.obsidian[k]
            for k in ("core_plugins", "community_plugins", "app_settings")
            if k in dir_source.obsidian
        }
        if config_subset:
            fm["obsidian_config"] = config_subset
    return fm


def _build_index_placeholder_body(dir_source: DirSource, contents: DirectoryContents) -> str:
    """Initial body for the source-index page, before sections are
    processed. Overwritten at the end of the directory pipeline with the
    synthesized overview + final TOC."""
    parts = [
        f"# {dir_source.path.name}",
        "",
        f"Source kind: `{dir_source.kind.value}`",
        f"Location: `{dir_source.location_uri}`",
        f"Supported files: {len(contents.supported)}",
        f"Ignored files: {len(contents.ignored)}",
        "",
        "_(overview synthesized at end of ingest)_",
    ]
    return "\n".join(parts)


def _build_source_index_body(
    *,
    dir_source: DirSource,
    contents: DirectoryContents,
    section_records: list[_SectionRecord],
    overview: str,
) -> str:
    """Final body for the source-index page: overview + section TOC +
    ignored-files list."""
    parts: list[str] = [f"# {dir_source.path.name}"]
    if overview:
        parts += ["", "## Overview", "", overview]
    if section_records:
        parts += ["", "## Sections", ""]
        for rec in section_records:
            parts.append(f"- `{rec.rel_path}` → `{rec.section_id}`")
    if contents.ignored:
        parts += ["", "## Ignored files", ""]
        for name in contents.ignored:
            parts.append(f"- {name}")
    if contents.truncated:
        parts += [
            "",
            "_Note: this ingest was truncated; the source has more supported "
            "files than were processed in a single run._",
        ]
    if dir_source.is_git and dir_source.git:
        parts += ["", "## Git", "", f"```\n{dir_source.git}\n```"]
    if dir_source.is_obsidian and dir_source.obsidian:
        parts += ["", "## Obsidian", "", f"```\n{dir_source.obsidian}\n```"]
    return "\n".join(parts)


def _build_section_body(
    section: RawSection,
    summary: str,
    rel_path: str,
    *,
    symbol_outline: str | None = None,
) -> str:
    """Body for a section page (one file in a multi-file source).

    For code files, `symbol_outline` (from deco-assaying) is inserted
    between the summary and the raw-content block. None → no symbols
    section.
    """
    fence = "```" + (section.classification.language or "")
    if summary:
        parts = [
            f"# {rel_path}",
            "",
            "## Summary",
            "",
            summary,
            "",
        ]
        if symbol_outline:
            parts += ["## Symbols", "", symbol_outline, ""]
        parts += [
            "## Source content",
            "",
            f"- relative path: `{rel_path}`",
            f"- sha256: `{section.content_hash}`",
            f"- truncated: {section.truncated}",
            "",
            fence,
            section.body,
            "```",
        ]
    else:
        parts = [
            f"# {rel_path}",
            "",
            "_(no LLM summary available)_",
            "",
        ]
        if symbol_outline:
            parts += ["## Symbols", "", symbol_outline, ""]
        parts += [
            f"- sha256: `{section.content_hash}`",
            f"- truncated: {section.truncated}",
            "",
            fence,
            section.body,
            "```",
        ]
    return "\n".join(parts)


# ---- id slugification ----


_SLUG_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")


def _make_source_id(resolved: Path) -> str:
    """Derive a stable, smalt-compatible id slug from a file path."""
    stem = resolved.stem
    ext = resolved.suffix.lstrip(".")
    slug = f"file-{stem}-{ext}" if ext else f"file-{stem}"
    slug = _SLUG_INVALID_CHARS.sub("-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug or not slug[0].isalnum():
        slug = f"file-{slug or 'unnamed'}"
    return slug[:80]


def _make_dir_source_id(dir_source: DirSource) -> str:
    """Source-id slug for a directory ingest.

    The slug encodes the kind + a sanitized name of the directory or
    git-remote tail. Smalt mangles it with a UUID suffix on create —
    the canonical id is what subsequent references use.
    """
    prefix = dir_source.kind.value  # "git", "obsidian", "dir"
    # Derive a meaningful tail from the location_uri.
    tail = dir_source.location_uri.split(":", 1)[-1]
    # For git URLs like `https://github.com/foo/bar.git`, take the
    # `foo-bar` tail. For paths, take the last component.
    if dir_source.kind.value == "git":
        # Strip protocol-style prefix.
        tail = tail.rstrip("/")
        if tail.endswith(".git"):
            tail = tail[:-4]
        parts = re.split(r"[/:]+", tail)
        parts = [p for p in parts if p]
        # Take the last 2 (org/repo) if possible.
        meaningful = parts[-2:] if len(parts) >= 2 else parts
        tail = "-".join(meaningful)
    else:
        tail = Path(tail).name or "root"
    slug = f"{prefix}-{tail}"
    slug = _SLUG_INVALID_CHARS.sub("-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug or not slug[0].isalnum():
        slug = f"src-{slug or 'unnamed'}"
    return slug[:80]


def _make_section_id(parent_source_id: str, rel_path: str) -> str:
    """Section page id: `<parent-source-id>::<rel-path-sanitized>`.

    smalt's section-id validator accepts `<source-id>::<rel-path>`
    where rel-path components are alphanumeric+`._-` and the leading
    char of each component is alphanumeric. We sanitize each component
    of the relative path to fit.
    """
    components = []
    for raw in Path(rel_path).parts:
        sanitized = _SLUG_INVALID_CHARS.sub("-", raw)
        # Replace dots that don't separate alnum from alnum
        # (Smalt's validator accepts dots inside components, but the
        # leading char must be alnum). Strip leading non-alnum.
        sanitized = re.sub(r"^[^a-zA-Z0-9]+", "", sanitized)
        if not sanitized:
            sanitized = "x"
        components.append(sanitized)
    rel_clean = "/".join(components)
    full = f"{parent_source_id}::{rel_clean}"
    # smalt caps at 254 chars.
    if len(full) > 254:
        # Truncate the rel-path portion, not the parent id.
        max_rel = 254 - len(parent_source_id) - len("::")
        full = f"{parent_source_id}::{rel_clean[:max_rel]}"
    return full


def _get_provider_or_none(app: App) -> AnthropicProvider | None:
    """Return the host's LLM provider, or None if the host wasn't
    started (no API key, etc.). We deliberately don't raise — agent
    fallback is the right behavior for an ingest pipeline."""
    try:
        return cast("AnthropicProvider", app.host.provider)
    except RuntimeError:
        return None
