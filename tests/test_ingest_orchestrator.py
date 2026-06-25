# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.ingest.orchestrator — pipeline driver.

Unit-level coverage with mocked smalt-mcp client + LLM provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cobalt_grinding.app import App
from cobalt_grinding.config import Config, MCPConfig
from cobalt_grinding.daemon.mcp_clients import CallResult
from cobalt_grinding.ingest.orchestrator import (
    IngestError,
    _make_source_id,
    ingest,
)

# ---- helpers ----


def _bare_app(tmp_path: Path) -> App:
    return App(
        Config(
            smalt_dir=tmp_path / "wiki",
            cobalt_grinding_dir=tmp_path / "cobalt_grinding",
            mcp=MCPConfig(clients={}),
        )
    )


def _ok_call_result(payload: dict[str, Any] | list[Any] | str) -> CallResult:
    text_block = MagicMock()
    text_block.text = payload if isinstance(payload, str) else json.dumps(payload)
    return CallResult(ok=True, is_error=False, content=[text_block], error_text=None)


def _err_call_result(message: str) -> CallResult:
    return CallResult(ok=False, is_error=True, content=[], error_text=message)


def _toolside_error_result(payload: dict[str, Any]) -> CallResult:
    """A CallResult representing a successful MCP dispatch but a tool that
    reported an error (`isError=True`)."""
    text_block = MagicMock()
    text_block.text = json.dumps(payload)
    return CallResult(ok=True, is_error=True, content=[text_block], error_text=None)


def _install_smalt_mock(
    app: App,
    *,
    find_by_alias_response: dict[str, Any] | None = None,
    write_page_response: dict[str, Any] | None = None,
    write_page_side_effect: Any = None,
    add_links_response: dict[str, Any] | None = None,
    read_page_response: dict[str, Any] | None = None,
    deco_analyze_response: dict[str, Any] | None = None,
    flint_read_text_response: dict[str, Any] | None = None,
    flint_pdf_info_response: dict[str, Any] | None = None,
) -> AsyncMock:
    """Install a fake mcp_clients on `app` whose call_tool dispatches per
    tool name. Each kwarg overrides the default response for that tool.
    Returns the call_tool AsyncMock for assertion convenience.

    Default behavior:
      smalt-mcp.find_by_alias → no matches (no existing source)
      smalt-mcp.write_page → success with canonical id `<original_id>__abc`
      smalt-mcp.add_links → success with empty results
      smalt-mcp.read_page → success but no source_content_hash
      deco-assaying.analyze_file → empty (no symbols) — orchestrator
        treats this as "no outline to render"
    """
    default_find = {"alias": "", "matches": [], "count": 0, "fuzzy": False}

    write_page_calls: list[dict[str, Any]] = []

    async def dispatch(client: str, tool: str, args: dict[str, Any]) -> CallResult:
        if client == "deco-assaying":
            if tool == "analyze_file":
                payload = deco_analyze_response or {"symbols": [], "imports": []}
                return _ok_call_result(payload)
            raise AssertionError(f"unexpected deco tool: {tool}")
        if client == "flint-slating":
            if tool == "pdf_read_text":
                payload = flint_read_text_response or {
                    "pages": [{"page": 1, "text": "(fake PDF text)"}],
                    "page_count": 1,
                }
                return _ok_call_result(payload)
            if tool == "pdf_info":
                payload = flint_pdf_info_response or {
                    "page_count": 1,
                    "sha256": "a" * 64,
                    "metadata": {},
                }
                return _ok_call_result(payload)
            raise AssertionError(f"unexpected flint tool: {tool}")
        assert client == "smalt-mcp", f"unexpected client: {client}"
        if tool == "find_by_alias":
            payload = find_by_alias_response or default_find
            return _ok_call_result(payload)
        if tool == "read_page":
            payload = read_page_response or {
                "id": args["page_id"],
                "title": "?",
                "type": "source",
                "frontmatter": {},
                "body": "",
            }
            return _ok_call_result(payload)
        if tool == "write_page":
            write_page_calls.append(args)
            if write_page_side_effect is not None:
                if isinstance(write_page_side_effect, BaseException):
                    raise write_page_side_effect
                if isinstance(write_page_side_effect, CallResult):
                    return write_page_side_effect
            if write_page_response is not None:
                return _ok_call_result(write_page_response)
            # Default: synth a canonical id from the caller's frontmatter id.
            original = args["frontmatter"]["id"]
            canonical = f"{original}__abc"
            return _ok_call_result(
                {
                    "id": canonical,
                    "original_id": original,
                    "path": f"pages/sources/{canonical}.md",
                    "type": args["frontmatter"].get("type", "source"),
                    "mode": args.get("mode", "create"),
                }
            )
        if tool == "add_links":
            payload = add_links_response or {
                "page_id": args["page_id"],
                "added": len(args["links"]),
                "results": [{"added": True, "link": link} for link in args["links"]],
            }
            return _ok_call_result(payload)
        raise AssertionError(f"unexpected tool: {tool}")

    fake = MagicMock()
    fake.call_tool = AsyncMock(side_effect=dispatch)
    fake.list_tools = MagicMock(return_value=[])
    app._mcp_clients = fake  # type: ignore[assignment]
    # Squirrel away the per-tool argument log so tests can assert on it.
    fake._write_page_calls = write_page_calls  # type: ignore[attr-defined]
    return fake.call_tool


