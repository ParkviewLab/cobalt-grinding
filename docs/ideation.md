# Cobalt Grinding — Ideation

**Date:** 2026-05-02
**Format:** record of the design conversation that produced the v1 plan
**Status:** ideation captured; v1 plan approved (see `docs/plan.md`)

This document records the brainstorming and design discussion that shaped Cobalt Grinding. It complements `docs/plan.md` (the implementation plan) by preserving the *reasoning*, the *alternatives considered*, and the *things deliberately deferred*. When in doubt about why a v1 decision was made, this is where the answer lives.

---

## 1. What we set out to build

A personal **LLM Wiki**, in the spirit of Andrej Karpathy's pattern: instead of stateless RAG (retrieve raw chunks every query), let an LLM **incrementally build and maintain** a structured, interlinked wiki of markdown files from heterogeneous source materials.

The pattern has three layers:

1. **Raw sources** — papers, repos, PRs, meeting notes, articles (immutable).
2. **Wiki** — markdown files (concept pages, entity pages, summaries, comparisons) that the LLM writes and updates; humans mostly read.
3. **Schema** — a document telling the LLM how to operate the wiki (naming, linking, update rules).

The shift Karpathy names: from *retrieval* to *compilation*; from *stateless* to *stateful* knowledge. When a source is added, the LLM reads it, writes a summary page, updates the index, and propagates edits across the related neighborhood (10–15 pages can be touched by a single source).

Cobalt Grinding adopts this pattern and extends it with a multi-agent architecture for ingestion, retrieval, conversation, and (later) synthesis, custodianship, and research.

---

## 2. What it should ingest

The system should accept whatever it's pointed at and figure out what to do with it.

**v1 source types (text-first):**
- Plain text and markdown: `.txt`, `.md`
- Structured config / data: `.json`, `.toml`, `.yaml`, `.xml`
- Markup: `.html`, `.css`
- Documents: `.pdf`, `.docx`, `.pages`, `.rtf`
- Source code (any language tree-sitter handles)
- Private GitHub repos (cloned via `gh`, including commits / PRs / issues)
- `.env` files (keys-only; values redacted)

**v2 (deferred — see §10):**
- Images: hand-drawn architecture, whiteboard photos, infographics, data charts
- Web crawling (vs. ingesting one URL at a time)
- CSV / TSV as structured tabular data
- Binary archives (`.zip`, `.tar.gz`, etc.)

**Per-source artifacts produced:**
- A summary page
- Lists of named entities (people, places, products, repos, apps, packages, etc.)
- Lists of named concepts, processes, taxonomies
- Lists of hard data values (with units, source pointers, confidence)
- Frontmatter linking the source to existing wiki pages

---

## 3. Preserving original structure

A key insight: the **organization of the source itself is signal** that must not be discarded at ingestion time. Different sources have different structures — repos have directory trees, papers have TOCs, slide decks have section dividers, configs have key hierarchies, email threads have reply trees — but they all reduce to a tree (sometimes a DAG) with possibly-ordered children.

**Why it matters:**
- *Locality is signal.* Two functions in one file are more related than two in different repos. Two paragraphs under the same heading are more related than two picked by a search index.
- *It's how humans navigate back.* "Section 3.2 of the Smith paper" beats "embedding 4729 of source-184."
- *It's a fallback when the LLM is wrong.* If extraction misses something, the original structure can re-derive it.

**How it's stored:**
- Each ingested source gets a `structure.json` in its raw-source folder.
- Every claim/entity/concept extracted carries a pointer like `source: smith2024.pdf#3.2.1` or `source: apps-repo@abc123:src/auth/session.ts:L42-78`.
- Path syntax: `source@version:path#fragment`.
- Repos get *two* structures recorded: directory tree (one) and import graph (the other) — they're orthogonal.

**Key principle:** original structure is preserved as **provenance**, *not* as the wiki's primary index. The wiki reorganizes around concepts; original structures provide traceability.

---

## 4. Storage substrate

The most-debated decision. Considered: pure markdown, pure Postgres, pure SQLite, LanceDB, DuckDB, hybrid.

**Conclusion: markdown is canonical, indexes are derived.**

**Why markdown wins as the source of truth:**
- Human-readable and human-editable in any editor (Obsidian, VS Code, vim).
- LLMs natively produce and modify it well.
- Git-diffable — see exactly what the agent changed, revert when needed.
- Zero infrastructure; portable.
- Schema-flexible; new frontmatter keys cost nothing.
- The day the agent gets it wrong, you `vim file.md` and fix it. No special UI needed. **This is the single most important property of the LLM-Wiki pattern.**