# ---- happy path (no LLM — host not started) ----


async def test_ingest_writes_source_page_when_llm_unavailable(tmp_path: Path) -> None:
    """Without an LLM provider, the orchestrator still writes the source
    page — just with a placeholder body."""
    app = _bare_app(tmp_path)
    f = tmp_path / "hello.md"
    f.write_text("# Hello\n\nWorld.\n", encoding="utf-8")
    _install_smalt_mock(app)

    result = await ingest(app, f)

    fm = app._mcp_clients._write_page_calls[0]["frontmatter"]  # type: ignore[union-attr]
    assert fm["type"] == "source"
    assert fm["location_kind"] == "file"
    assert fm["location_uri"].startswith("file:")
    assert fm["location_uri"].endswith("hello.md")
    assert len(fm["source_content_hash"]) == 64
    assert fm["aliases"] == [fm["location_uri"]]
    # No LLM → no entity / glossary pages.
    assert result.entity_pages_written == []
    assert result.glossary_pages_written == []
    assert result.links_added == 0
    # Source page was written.
    assert result.source_id.startswith("file-hello-md")
    assert result.pages_written == [result.source_id]


async def test_ingest_skips_when_content_hash_matches_existing(tmp_path: Path) -> None:
    """Re-ingest with unchanged content → orchestrator should short-circuit."""
    app = _bare_app(tmp_path)
    f = tmp_path / "same.md"
    f.write_text("same content", encoding="utf-8")

    # Find the file's actual sha256 the orchestrator will compute.
    import hashlib

    existing_hash = hashlib.sha256(b"same content").hexdigest()
    existing_id = "file-same-md__previous"
    _install_smalt_mock(
        app,
        find_by_alias_response={
            "alias": f"file:{f.resolve()}",
            "matches": [{"id": existing_id, "title": "same.md", "type": "source"}],
            "count": 1,
            "fuzzy": False,
        },
        read_page_response={
            "id": existing_id,
            "title": "same.md",
            "type": "source",
            "frontmatter": {"source_content_hash": existing_hash},
            "body": "",
        },
    )

    result = await ingest(app, f)
    assert result.skipped is True
    assert result.skip_reason == "content_hash_matches_existing"
    assert result.source_id == existing_id
    assert result.pages_written == []
    # write_page was NOT called (find_by_alias + read_page only).
    assert app._mcp_clients._write_page_calls == []  # type: ignore[union-attr]


async def test_ingest_rewrites_when_content_hash_differs(tmp_path: Path) -> None:
    """Re-ingest with changed content → orchestrator should write a fresh
    source page (Chunk 2 always creates new; in-place update lands later)."""
    app = _bare_app(tmp_path)
    f = tmp_path / "changed.md"
    f.write_text("new content", encoding="utf-8")
    _install_smalt_mock(
        app,
        find_by_alias_response={
            "alias": f"file:{f.resolve()}",
            "matches": [{"id": "file-changed-md__old", "title": "changed.md", "type": "source"}],
            "count": 1,
            "fuzzy": False,
        },
        read_page_response={
            "id": "file-changed-md__old",
            "title": "changed.md",
            "type": "source",
            # Different hash from what the orchestrator will compute.
            "frontmatter": {"source_content_hash": "0" * 64},
            "body": "",
        },
    )

    result = await ingest(app, f)
    assert result.skipped is False
    # A fresh source page was written.
    assert len(app._mcp_clients._write_page_calls) == 1  # type: ignore[union-attr]
    assert result.pages_written


async def test_ingest_with_llm_writes_entity_and_glossary_pages_with_links(
    tmp_path: Path,
) -> None:
    """End-to-end Chunk 2 flow: LLM extracts entities + glossary terms;
    orchestrator writes a SourcePage + EntityPage + ConceptPage; adds
    cross-page links source → each extracted page."""
    app = _bare_app(tmp_path)
    f = tmp_path / "thing.md"
    f.write_text("# A doc mentioning Anthropic and MCP\n", encoding="utf-8")

    # Track every smalt call.
    write_page_calls: list[dict[str, Any]] = []
    add_links_calls: list[dict[str, Any]] = []

    async def dispatch(_client: str, tool: str, args: dict[str, Any]) -> CallResult:
        if tool == "find_by_alias":
            return _ok_call_result({"matches": [], "count": 0})
        if tool == "write_page":
            write_page_calls.append(args)
            original = args["frontmatter"]["id"]
            canonical = f"{original}__id"
            page_type = args["frontmatter"]["type"]
            return _ok_call_result(
                {
                    "id": canonical,
                    "original_id": original,
                    "path": f"pages/{page_type}s/{canonical}.md",
                    "type": page_type,
                    "mode": "create",
                }
            )
        if tool == "add_links":
            add_links_calls.append(args)
            return _ok_call_result(
                {
                    "page_id": args["page_id"],
                    "added": len(args["links"]),
                    "results": [{"added": True, "link": ln} for ln in args["links"]],
                }
            )
        raise AssertionError(f"unexpected tool: {tool}")

    fake = MagicMock()
    fake.call_tool = AsyncMock(side_effect=dispatch)
    app._mcp_clients = fake  # type: ignore[assignment]

    # Install a fake host with a fake provider that returns canned
    # summarizer/entity/glossary outputs.
    def _msg(text: str) -> Any:
        block = MagicMock()
        block.type = "text"
        block.text = text
        m = MagicMock()
        m.content = [block]
        return m

    extractor_replies = {
        "summarization": _msg("This file is a placeholder doc."),
        "entities": _msg(
            '[{"name":"Anthropic","aliases":[],"kind":"org","snippet":"mentioning Anthropic"}]'
        ),
        "glossary": _msg(
            '[{"term":"MCP","definition":"Model Context Protocol","snippet":"and MCP"}]'
        ),
    }

    async def fake_complete(**kwargs: Any) -> Any:
        system = kwargs.get("system", "")
        if "summarization" in system:
            return extractor_replies["summarization"]
        if "named entities" in system:
            return extractor_replies["entities"]
        if "glossary terms" in system:
            return extractor_replies["glossary"]
        raise AssertionError(f"unknown system prompt: {system[:60]}")

    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(side_effect=fake_complete)
    fake_host = MagicMock()
    fake_host.provider = fake_provider
    app._host = fake_host  # type: ignore[assignment]

    result = await ingest(app, f)

    # Source + entity + glossary pages all written.
    types_written = [c["frontmatter"]["type"] for c in write_page_calls]
    assert "source" in types_written
    assert "entity" in types_written
    assert "concept" in types_written
    # Entity page has glossary=False / entity_kind=org.
    entity_call = next(c for c in write_page_calls if c["frontmatter"]["type"] == "entity")
    assert entity_call["frontmatter"]["entity_kind"] == "org"
    # Glossary concept page has glossary=True.
    concept_call = next(c for c in write_page_calls if c["frontmatter"]["type"] == "concept")
    assert concept_call["frontmatter"]["glossary"] is True

    # Cross-page links added from the source.
    assert len(add_links_calls) == 1
    links = add_links_calls[0]["links"]
    labels = {link.get("label") for link in links}
    assert "mentions" in labels
    assert "defines" in labels

    # Result reports both auxiliary page lists + link count.
    assert len(result.entity_pages_written) == 1
    assert len(result.glossary_pages_written) == 1
    assert result.links_added == 2

    # Source page body contains the summary at the top.
    source_call = next(c for c in write_page_calls if c["frontmatter"]["type"] == "source")
    assert "placeholder doc" in source_call["body"]


# ---- directory ingest (Chunk 3) ----


async def test_ingest_code_file_includes_symbol_outline_from_deco(tmp_path: Path) -> None:
    """Single-file ingest of a Python file calls deco-assaying and
    injects a `## Symbols` section into the source-page body."""
    app = _bare_app(tmp_path)
    f = tmp_path / "module.py"
    f.write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    _install_smalt_mock(
        app,
        deco_analyze_response={
            "symbols": [{"name": "hello", "kind": "function", "span": {"start_line": 1}}],
            "imports": [],
        },
    )

    await ingest(app, f)

    write_call = app._mcp_clients._write_page_calls[0]  # type: ignore[union-attr]
    body = write_call["body"]
    assert "## Symbols" in body
    assert "hello" in body
    assert "function" in body
    assert "line 1" in body