**Why pure database doesn't:**
- Loses human-readability and direct-editability.
- Fights the LLM, which writes markdown fluently and SQL grudgingly.
- Adds infrastructure dependencies.
- Makes the wiki harder to back up, sync, fork, or share.

**The hybrid that won: LanceDB primary index, DuckDB analytical layer (later).**

**LanceDB for the operational index:**
- Built-in hybrid search (BM25 + vector + metadata filter + RRF) — one API call.
- Designed for embeddings + metadata + FTS together.
- Time-travel (versioning) — useful for "what did the wiki think yesterday."
- Embedded; no server.
- Python SDK matches the agent layer.

**DuckDB as a parallel reader (deferred to v1.5):**
- Full SQL for ad-hoc analytics across wiki data.
- Reads Lance/Arrow/Parquet directly — same files, no double-storage.
- Best for cross-source aggregations, GROUP BY, complex JOINs.
- Single-writer rule preserved: indexer writes to LanceDB; DuckDB only reads.

**Why both rather than one:**
- LanceDB owns operational/retrieval. DuckDB owns analytics. They share substrate.
- Two engines means contributors learn both, but the cognitive cost is bounded by clean module boundaries.
- The decision is reversible: indexes are derived, so swapping engines is "rewrite the indexer," not "migrate everything."

**Other engines considered, rejected for v1:**
- *SQLite + sqlite-vec + FTS5* — bulletproof, more general-purpose, but hybrid search must be assembled by hand.
- *Postgres* — overkill until multi-user concurrent writes, multi-GB scale, or network sharing become real needs.
- *Tantivy / Elasticsearch / Meilisearch* — separate indexing infrastructure that LanceDB already provides.

---

## 5. Retrieval pipeline

Started from the user's sketch — `BM25 → top N → vector similarity → LLM reranker` — and refined.

**Refinements:**
1. **Hybrid in parallel, not sequential.** Sequential filtering cuts recall. Run BM25 and vector simultaneously; fuse with Reciprocal Rank Fusion.
2. **Metadata filter first** — frontmatter constraints (`type`, `tags`, `confidence`, `source.date`) are free with indexes and often the most decisive narrowing step.
3. **Multiple indexable surfaces per page**, queried differently: title+aliases (entity-name lookup), body (BM25), summary (embeddings), frontmatter (filters), links (graph traversal).
4. **Graph traversal is its own retrieval mode.** "Tell me about auth" → find auth-tagged pages → traverse 1-hop links. Often beats vector similarity for connected-concept queries.
5. **The LLM reranker can collapse into the answering LLM** — saves a round trip; the answerer is already doing semantic judgment.
6. **Code wants a separate retrieval path** — BM25 over identifiers + AST filters beats embedding similarity for symbol queries.
7. **Cache reranks aggressively** — `(query, candidate_set) → ranked_order`.
8. **Know when retrieval failed** — emit a *gap signal* when top scores are weak. Used by the v2 researcher to find new sources; used immediately by the sage to refuse to hallucinate.
9. **Retrieval is also a write trigger** — when the agent answers by combining N sources in a way not previously connected, *that synthesis* should become a new wiki page (otherwise the same answer is re-derived every query).

**Final shape:**
```
query
  → metadata filter (free)
  → parallel: entity-name lookup ‖ BM25 ‖ vector similarity
  → RRF fuse
  → optional 1-hop graph expansion
  → rerank or fold into answering LLM
  → cache result
  → emit "gap" if scores weak
  → emit "novel synthesis" if answer crosses sources newly → write trigger
```

---

## 6. Agentic architecture: six systems

The system isn't one agent; it's a constellation of orchestrators with specialized sub-agents. Each system has a distinct purpose, cadence, and policy.

| # | System | Purpose | Cadence | v1? |
|---|---|---|---|---|
| 1 | **Ingestion** | Source → pages | Event-driven, minutes per source | ✅ v1 |
| 2 | **Retrieval** | Query → ranked pages | Interactive, sub-second | ✅ v1 |
| 3 | **Sage** | Conversational interface | Interactive, conversational | ✅ v1 |
| 4 | **Synthesis** | Discover new connections, concepts, contradictions | Background, batch | v2 |
| 5 | **Custodian** | Find orphans, duplicates, staleness, extraordinary claims | Background, batch | v1.5 |
| 6 | **Researcher** | Propose new sources from gap signals | Event-driven | v2 |

**Each system is an orchestrator + specialized sub-agents.** Examples:

- *Ingestion sub-agents:* format-classifier, structure-extractor, chunker, summarizer, entity/concept/process/data extractors, link-resolver, frontmatter-writer, page-writer, indexer-caller.
- *Retrieval sub-agents:* query-classifier, hybrid-searcher, graph-expander, gap-detector.
- *Sage sub-agents:* intent-classifier, retriever, answerer, citation-checker, novelty-detector.
- *Synthesis sub-agents (v2):* cluster-finder, edge-proposer, taxonomy-builder, contradiction-finder, consolidator.
- *Custodian sub-agents (v1.5):* orphan-detector, duplicate-detector, staleness-checker, extraordinary-claim-flagger, dust-detector, broken-link-detector, drift-detector.
- *Researcher sub-agents (v2):* request-classifier, search-executor, candidate-evaluator, duplicate-checker, proposal-writer.

**Cross-cutting flows make the wiki *compounding* rather than *static*:**
- Sage answer → novelty-detector flags it → synthesis writes a new page.
- Retrieval gap → researcher finds source → ingestion processes it.
- Ingestion encounters citation to unknown work → researcher request.
- Custodian flags staleness → researcher seeks updated source.
- Synthesis discovers contradiction → custodian tracks resolution.

A single shared `tasks/` directory (or small event bus) lets these flows be first-class rather than emergent.

---

## 7. Cross-cutting design principles

These are baked in regardless of which subsystem is being implemented.

1. **Markdown is canonical; indexes are derived.** Any index can be `rm -rf`'d and rebuilt from the markdown. This is the test that markdown is actually the source of truth.

2. **Single writer to the corpus.** Only the ingestion subsystem writes pages. Custodian, synthesis, and researcher all *propose* via `tasks/proposals/` for human or sage review. Avoids races; preserves audit trail.

3. **Propose, don't act.** Any agent that audits, critiques, or speculates writes proposals — not direct edits. The cost of a wrong autonomous edit is much higher than the cost of a slightly cluttered wiki.

4. **Original structure is preserved as provenance.** TOC, dir tree, heading hierarchy — all captured at ingestion, never reconstructed.

5. **Confidence and provenance live at the *value* level.** A claim like `revenue_q3_2024 = 4.2M` carries source pointer, units, date, and confidence — not just the page it lives on.

6. **Schema-flexible, schema-prescriptive, schema-validated.**
   - Flexible: frontmatter accepts new keys.
   - Prescriptive: `SCHEMA.md` tells the LLM what's expected.
   - Validated: Pydantic models + indexer pass catch drift.

7. **Sage is bounded.** It's not "all-knowing"; it's "knows what's been ingested." The prompt and UX make this explicit. No claims about uningested topics.

8. **Ingestion is transactional.** Stage to a temp location, validate, atomically swap. Avoids partial-ingest corruption.

9. **Background systems are incremental.** Custodian and synthesis (when they exist) track what's changed since last run. Re-analyzing 10k pages every cycle is wasteful.

10. **Cost is multiplicative; cache aggressively.** Each ingestion involves many LLM calls. Hash-keyed caching of embeddings, summaries, and rerank results is high-value.

---

## 8. Decisions made (with reasoning)

| Decision | Why |
|---|---|
| Markdown canonical, indexes derived | Human-editable, LLM-native, git-diffable, portable, lets a human fix what the agent breaks |
| LanceDB as primary operational index | Built-in hybrid search, purpose-built for this workload, time-travel useful for dev, Python SDK match |
| DuckDB analytical layer deferred to v1.5 | Cross-cutting analytics is step-2; v1 doesn't need ad-hoc SQL |
| Python runtime, Claude Agent SDK | Best LLM/agent ecosystem, LanceDB native Python, sub-agent orchestration out of box |
| Six systems, four in v1 | Synthesis and researcher are easiest to over-promise; deliver boring-and-useful core first |
| Custodian in v1.5, not v1 | Maintenance is needed once the corpus has size; not on day one |
| "Propose, don't act" for auditors | Cost of wrong autonomous edit > cost of slight clutter |
| Single writer to corpus | Simplifies coordination; preserves audit trail |
| Image / multimodal ingestion deferred to v2 | ~80% of value at <40% of effort by going text-first |
| Original structure preserved as provenance | Locality is signal; lost at ingestion time it can't be recovered |
| Per-value confidence + provenance | Hard data from a chart is softer than from a CSV; the index needs to know |
| Sage framed as bounded, not all-knowing | The over-confident sage is exactly the LLM-hallucination failure the wiki is supposed to fix |

---

## 9. Things deliberately deferred

Naming what's *not* in v1 is as important as naming what is.

**v1.5:**
- Custodian subsystem (orphans, duplicates, staleness, drift, extraordinary claims).
- DuckDB analytical layer wired up against the same Lance files.
- Daemon mode for background custodian runs.