async def test_ingest_pdf_url_routes_through_flint(tmp_path: Path) -> None:
    """A `.pdf` URL goes through `flint.pdf_info` + `flint.pdf_read_text`
    (URL source), not through `git clone`. Result has
    `location_kind: url` and uses flint's sha256 as `content_hash`."""
    app = _bare_app(tmp_path)
    _install_smalt_mock(
        app,
        flint_pdf_info_response={
            "page_count": 3,
            "sha256": "f" * 64,
            "metadata": {"title": "A Paper", "author": "Researcher"},
        },
        flint_read_text_response={
            "pages": [
                {"page": 1, "text": "abstract"},
                {"page": 2, "text": "methods"},
                {"page": 3, "text": "results"},
            ],
            "page_count": 3,
        },
    )

    result = await ingest(app, "https://example.com/paper.pdf")

    assert result.location_uri == "url:https://example.com/paper.pdf"
    assert result.location_kind == "url"
    assert result.content_hash == "f" * 64
    assert result.files_ingested == 1
    # Verify the smalt write_page got the right shape.
    fm = app._mcp_clients._write_page_calls[0]["frontmatter"]  # type: ignore[union-attr]
    assert fm["type"] == "source"
    assert fm["location_uri"] == "url:https://example.com/paper.pdf"
    assert fm["location_kind"] == "url"
    assert fm["source_content_hash"] == "f" * 64
    # PDF metadata flowed through.
    assert fm.get("pdf_author") == "Researcher"
    assert fm.get("pdf_page_count") == 3
    # Title came from PDF metadata, not the URL filename.
    assert fm["title"] == "A Paper"

    # Verify both flint tools got called.
    tool_calls = [
        (c.args[0], c.args[1])
        for c in app._mcp_clients.call_tool.await_args_list  # type: ignore[union-attr]
    ]
    assert ("flint-slating", "pdf_info") in tool_calls
    assert ("flint-slating", "pdf_read_text") in tool_calls


async def test_ingest_pdf_url_skips_on_hash_match(tmp_path: Path) -> None:
    """Re-ingest a PDF URL whose sha256 matches the existing source
    page → skip (no write)."""
    app = _bare_app(tmp_path)
    existing_id = "url-paper__1"
    _install_smalt_mock(
        app,
        flint_pdf_info_response={"page_count": 3, "sha256": "abc", "metadata": {}},
        find_by_alias_response={
            "alias": "url:https://example.com/paper.pdf",
            "matches": [{"id": existing_id, "title": "paper.pdf", "type": "source"}],
            "count": 1,
            "fuzzy": False,
        },
        read_page_response={
            "id": existing_id,
            "title": "paper.pdf",
            "type": "source",
            "frontmatter": {"source_content_hash": "abc"},
            "body": "",
        },
    )

    result = await ingest(app, "https://example.com/paper.pdf")
    assert result.skipped is True
    assert result.skip_reason == "content_hash_matches_existing"
    assert result.source_id == existing_id
    # No write_page call.
    assert app._mcp_clients._write_page_calls == []  # type: ignore[union-attr]


async def test_ingest_pdf_url_propagates_flint_failure(tmp_path: Path) -> None:
    """If flint is unreachable, the PDF URL ingest surfaces a clear
    IngestError (not a generic clone-failure)."""
    app = _bare_app(tmp_path)

    async def dispatch(client: str, _tool: str, _args: dict[str, Any]) -> CallResult:
        if client == "flint-slating":
            return CallResult(ok=False, is_error=True, content=[], error_text="flint not running")
        return _ok_call_result({})

    fake = MagicMock()
    fake.call_tool = AsyncMock(side_effect=dispatch)
    app._mcp_clients = fake  # type: ignore[assignment]

    with pytest.raises(IngestError, match="flint not running"):
        await ingest(app, "https://example.com/paper.pdf")


async def test_ingest_pdf_url_is_checked_before_git_url(tmp_path: Path) -> None:
    """A `.pdf` URL must NOT be routed to `git clone` — `looks_like_pdf_url`
    is checked first."""
    app = _bare_app(tmp_path)
    _install_smalt_mock(app)

    # If the orchestrator wrongly routed to git clone, this test would
    # invoke `clone_to_tempdir` which calls subprocess. We DON'T patch
    # clone — if it gets called, the test fails (subprocess error).
    # The successful flint path proves we never reached the clone.
    await ingest(app, "https://example.com/whitepaper.pdf")

    # Confirm: only flint tools + smalt tools were used; no clone.
    tool_calls = [
        c.args[0]
        for c in app._mcp_clients.call_tool.await_args_list  # type: ignore[union-attr]
    ]
    assert "flint-slating" in tool_calls
    # And no MCP call to `deco-assaying` either (PDFs aren't code).


async def test_ingest_pdf_uses_flint_for_body(tmp_path: Path) -> None:
    """A `.pdf` file routes through `flint-slating.pdf_read_text` for
    the body; the SourcePage body contains the extracted text."""
    app = _bare_app(tmp_path)
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 dummy bytes")
    _install_smalt_mock(
        app,
        flint_read_text_response={
            "pages": [
                {"page": 1, "text": "the document's first page text"},
                {"page": 2, "text": "second page details"},
            ],
            "page_count": 2,
        },
    )

    await ingest(app, f)

    write_call = app._mcp_clients._write_page_calls[0]  # type: ignore[union-attr]
    body = write_call["body"]
    assert "the document's first page text" in body
    assert "second page details" in body
    # PDF files are NOT code, so no symbols section.
    assert "## Symbols" not in body


async def test_ingest_non_code_file_skips_symbol_outline(tmp_path: Path) -> None:
    """Markdown file → no deco call, no symbols section in the body."""
    app = _bare_app(tmp_path)
    f = tmp_path / "readme.md"
    f.write_text("# Readme", encoding="utf-8")
    _install_smalt_mock(app)

    await ingest(app, f)

    write_call = app._mcp_clients._write_page_calls[0]  # type: ignore[union-attr]
    body = write_call["body"]
    assert "## Symbols" not in body
    # Sanity: deco was NOT called.
    tools_called = [
        c.args[1]
        for c in app._mcp_clients.call_tool.await_args_list  # type: ignore[union-attr]
    ]
    assert "analyze_file" not in tools_called


async def test_ingest_directory_writes_index_and_sections(tmp_path: Path) -> None:
    """A directory with three supported files produces a source-index
    page + three section pages, all addressed via `parent_source`."""
    app = _bare_app(tmp_path)
    d = tmp_path / "tinydir"
    d.mkdir()
    (d / "a.md").write_text("# A", encoding="utf-8")
    (d / "b.py").write_text("print('b')", encoding="utf-8")
    (d / "c.toml").write_text("[c]\nx = 1", encoding="utf-8")
    _install_smalt_mock(app)

    result = await ingest(app, d)

    # source-index page + 3 section pages were all writes.
    write_page_calls = app._mcp_clients._write_page_calls  # type: ignore[union-attr]
    # First write is the index (placeholder body); then 3 sections; then
    # the index gets a final update_mode write with the synthesized body.
    types_written = [c["frontmatter"]["type"] for c in write_page_calls]
    assert types_written.count("source") >= 4  # index + 3 sections (sources too)
    # Section pages have `::` in their id (smalt's section-id pattern).
    section_ids = [
        c["frontmatter"]["id"] for c in write_page_calls if "::" in c["frontmatter"]["id"]
    ]
    assert len(section_ids) == 3
    # `parent_source` points at the source-index canonical id.
    parents = {
        c["frontmatter"].get("parent_source")
        for c in write_page_calls
        if "parent_source" in c["frontmatter"]
    }
    assert len(parents) == 1
    parent = parents.pop()
    assert parent and parent.startswith("dir-tinydir")

    # IngestResult reflects the structure.
    assert result.files_ingested == 3
    assert len(result.section_pages_written) == 3
    assert result.source_id.startswith("dir-tinydir")
    assert result.location_kind == "dir"


async def test_ingest_directory_records_ignored_files(tmp_path: Path) -> None:
    """Files with unsupported extensions go in `files_ignored`, not
    section pages."""
    app = _bare_app(tmp_path)
    d = tmp_path / "mixed"
    d.mkdir()
    (d / "real.md").write_text("real", encoding="utf-8")
    (d / "image.png").write_bytes(b"\x89PNG")
    (d / "binary.so").write_bytes(b"\x7fELF")
    _install_smalt_mock(app)

    result = await ingest(app, d)
    assert result.files_ingested == 1
    assert "image.png" in result.files_ignored
    assert "binary.so" in result.files_ignored


async def test_ingest_directory_skips_hidden_dot_dirs(tmp_path: Path) -> None:
    """`.git/` / `.obsidian/` contents are NOT processed as sections."""
    app = _bare_app(tmp_path)
    d = tmp_path / "plain"
    d.mkdir()
    (d / "real.md").write_text("real", encoding="utf-8")
    git_dir = d / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]", encoding="utf-8")
    _install_smalt_mock(app)

    result = await ingest(app, d)
    # Only the real.md becomes a section.
    assert result.files_ingested == 1
    section_paths = [
        c["frontmatter"]["title"]
        for c in app._mcp_clients._write_page_calls  # type: ignore[union-attr]
        if "::" in c["frontmatter"]["id"]
    ]
    assert section_paths == ["real.md"]