**v2:**
- Synthesis subsystem (constructive: emergent concepts, link discovery, taxonomy building, contradiction *discovery*).
- Researcher subsystem (gap → web/GitHub/ArXiv search → propose source).
- Image / multimodal ingestion (whiteboard, infographic, charts).
- Web crawling and recursive ingestion (vs. one-URL-at-a-time).
- CSV / spreadsheet structured ingestion.
- Claude Code skill / plugin integration.
- Multi-user concurrent writes.
- Auto-ingestion of researcher-found sources (v1 keeps human-in-loop).

**Why these specifically:**
- *Synthesis and researcher* are the most easily over-promised; they need a stable corpus and stable schema before being useful, so they wait.
- *Custodian* needs a corpus to clean; it's pointless on day one.
- *Multimodal* adds complexity disproportionate to v1 value.
- *Multi-user / daemon* are scale concerns; v1 is single-user CLI.

---

## 10. Open questions (resolve early in implementation)

1. **Embedding model.** Voyage 3 large vs. OpenAI text-embedding-3-large vs. Anthropic. Default: Voyage 3 large unless cost forces otherwise.
2. **CLI vs. daemon.** v1 is CLI-only. Daemon arrives with v1.5 custodian. Design CLI to make that path easy.
3. **Storage location.** `~/.cobalt/wiki/` default? Configurable? Multi-wiki support? v1: single wiki, configurable path.
4. **Embedding cost control.** Cache by content hash. Default: yes, content-hash-keyed.
5. **Concurrency model.** Single-process v1; daemon + worker model in v1.5+.
6. **Test corpus.** Need 10–20 diverse seed sources for development and regression. Build alongside Phase 0.
7. **Extraordinary-claim detection.** v1.5 custodian feature. Worth being honest: it requires computing a prior over the wiki's existing knowledge — start simple (numeric outliers, single-source claims with high confidence, strong-language qualifiers) and get fancier only if simple isn't enough.
8. **Auto-ingestion threshold.** v2 researcher: when does it propose vs. auto-add? Default for v2: always propose; auto-add is v3 (if ever).

---

## 11. Naming conventions surfaced during ideation

- **Cobalt Grinding** — the project itself.
- **The wiki** — the corpus of markdown pages plus its indexes.
- **Sage** — the conversational interface (consciously *not* "all-knowing"; it's bounded).
- **Custodian** — the maintenance auditor (not a deleter).
- **Researcher** — the gap-driven source acquirer (not an auto-ingester).
- **Synthesis** — constructive graph analysis (additive).
- **Source-id** — stable identifier for an ingested source; survives re-ingestion of the same source.
- **Source pointer** — `source@version:path#fragment` syntax.
- **Page** — a markdown file in `wiki/pages/` with frontmatter.
- **Claim** — a structured assertion attached to a page, with value, unit, confidence, and source pointer.

---

## 12. What the conversation surfaced that didn't fit a section above

A few observations that informed the design but didn't end up in any one section:

- **"Extraordinary claims require extraordinary proof"** as a literal heuristic in the custodian — claims that violate the wiki's prior get flagged for additional sources before being propagated.
- **The contradiction detector is one of the highest-value behaviors of the whole system** — but only if it captures the *axis* of disagreement, not just the fact of it.
- **Code-prose contradictions are valuable bug indicators** — "the README says X, the code does Y" is exactly where bugs and stale docs live.
- **Hard data from charts is softer than hard data from CSVs.** The two-tier confidence model (`source_type: chart_estimate` vs. `source_type: tabular`) lets the system reconcile them correctly when the same value appears twice.
- **A single source can touch 10–15 wiki pages.** This is what makes ingestion expensive but also what makes the wiki interconnected. It also implies ingestion needs to be transactional — half-applied edits across 10 pages is much worse than half-applied edits to one.
- **Repos have two parallel structures** (directory tree + import graph), and neither subsumes the other.
- **`.env` files are interesting** — they describe the *shape* of a project (what services it talks to, what flags exist) while their values are secrets. v1 treats them as keys-only.

---

## 13. The path from ideation to plan

The plan in `docs/plan.md` is the v1 cut of all of the above. Mapping:

- §2 (source types) → Phases 2, 5, 6 of the plan.
- §3 (original structure) → `structure_extractor` sub-agent + `raw/<source-id>/structure.json`.
- §4 (storage) → Phases 0–1 of the plan; LanceDB choice locked in.
- §5 (retrieval) → Phase 3 of the plan.
- §6 (agentic systems) → Plan implements Ingestion (Phase 2), Retrieval (Phase 3), Sage (Phase 4); Custodian/Synthesis/Researcher noted as deferred.
- §7 (principles) → embedded throughout the plan.
- §9 (deferred) → "OUT" section + Phase 7+ stub of the plan.
- §10 (open questions) → carried directly into the plan's "Open questions" section.

When the plan needs revisiting, this document is the trail back to *why*.