# ---- URL ingestion (Phase G demo: github.com URL → clone → directory pipeline) ----


async def test_ingest_url_clones_then_runs_directory_pipeline(tmp_path: Path) -> None:
    """A URL input goes through `url_fetcher.clone_to_tempdir` (which we
    patch so no real `git clone` runs), and the resulting local path
    feeds the existing directory pipeline. SourcePage's location_uri
    ends up reflecting the git remote captured by source_fetcher
    (the patched clone fakes a `.git/` with a remote)."""
    app = _bare_app(tmp_path)

    # Build a fake "cloned repo" with a .git/ + remote so source_fetcher
    # classifies it as a git source.
    fake_clone = tmp_path / "fake-clone"
    fake_clone.mkdir()
    (fake_clone / "README.md").write_text("# Hello", encoding="utf-8")
    git_dir = fake_clone / ".git"
    git_dir.mkdir()
    # No real git ops; source_fetcher's _capture_git_metadata calls
    # `_run_git` which will fail without a real repo. We patch it
    # to return a canonical origin remote.

    _install_smalt_mock(app)

    # Patch `clone_to_tempdir` to yield our fake_clone instead of
    # actually invoking `git clone`. Patch source_fetcher's git metadata
    # capture so the .git/ stub doesn't trigger a real subprocess.
    import contextlib

    @contextlib.contextmanager
    def fake_clone_ctx(_url: str):
        yield fake_clone

    fake_remotes = {"origin": "https://github.com/foo/bar.git"}

    with (
        patch(
            "cobalt_grinding.ingest.orchestrator.url_fetcher.clone_to_tempdir",
            fake_clone_ctx,
        ),
        patch(
            "cobalt_grinding.ingest.source_fetcher._capture_git_metadata",
            return_value={"remotes": fake_remotes, "branch": "main"},
        ),
    ):
        result = await ingest(app, "https://github.com/foo/bar")

    # The source page's location_uri reflects the GIT URL, not the
    # tempdir path. This is what makes re-ingest lookup work across
    # clones.
    assert result.location_uri == "git:https://github.com/foo/bar.git"
    assert result.location_kind == "git"
    # The README.md became a section page.
    assert result.files_ingested == 1


async def test_ingest_url_propagates_clone_failure_as_ingest_error(tmp_path: Path) -> None:
    """If `git clone` fails, the orchestrator surfaces a clear
    IngestError with the underlying clone-failure reason."""
    app = _bare_app(tmp_path)
    _install_smalt_mock(app)

    import contextlib

    from cobalt_grinding.ingest.url_fetcher import CloneError

    @contextlib.contextmanager
    def failing_clone_ctx(_url: str):
        raise CloneError("git clone failed: repository not found")
        yield  # unreachable but appeases the type checker

    with (
        patch(
            "cobalt_grinding.ingest.orchestrator.url_fetcher.clone_to_tempdir",
            failing_clone_ctx,
        ),
        pytest.raises(IngestError, match="repository not found"),
    ):
        await ingest(app, "https://github.com/no/such/repo")


async def test_ingest_url_detection_does_not_check_filesystem(tmp_path: Path) -> None:
    """Even though `https://…` won't exist as a local path, the
    orchestrator must route it through the URL path before trying
    `Path.exists`. (Regression guard.)"""
    app = _bare_app(tmp_path)
    _install_smalt_mock(app)

    fake_clone = tmp_path / "tiny-clone"
    fake_clone.mkdir()
    (fake_clone / "f.md").write_text("x", encoding="utf-8")

    import contextlib

    @contextlib.contextmanager
    def fake_clone_ctx(_url: str):
        yield fake_clone

    with (
        patch(
            "cobalt_grinding.ingest.orchestrator.url_fetcher.clone_to_tempdir",
            fake_clone_ctx,
        ),
        patch(
            "cobalt_grinding.ingest.source_fetcher._capture_git_metadata",
            return_value={},
        ),
    ):
        # Should NOT raise "path does not exist" even though the URL
        # isn't a filesystem path.
        result = await ingest(app, "https://github.com/foo/bar.git")
    assert result.files_ingested >= 0


async def test_ingest_empty_directory_produces_index_page_zero_sections(
    tmp_path: Path,
) -> None:
    app = _bare_app(tmp_path)
    d = tmp_path / "empty"
    d.mkdir()
    _install_smalt_mock(app)

    result = await ingest(app, d)
    assert result.files_ingested == 0
    assert result.section_pages_written == []
    # An index page is still written.
    assert result.pages_written


async def test_full_directory_pipeline_with_mocked_llm(tmp_path: Path) -> None:
    """End-to-end Chunk 3 flow on a multi-file fixture directory with a
    mocked LLM. Asserts: source-index page + N section pages written;
    entity/glossary pages from EACH section; cross-page links from the
    source-index to its sections; LLM-driven source overview lands in
    the index body.

    This is the "self-ingest"-style test the master plan calls for —
    smaller fixture instead of the whole cobalt-grinding repo, but
    exercises every chunk of the pipeline at once.
    """
    app = _bare_app(tmp_path)
    repo = tmp_path / "fakerepo"
    repo.mkdir()
    (repo / "README.md").write_text("# FakeRepo\n\nA test project.", encoding="utf-8")
    (repo / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text('[project]\nname="fakerepo"', encoding="utf-8")
    (repo / "image.png").write_bytes(b"\x89PNG")  # ignored
    # A subdir with a section file.
    sub = repo / "src"
    sub.mkdir()
    (sub / "helpers.py").write_text("def helper():\n    return 1\n", encoding="utf-8")

    # Track every smalt write_page call.
    write_page_calls: list[dict[str, Any]] = []
    add_links_calls: list[dict[str, Any]] = []

    async def dispatch(client: str, tool: str, args: dict[str, Any]) -> CallResult:
        if client == "deco-assaying" and tool == "analyze_file":
            return _ok_call_result(
                {
                    "symbols": [{"name": "main", "kind": "function", "span": {"start_line": 1}}],
                    "imports": [],
                }
            )
        if tool == "find_by_alias":
            return _ok_call_result({"matches": [], "count": 0})
        if tool == "write_page":
            write_page_calls.append(args)
            original = args["frontmatter"]["id"]
            # Section ids use the upsert path; smalt returns them as-is.
            canonical = original if "::" in original else f"{original}__abc"
            return _ok_call_result(
                {
                    "id": canonical,
                    "original_id": original,
                    "path": f"pages/sources/{canonical}.md",
                    "type": args["frontmatter"].get("type", "source"),
                    "mode": args.get("mode", "create"),
                }
            )
        if tool == "add_links":
            add_links_calls.append(args)
            return _ok_call_result(
                {
                    "page_id": args["page_id"],
                    "added": len(args["links"]),
                    "results": [{"added": True, "link": ln} for ln in args["links"]],
                }
            )
        raise AssertionError(f"unexpected tool: {tool}")

    fake = MagicMock()
    fake.call_tool = AsyncMock(side_effect=dispatch)
    app._mcp_clients = fake  # type: ignore[assignment]

    # Fake LLM provider: returns canned responses based on prompt system.
    def _msg(text: str) -> Any:
        block = MagicMock()
        block.type = "text"
        block.text = text
        m = MagicMock()
        m.content = [block]
        return m

    async def fake_complete(**kwargs: Any) -> Any:
        system = kwargs.get("system", "")
        if "source overview" in system or "synthesize" in system.lower():
            return _msg("FakeRepo is a small Python test project for ingest verification.")
        if "summarization" in system:
            return _msg("This file summarizes one piece of the repo.")
        if "named entities" in system:
            return _msg('[{"name":"FakeRepo","aliases":[],"kind":"product","snippet":"FakeRepo"}]')
        if "glossary terms" in system:
            return _msg('[{"term":"main","definition":"entry point","snippet":"def main"}]')
        raise AssertionError(f"unknown system: {system[:60]!r}")

    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(side_effect=fake_complete)
    fake_host = MagicMock()
    fake_host.provider = fake_provider
    app._host = fake_host  # type: ignore[assignment]

    result = await ingest(app, repo)

    # 4 supported files (README.md + main.py + pyproject.toml + src/helpers.py)
    assert result.files_ingested == 4
    assert len(result.section_pages_written) == 4
    # Ignored .png file recorded.
    assert "image.png" in result.files_ignored

    # Source-index has been written TWICE: once with placeholder body
    # (create), once with synthesized overview (update). That's by
    # design so the body can incorporate section summaries.
    index_writes = [
        c
        for c in write_page_calls
        if c["frontmatter"].get("type") == "source"
        and "::" not in c["frontmatter"]["id"]
        and c["frontmatter"].get("parent_source") is None
    ]
    assert len(index_writes) == 2
    create_call, update_call = index_writes
    assert create_call["mode"] == "create"
    assert update_call["mode"] == "update"
    assert "FakeRepo is a small Python test project" in update_call["body"]

    # Each section has parent_source pointing to the source-index id.
    section_writes = [c for c in write_page_calls if "::" in c["frontmatter"]["id"]]
    assert len(section_writes) == 4
    parents = {c["frontmatter"]["parent_source"] for c in section_writes}
    assert len(parents) == 1

    # Cross-page links: source-index → 4 sections via `contains`, plus
    # each section → its own extracted entity+glossary pages.
    # add_links was called once per section (for the section's
    # mentions/defines edges) plus once for the source-index's contains.
    assert len(add_links_calls) >= 5  # 4 sections + 1 index
    contains_calls = [
        c for c in add_links_calls if any(link.get("label") == "contains" for link in c["links"])
    ]
    assert len(contains_calls) == 1
    assert len(contains_calls[0]["links"]) == 4


async def test_force_flag_bypasses_re_ingest_skip(tmp_path: Path) -> None:
    """`force=True` should re-write even when hash matches."""
    app = _bare_app(tmp_path)
    f = tmp_path / "force.md"
    f.write_text("content", encoding="utf-8")
    # Don't bother with find_by_alias matching — force should skip that
    # lookup entirely.
    _install_smalt_mock(app)

    result = await ingest(app, f, force=True)
    assert result.skipped is False
    # find_by_alias was not even called (force skips the lookup).
    tool_names = [c.args[1] for c in app._mcp_clients.call_tool.await_args_list]  # type: ignore[union-attr]
    assert "find_by_alias" not in tool_names


# ---- error paths ----


async def test_ingest_rejects_missing_path(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    missing = tmp_path / "nope.md"
    with pytest.raises(IngestError, match="does not exist"):
        await ingest(app, missing)


async def test_ingest_accepts_empty_directory(tmp_path: Path) -> None:
    """Chunk 3 supports directory ingest. An empty directory should
    produce a source-index page with zero sections (no error)."""
    app = _bare_app(tmp_path)
    d = tmp_path / "empty-dir"
    d.mkdir()
    _install_smalt_mock(app)
    result = await ingest(app, d)
    assert result.files_ingested == 0
    assert result.section_pages_written == []
    assert result.location_kind in ("dir", "git", "obsidian")


async def test_ingest_rejects_unsupported_file_type(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(IngestError, match="not supported"):
        await ingest(app, f)


async def test_ingest_propagates_smalt_write_page_dispatch_failure(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    f = tmp_path / "ok.md"
    f.write_text("body", encoding="utf-8")
    _install_smalt_mock(app, write_page_side_effect=_err_call_result("smalt-mcp not running"))
    with pytest.raises(IngestError, match=r"smalt\.write_page dispatch failed"):
        await ingest(app, f)


async def test_ingest_propagates_smalt_tool_side_error(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    f = tmp_path / "ok.md"
    f.write_text("body", encoding="utf-8")
    _install_smalt_mock(
        app,
        write_page_side_effect=_toolside_error_result(
            {"error": "validation_error", "message": "bad frontmatter"}
        ),
    )
    with pytest.raises(IngestError, match="bad frontmatter"):
        await ingest(app, f)


async def test_ingest_raises_when_mcp_supervisor_not_started(tmp_path: Path) -> None:
    app = _bare_app(tmp_path)
    f = tmp_path / "ok.md"
    f.write_text("body", encoding="utf-8")
    # No _mcp_clients patch; the property raises.
    with pytest.raises(IngestError, match="supervisor not started"):
        await ingest(app, f)


# ---- source-id slugification ----


def test_make_source_id_handles_normal_filename(tmp_path: Path) -> None:
    sid = _make_source_id(tmp_path / "readme.md")
    assert sid.startswith("file-readme")
    assert sid.endswith("md") or "md" in sid


def test_make_source_id_strips_disallowed_chars(tmp_path: Path) -> None:
    sid = _make_source_id(tmp_path / "weird name with spaces.md")
    assert " " not in sid
    assert all(c.isalnum() or c in "-_" for c in sid)
    assert sid[0].isalnum()


def test_make_source_id_caps_length(tmp_path: Path) -> None:
    sid = _make_source_id(tmp_path / ("a" * 500 + ".md"))
    assert len(sid) <= 80


def test_make_source_id_no_extension(tmp_path: Path) -> None:
    sid = _make_source_id(tmp_path / "noext")
    assert sid.startswith("file-")
    assert sid[0].isalnum()
