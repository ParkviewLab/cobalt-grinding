<!--
SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# The architecture: the reasoning, the alternatives and the directions set aside

This file is the second half of the record of cobalt-grinding's architecture. The line between the two is what a reader needs: [`architecture.md`](architecture.md) describes what the code does and how it is put together, and this file keeps what is needed only to reopen the design, which is the reasoning of the design conversation of 2026-05-02 that produced the first plan, the alternatives it weighed and the directions it set aside, together with that plan's work plan as it was last revised on 2026-05-17. The decisions made since, in the plan and in the code, are in [`decisions.md`](decisions.md). Entries are in date order and the record at the foot summarises them.

It is a record, so it describes the past and keeps doing so (the handbook's [documents and records](https://github.com/ParkviewLab/handbook/blob/main/docs/documentation.md#documents-and-records)). An open question does not belong here; one about the architecture as it stands goes to [`in-flight_ideas.md`](in-flight_ideas.md). The conversation's names differ from the code's: its Ingestion, Retrieval and Sage are Ingest, Retrieve and Converse, and its Synthesis, Custodian and Researcher are Cogitate, Curate and Research, names the plan adopted the same day; its wiki is the Smalt, named on 2026-05-05.

## 2026-05-02: a wiki compiled by a model

The aim was a personal wiki in the pattern Andrej Karpathy described: instead of retrieving raw chunks at every query, as stateless retrieval-augmented generation does, a language model builds and maintains, one source at a time, a structured, interlinked wiki of Markdown files drawn from heterogeneous sources. The pattern has three layers: the raw sources (papers, repositories, pull requests, meeting notes, articles), which are immutable; the wiki, pages of concepts, entities, summaries and comparisons, which the model writes and updates and people mostly read; and a schema, a document telling the model how to keep the wiki (naming, linking, rules of update). The shift it names is from retrieval to compilation, and from stateless to stateful knowledge: when a source is added, the model reads it, writes a summary page, updates the index and carries its edits across the related neighbourhood, so that one source can touch ten to fifteen pages. Cobalt Grinding adopted the pattern and extended it with several agentic systems, for ingestion, retrieval and conversation first, and later for synthesis, custodianship and research.

## 2026-05-02: what to ingest, and the structure of sources

The system was to accept whatever it was pointed at and work out what to do with it. The first version was to be text first: plain text and Markdown; structured configuration and data (`.json`, `.toml`, `.yaml`, `.xml`); markup (`.html`, `.css`); documents (`.pdf`, `.docx`, `.pages`, `.rtf`); source code in any language tree-sitter handles; private GitHub repositories, cloned through `gh` with their commits, pull requests and issues; and `.env` files, keys only, with their values redacted. Deferred to a second version: images (hand-drawn architecture, photographs of whiteboards, infographics, charts), crawling the web rather than ingesting one URL at a time, CSV and TSV as tabular data, and binary archives. Each source was to yield a summary page, its named entities (people, places, products, repositories, applications, packages), its named concepts, processes and taxonomies, its hard data values with units, source pointers and confidence, and frontmatter linking it to existing pages.

A key point of the conversation was that the organisation of a source is itself signal and must not be discarded at ingestion. Sources differ in structure (a repository has a directory tree, a paper a table of contents, a slide deck its dividers, a configuration its key hierarchy, an e-mail thread its reply tree), but all reduce to a tree, sometimes a directed acyclic graph, with possibly ordered children. It matters for three reasons: locality is signal (two functions in one file are more related than two in different repositories, and two paragraphs under one heading more than two found by a search index); it is how people find their way back ("section 3.2 of the Smith paper" rather than "embedding 4729 of source 184"); and it is a fallback when the model is wrong, since the original structure can re-derive what extraction missed. The plan of storage was a `structure.json` beside each raw source, a pointer on every claim, entity or concept of the form `source@version:path#fragment` (`smith2024.pdf#3.2.1`, `apps-repo@abc123:src/auth/session.ts:L42-78`), and two structures for a repository, its directory tree and its import graph, which are orthogonal. The principle: original structure is kept as provenance, not as the wiki's primary index; the wiki is organised around concepts, and the original structures give traceability.

## 2026-05-02: Markdown as the store of record, LanceDB as the index

The storage substrate was the most debated question. The options weighed were pure Markdown, pure Postgres, pure SQLite, LanceDB, DuckDB and a hybrid. The conclusion: Markdown is canonical, and every index is derived.

Markdown won as the source of truth because it can be read and edited by a person in any editor (Obsidian, VS Code, vim); language models produce and modify it well; it is diffable in git, so what the agent changed can be seen and reverted; it needs no infrastructure and is portable; it accepts new frontmatter keys at no cost; and on the day the agent gets something wrong, a person opens the file and fixes it, with no special interface. That last property was judged the most important of the whole pattern. A database as the store of record was set aside because it loses that readability and direct editing, works against a model that writes Markdown fluently and SQL grudgingly, adds infrastructure, and makes the wiki harder to back up, synchronise, fork or share.

The hybrid chosen: LanceDB as the operational index, and DuckDB as an analytical layer later. LanceDB offers hybrid search in one call (BM25, vectors, metadata filters and fusion by reciprocal rank), is designed for embeddings, metadata and full-text search together, keeps versions (useful for asking what the wiki thought yesterday), is embedded with no server, and has a Python SDK matching the agent layer. DuckDB, deferred, would give full SQL for analysis across the wiki's data, reading the Lance, Arrow and Parquet files directly with no second copy, for aggregations across sources and complex joins, while keeping a single writer: the indexer writes to LanceDB and DuckDB only reads. Two engines were preferred to one because each owns a role (operations and retrieval, analytics) over a shared substrate; the cost of learning both is bounded by clean module boundaries; and the choice is reversible, since an index is derived and changing engines means rewriting the indexer, not migrating the data.

Set aside for the first version: SQLite with `sqlite-vec` and FTS5, robust and more general, because hybrid search would have to be assembled by hand; Postgres, too much until concurrent writes by several users, gigabytes of data or sharing over a network become real needs; and Tantivy, Elasticsearch and Meilisearch, separate indexing infrastructure that LanceDB already provides.

## 2026-05-02: retrieval

The conversation started from a sketch of sequential retrieval (BM25, then the top N ranked by vector similarity, then a reranking model) and refined it:

1. Hybrid in parallel, not in sequence: filtering in sequence loses recall, so BM25 and vector search run together and their rankings are fused by reciprocal rank.
2. The metadata filter first: constraints on frontmatter (`type`, `tags`, `confidence`, the source's date) cost nothing with indexes and are often the most decisive narrowing.
3. Several surfaces of each page, queried differently: the title and aliases for lookups of entity names, the body for BM25, a summary for embeddings, the frontmatter for filters, and the links for traversal.
4. Graph traversal as a retrieval mode of its own: for "tell me about auth", finding the pages tagged with auth and following their links one hop often beats vector similarity on questions about connected concepts.
5. The reranker folded into the answering model, which saves a round trip, the answering model already judging meaning.
6. A separate path for code: BM25 over identifiers with filters on the syntax tree beats embedding similarity for questions about symbols.
7. Reranking results cached aggressively, keyed by the query and the set of candidates.
8. Knowing when retrieval failed: a gap signal when the top scores are weak, for the researcher to find new sources and for the conversational agent to refuse to invent an answer.
9. Retrieval as a trigger for writing: an answer that combines sources in a way not connected before should become a page of its own, or the same answer is derived again at every query.

The resulting shape: the query, the metadata filter, a lookup of entity names, BM25 and vector similarity in parallel, fusion, an optional expansion of one hop along the graph, reranking or its folding into the answer, a cached result, a gap signal on weak scores, and a signal of novel synthesis when an answer crosses sources in a new way.

## 2026-05-02: six systems, each an orchestrator

The system was conceived not as one agent but as a constellation of orchestrators with specialised sub-agents, each system with its own purpose, cadence and policy:

| System | Purpose | Cadence | Version |
|---|---|---|---|
| Ingestion | a source becomes pages | on events, minutes per source | 1 |
| Retrieval | a query becomes ranked pages | interactive, under a second | 1 |
| Sage | the conversational interface | interactive, conversational | 1 |
| Synthesis | new connections, concepts and contradictions | in the background, in batches | 2 |
| Custodian | orphans, duplicates, staleness, extraordinary claims | in the background, in batches | 1.5 |
| Researcher | new sources proposed from gap signals | on events | 2 |

Their sub-agents as sketched: for ingestion, a classifier of formats, an extractor of structure, a chunker, a summariser, extractors of entities, concepts, processes and data, a resolver of links, a writer of frontmatter, a writer of pages and a caller of the indexer; for retrieval, a classifier of queries, a hybrid searcher, a graph expander and a gap detector; for the Sage, a classifier of intent, a retriever, an answerer, a checker of citations and a detector of novelty; for synthesis, a finder of clusters, a proposer of edges, a builder of taxonomies, a finder of contradictions and a consolidator; for the custodian, detectors of orphans, duplicates, staleness, extraordinary claims, dust, broken links and drift; for the researcher, a classifier of requests, an executor of searches, an evaluator of candidates, a checker of duplicates and a writer of proposals.

The flows between the systems were what would make the wiki compound rather than sit still: an answer the detector of novelty flags becomes a page written by synthesis; a gap in retrieval leads the researcher to a source that ingestion processes; a citation of an unknown work met in ingestion becomes a request to the researcher; staleness the custodian flags sends the researcher for a newer source; and a contradiction synthesis finds is tracked by the custodian until resolved. A shared `tasks/` directory, or a small event bus, was to make these flows explicit rather than emergent.

## 2026-05-02: principles held across the systems

1. Markdown is canonical and indexes are derived: any index can be deleted and rebuilt from the Markdown, which is the test that Markdown really is the source of truth.
2. A single writer to the corpus: only ingestion writes pages; the custodian, synthesis and the researcher propose, through `tasks/proposals/`, for a person or the Sage to review. This avoids races and keeps an audit trail.
3. Propose, don't act: an agent that audits, critiques or speculates writes proposals, not edits, since a wrong autonomous edit costs much more than a slightly cluttered wiki.
4. Original structure kept as provenance: tables of contents, directory trees and heading hierarchies captured at ingestion, never reconstructed.
5. Confidence and provenance for each value: a claim such as `revenue_q3_2024 = 4.2M` carries its source pointer, units, date and confidence, not only the page it sits on.
6. Schemas flexible, prescriptive and validated: the frontmatter accepts new keys; `SCHEMA.md` tells the model what is expected; Pydantic models and the indexer's pass catch drift.
7. The Sage is bounded: it knows what has been ingested, not everything, and its prompt and interface say so; it makes no claims about topics not ingested.
8. Ingestion is transactional: staged in a temporary location, validated, then swapped in atomically, so that no ingest is left half applied.
9. The background systems are incremental: the custodian and synthesis track what changed since their last run rather than analysing every page each time.
10. Cost is multiplicative, so caching is aggressive: each ingestion involves many model calls, and caching embeddings, summaries and rerankings by hash is of high value.

## 2026-05-02: what was deferred, and why

Naming what the first version would not do was held to be as important as naming what it would. For version 1.5: the custodian; the DuckDB layer over the same Lance files; and a daemon for the custodian's background runs. For version 2: synthesis (emergent concepts, discovery of links, taxonomies, discovery of contradictions); the researcher (from a gap to a search of the web, GitHub or arXiv to a proposed source); images and other multimodal sources; crawling the web and ingesting recursively; structured ingestion of CSV and spreadsheets; a Claude Code skill or plugin; concurrent writes by several users; and ingestion of the researcher's sources without a person's approval. The reasons: synthesis and the researcher are the easiest to over-promise, and need a stable corpus and schema before they are useful; the custodian needs a corpus to clean and is pointless on the first day; multimodal sources add complexity out of proportion to their value in the first version; and several users or a daemon are concerns of scale, while the first version was a command line for one user.

## 2026-05-02: what the conversation noticed

Observations that informed the design without fitting one section:

- "Extraordinary claims require extraordinary proof" as a literal heuristic for the custodian: a claim that violates the wiki's prior is flagged for more sources before it spreads.
- The detector of contradictions is among the most valuable behaviours of the whole system, but only if it captures the axis of the disagreement, not merely the fact of it.
- Contradictions between code and prose are good indicators of bugs: "the README says X, the code does Y" is exactly where bugs and stale documents live.
- Hard data read from a chart is softer than hard data from a CSV file; two tiers of confidence (`source_type: chart_estimate` and `source_type: tabular`) let the system reconcile the two when the same value appears twice.
- One source can touch ten to fifteen pages. That makes ingestion expensive, but it is also what interconnects the wiki, and it is why ingestion must be transactional: edits half applied across ten pages are much worse than a half-applied edit to one.
- A repository has two parallel structures, its directory tree and its import graph, and neither subsumes the other.
- `.env` files describe the shape of a project (the services it talks to, the flags it has) while their values are secrets, so they were to be read as keys only.

## 2026-05-02: the questions left open, and where they went

The conversation ended with eight questions to settle early in implementation. The plan and the code answered five ([`decisions.md`](decisions.md)):

- The embedding model: the conversation's default was Voyage 3 large, against OpenAI's text-embedding-3-large and Anthropic's; the plan chose local embeddings with fastembed the same day.
- A command line or a daemon: the conversation had a command line only, with a daemon arriving for the custodian in version 1.5; the plan made one daemon the architecture from its first milestones, the same day.
- The storage location: the conversation proposed `~/.cobalt/wiki/`, configurable, with one wiki in the first version; the plan chose a visible directory in the Documents folder, configurable.
- The model of concurrency: the conversation had one process in the first version and a daemon with workers later; the plan chose a daemon with a thread pool from the start.
- The threshold for ingesting automatically: the researcher always proposes, and adding automatically was left to a later version, if ever; the plan decided the same (Research proposes only).

The other three are open in [`in-flight_ideas.md`](in-flight_ideas.md): a seed corpus of ten to twenty diverse sources for development and regression tests; the detection of extraordinary claims, which requires a prior over what the wiki already holds and was to start simply (numeric outliers, single-source claims held with high confidence, strong qualifiers in the language) and grow only if the simple version did not suffice; and controlling the cost of embeddings by caching them on content hashes, which has been smalt-mcp's concern since the Smalt's embeddings moved there on 2026-05-17.

## 2026-05-17: the plan as it was written

The plan the project was built from, `docs/plan.md`, was written on 2026-05-02, the day of the conversation above, and revised in seventeen further commits until 2026-05-17; after that only its links changed, until it was divided on 2026-09-27. Its work plan follows as it then stood: the milestones with their done-when lists, the planned layout of the repository and of the two stores, and the verification table. It is kept as the record of how the project was planned and built, not as a description of the code: what the code does is in [`architecture.md`](architecture.md), the plan's decisions are in [`decisions.md`](decisions.md), and what it planned and the code has not built stays open in [`in-flight_ideas.md`](in-flight_ideas.md), whose entries for M6 to M9+ point back here.

The text is the plan's own, with three changes: its dashes are replaced by other punctuation, its bold type is removed, and its headings are set one level lower; its code blocks are unchanged. It uses the names of 2026-05-17, so `cogrind-workshop --status`, `--index` and the like are the command-line client as then planned, and the naming note that opened the plan, below, explains the older names. The other sections of the plan that the milestones refer to (Cross-system flows, the Ingest subsystem, Proposal document shape and lifecycle, Apply-time post-mortem) were not part of its work plan; their substance is in `in-flight_ideas.md` and `architecture.md`, and their decisions in `decisions.md`.

### The naming note

> Naming note (post-M2.7): Earlier drafts of this plan named the daemon `cogrindd` and a sibling CLI `cogrind`. The M2.7 cleave collapsed that into one binary per repo: the daemon is now `cobalt-grinding` (shipped here); the CLI was extracted to the sibling repo `cogrind-workshop`. Historical references to `cogrind init` / `cogrind status` / `cogrind mcp serve` describe the old combined-CLI surface. Those verbs were dropped during the cleave; their function lives in `cogrind-workshop --status` / `wiki.*` MCP tools / the `cobalt-grinding` daemon binary itself.

### Implementation milestones

Phase 1 covers M0 to M5 (Bootstrap → Indexer → Daemon shape → Ingest → Retrieve → Converse). Phase 2 begins at M6: Research, then Cogitate (M7), then Curate (M8). Each milestone ends in a demoable state. Milestones are roughly sequential; some can overlap.

#### M0: Bootstrap (foundations, no agents yet)

> Substrate note. As of M2.7 (smalt-mcp v0.5.0+) and the workstream-B ebony-enriching launch, cobalt-grinding ships zero storage layer. The "Pydantic models / SCHEMA.md / LanceDB schema" work in M0 now lives in the smalt-mcp repo (the canonical-knowledge substrate) and the ebony-enriching repo (the lab notebook). M0 here is downgraded to "bootstrap-as-MCP-host": cobalt-grinding wires up its MCP children, runs their `bootstrap` tools, and brings up its own thin scaffolding.

- Repo skeleton: `src/cobalt_grinding/` Python package (cognitive systems + MCP host; no storage layer; no schema models).
- MCP children configured under `[mcp.clients.*]`: `smalt-mcp` (SMALT_DIR-backed), `ebony-enriching` (EBONY_ENRICHING_DIR-backed), `deco-assaying` (stateless). cobalt-grinding autostarts all three on first run.
- Substrate bootstrap on startup: cobalt-grinding calls `smalt.bootstrap()` to materialize SMALT_DIR's canonical layout (`pages/`, `schema/SCHEMA.md`, `schema/POLICY.md`, `index/lance/` tables) and `ebony.bootstrap()` to materialize EBONY_ENRICHING_DIR's canonical layout (`proposals/{schema,cogitate,curate,research,toolsmith,converse}/`, `experiments/`, `gaps.md`, `schema/SCHEMA.md`, `schema/POLICY.md`). Both calls are idempotent.
- Schema work (page-type models, `ProposalPage` with lifecycle states, falsifiability + cost-tier rules) is owned by the substrate repos, not by cobalt-grinding. SCHEMA.md/POLICY.md placeholders ship with each substrate's bootstrap; humans + Claude seed them on Day 0; thereafter they're groomed via the proposal-as-hypothesis loop.
- Basic CLI shell: `cogrind-workshop --status` (queries `smalt.status` + `ebony.status` via the daemon).
- MCP server scaffolding (the daemon binary itself): empty `wiki.*` tool registry; tools are added in later phases as each cognitive system comes online.
- Test fixtures: a tiny seed Smalt + a tiny seed EbonyEnriching for cross-substrate scenario tests.

Done when: `cobalt-grinding` starts cleanly with all three MCP children autostarted; `smalt.bootstrap()` materializes the canonical SMALT_DIR layout; `ebony.bootstrap()` materializes the canonical EBONY_ENRICHING_DIR layout; `cogrind-workshop --status` reports both substrates green; the daemon binary itself accepts an MCP client connection (no `wiki.*` tools yet; added per cognitive system).

#### M1: Indexer (single-shot, in-process)

The indexer turns markdown pages into queryable LanceDB rows. M1 ships it as a single-shot CLI command; the daemon shape comes in M2 and lifts this work into a long-running process. The indexer's code is written with strict discipline (no module-level globals, all state passed as arguments, lazy resource construction) so M2 can wrap it as a daemon task without redesign.

- Walk `smalt/pages/`, parse frontmatter, validate against schema
- Compute content hash, populate LanceDB `pages`, `links`, `claims` tables
- Generate embeddings using fastembed with `BAAI/bge-small-en-v1.5` (384-dim) by default: local, ONNX-quantized, no API keys, no per-token cost, runs offline. Provider is configurable; hosted alternatives (Voyage, OpenAI) are supported via the `[embedding]` config block but not the default. (Cold-loaded each invocation in M1; fixed in M2.)
- FTS index on body + title
- HNSW index on embeddings
- Incremental: only re-process files whose `content_hash` has changed
- `cogrind-workshop --index [--full]` CLI command (synchronous, blocks until done)

Done when: hand-write 3 markdown pages → `cogrind-workshop --index` → LanceDB has the rows + embeddings + FTS + HNSW; modify one page → `cogrind-workshop --index` → only that page reprocesses; querying LanceDB directly returns the indexed pages. The indexer code follows the no-globals discipline so M2 can lift it without refactor.

#### M2: Daemon + CLI split

The big architectural slice for Phase 1: split CoGrind into two binaries with a single MCP protocol between them, and lift M1's indexer behind the daemon's tool handler.

Two binaries, one protocol, no shared business logic:

- `cobalt-grinding`: the daemon. New entry point. Long-running. Hosts the MCP server, the task scheduler, the worker pool, and all Smalt business logic.
- `cogrind-workshop`: the CLI (sibling repo, extracted at M2.7). Pure MCP client. No business logic; no `App`; no in-process work. Calls the daemon's `wiki.*` tools and renders results. Lives in [`ParkviewLab/cogrind-workshop`](https://github.com/ParkviewLab/cogrind-workshop); this repo no longer ships a CLI binary.

Daemon (`cobalt-grinding`):

- `src/cobalt_grinding/daemon/main.py`: entry point: `cobalt-grinding`. Loads config, runs the bootstrap step, starts the MCP server (HTTP transport for daemon clients; stdio variant for child-process clients like Claude Desktop), starts the task scheduler.
- `src/cobalt_grinding/app.py`: `App` class holding shared resources: `Config`, fastembed model, LanceDB connection, LLM client (constructed lazily). All subsystems take an `App` as a dependency; no module-level globals.
- `src/cobalt_grinding/daemon/scheduler.py`: task scheduler + worker pool. Asyncio event loop + `concurrent.futures.ThreadPoolExecutor`. A `Task` model (`task_id`, `kind`, `status` ∈ {queued, running, succeeded, failed, cancelled}, `progress`, `result`, `error`, `submitted_at`, `started_at`, `finished_at`).
- `src/cobalt_grinding/daemon/bootstrap.py`: first-run Smalt initialization. On startup, if the configured `smalt_dir` is empty/missing, the daemon creates the canonical layout, drops in fresh `SCHEMA.md` / `POLICY.md` templates, and creates the empty LanceDB tables. Replaces the old `cogrind init` CLI verb (the old daemon-startup pattern, now obsolete).
- `src/cobalt_grinding/daemon/mutex.py`: single-writer corpus mutex (used by M3+ ingest workers).
- Refactor M1's indexer to take an `App` and run as a daemon task via the `wiki.index` MCP tool. The Indexer class itself doesn't change: it's already no-globals, args-in-constructor.
- MCP tools registered in M2: `wiki.index`, `wiki.status`, `wiki.task_status`, `wiki.task_list`, `wiki.task_cancel`. Subsequent milestones add `wiki.ingest` (M3), `wiki.search` (M4), `wiki.ask` (M5), and the Phase 2 tools.

CLI (`cogrind-workshop`):

- (cogrind-workshop sibling repo) `src/cogrind_workshop/main.py`: entry point: `cogrind-workshop`. Click for flags and args.
- `src/cobalt_grinding/cli/client.py`: thin MCP-over-HTTP client. Discovers a running `cobalt-grinding`, calls tools, optionally tails task progress.
- `src/cobalt_grinding/cli/formatters.py`: render task results, status tables, gap reports, etc. for a terminal.
- Initial flag-style interface (M2-era): `cogrind-workshop --status`, `cogrind-workshop --index`, `cogrind-workshop --ingest /path` (M3 onwards). One verb per invocation. Long-running operations show live task progress.
- Future: an interactive REPL (`cogrind-workshop` with no flags): same shape as `claude`. Out of M2 scope.
- No daemon → clear error: every Smalt-operation flag fails with a friendly message ("no `cobalt-grinding` running; start one with `cobalt-grinding &`").

`pyproject.toml` entry points (post-M2.7 cleave: one binary per repo, not two from this one):

```toml
# cobalt-grinding/pyproject.toml — the daemon binary only
[project.scripts]
cobalt-grinding = "cobalt_grinding.daemon.main:run"

# cogrind-workshop/pyproject.toml — the CLI binary (sibling repo)
[project.scripts]
cogrind-workshop = "cogrind_workshop.main:main"
```

What gets removed in M2:

- `cogrind init` CLI verb: replaced by daemon-startup auto-init (M2.7+ this lives entirely in smalt-mcp's `bootstrap` tool).
- `cogrind status` CLI verb: replaced by the `wiki.status` MCP tool, called by `cogrind-workshop --status`.
- `cogrind mcp serve` CLI verb: the daemon binary `cobalt-grinding` is the MCP server itself; no "serve" subcommand needed.
- The original `cogrind/cli.py` + `cogrind/commands/` flat structure: replaced by a daemon-only `src/cobalt_grinding/daemon/` subpackage, with the CLI extracted entirely to the cogrind-workshop sibling repo at M2.7.

What stays (and is just relocated or reused):

- `src/cobalt_grinding/ingest/`, `src/cobalt_grinding/retrieve/`, `src/cobalt_grinding/converse/`: cognitive-system business logic. The daemon's `wiki.*` tool handlers call into these; the CLI never does.
- `src/cobalt_grinding/config.py`: same loader; both binaries read the same config layers.
- Storage layer is OUT (post-cleave: smalt-mcp v0.5.0+). The Smalt schema, indexer, and LanceDB plumbing now live in smalt-mcp; the lab-notebook schema lives in ebony-enriching. cobalt-grinding's `wiki.index` tool handler is a thin shim that calls `smalt.bootstrap` (which auto-runs the indexer on every write). Cobalt-grinding ships no `src/cobalt_grinding/storage/` or `src/cobalt_grinding/schema/` packages anymore.

Done when:

1. `cobalt-grinding` starts cleanly; pointed at an empty / missing Smalt dir, it auto-creates the canonical layout (the M0 layout, plus M1's tables) without needing any prior CLI invocation.
2. `cogrind-workshop --status` from a separate shell connects over HTTP MCP and returns the daemon's view of the Smalt (tables, page counts, scheduler state).
3. `cogrind-workshop --index` over MCP runs M1's indexer as a daemon task; second invocation is visibly faster than the first (warm fastembed + LanceDB connection).
4. `wiki.task_status(task_id)` returns live state for an in-flight indexer run; `wiki.task_cancel(task_id)` interrupts a running task cleanly.
5. `cogrind-workshop --status` (or any Smalt-operation flag) with no daemon running fails with a clear error pointing at `cobalt-grinding`.
6. Multiple concurrent MCP requests are handled correctly: e.g., a long-running `wiki.index` task and several quick `wiki.task_status` calls interleave without serializing.
7. Single-writer mutex test: two concurrent ingest-equivalent operations serialize on the corpus-write step.
8. Claude Desktop can connect (via either stdio child or HTTP) and successfully call `wiki.status` / `wiki.index` as it would any other MCP server.

#### M2.5: Daemon as MCP host + agent runtime

M2 made the daemon an MCP server (it answers requests). M2.5 makes it an MCP host (it also makes them) *and* gives it an internal agent runtime that runs the LLM tool-use loop on behalf of CoGrind's own subsystems. Lands before M3 so M3's ingest sub-agents are written against `app.host.run_agent(...)` natively.

The line this milestone draws, capability vs. infrastructure:

- Capabilities: anything CoGrind invokes against *external content* (parse this file, extract text from this PDF, search the web, fetch this URL, OCR this image). These run as MCP child servers; their tools are discovered by the host at startup, indexed for retrieval, and reach the LLM via the agent runtime. Adding a new file type or a new search backend later is a new MCP child server in someone's config, not a code change in CoGrind core.
- Infrastructure: anything CoGrind uses internally to maintain *its own state* (LanceDB queries, embedder calls, page writes, the LLM client). These stay direct imports. Forcing them through MCP would pay serialization cost on hot paths for no extensibility benefit, since they're not extension points.

This line is what makes ingest growth tractable. Five of CoGrind's six subsystems are agentic; ingest in particular will keep meeting new file types (`.docx`, `.epub`, `.org`, `.rst`, `.ipynb`, image OCR, table extraction, archives, audio transcription, …). Each one is a parser + extractor capability. Shipping all of them inside CoGrind core means every new format needs a CoGrind release. Shipping each as an MCP child server means a new format is a new package, configured into `[mcp.clients.<name>]`, picked up automatically by the host. Different language ecosystems (a Rust PDF library, a Go archive walker) can contribute parsers without growing CoGrind's polyglot dependency tree.

Two intertwined capabilities `cobalt-grinding` gains:

1. MCP host (client side): spawns and supervises configured child MCP servers; collects their tools at handshake; dispatches tool calls to the right child.
2. Agent runtime: exposes `await app.host.run_agent(system, messages, ...)` to CoGrind's own subsystems. Implementation runs the full LLM tool-use loop (LLM → `tool_use` → MCP child → `tool_result` → LLM → … → `end_turn`). Agents never see MCP plumbing or do tool selection themselves.

CoGrind core ships zero MCP children. The first child CoGrind's M3 ingest will configure is [`deco-assaying`](https://github.com/ParkviewLab/deco-assaying) (separate project), but it's not built or tested as part of M2.5; M2.5's integration test uses an in-tree stub MCP server.

Agent API (what CoGrind's own subsystems call):

```python
# A summarizer agent
response = await app.host.run_agent(
    system=SUMMARIZER_PROMPT,
    messages=[{"role": "user", "content": f"Summarize:\n{body}"}],
)

# A code handler agent — same shape; the host picks deco-assaying tools by retrieval
response = await app.host.run_agent(
    system=CODE_HANDLER_PROMPT,
    messages=[{"role": "user", "content": f"Analyze {file.name}."}],
)
```

That's the whole surface agents see. No `allowed_tools`, no tool registry lookups, no MCP types. Tool selection is the host's job (see *Tool selection* below).

Tool selection (hybrid retrieval over a `tools_index`, reusing M1 infrastructure):

1. Tools-index, populated at daemon startup. When `McpClientManager` finishes handshakes with each `[mcp.clients.*]` child (MCP `tools/list`), the host writes every discovered tool's `{prefixed_name, description, input_schema, owning_child}` into a LanceDB table `tools_index`. Description is embedded via the existing fastembed embedder; FTS index built on description. Same plumbing pattern as `smalt-mcp`'s LanceDB tables: different table, same patterns. (Note: this `tools_index` LanceDB lives inside `cobalt-grinding`'s own state dir, not in SMALT_DIR or EBONY_ENRICHING_DIR; it's about cobalt-grinding's own routing, not corpus content.) Refreshed on supervisor events (child crashed → entries removed; child restored → re-indexed).

   Note: cobalt-grinding's own `wiki.*` MCP server-side tools are *not* in `tools_index`. Those are served to external clients (CLI, Claude Desktop). The tools index covers *child capabilities* the host's own agents might use.

2. At agent invocation, hybrid BM25 + vector search. The host takes the latest user turn (or a synthesized agent-purpose string; small experiment) and queries `tools_index`:

   ```
   query
     ↓
   BM25 over description ‖ vector similarity over description embedding
     ↓
   RRF fuse → top-K (default K=10, configurable)
     ↓
   LLM `tools` parameter for this call
   ```

   Identical pattern to how Retrieve will work over Smalt pages in M4: same code path, different corpus. Same pattern bronze-scribing already proves out.

3. Render and dispatch. Top-K tools go straight into Anthropic's `messages.create(tools=[...])` (same `{name, description, input_schema}` shape; zero translation). On `tool_use` blocks, the host strips the prefix, dispatches to the owning child via `McpClientManager`, feeds `tool_result` back into the loop. Loop until `end_turn` or iter cap.

Why this shape is right (and less code than caller-declared globs would have been):

- The retrieval infrastructure is already built. M1 ships LanceDB + fastembed + FTS + hybrid query patterns. Pointing a new table at the same machinery is a small Δ, not a new system.
- Agents are simpler: describe the work, hand it to the host. No coupling between agent code and the tool registry. New tool added → agents pick it up automatically through retrieval.
- Open-ended agents (e.g. future Research) work without special-casing: same retrieval picks relevant tools whether the agent knows what's available or not.
- Scales naturally from 5 tools to 500 with no behavior change. No "static now, semantic later" two-phase migration.
- Same pattern Retrieve will use over pages in M4. Building it once for tools means M4 inherits the muscle.

Future seams (deferred):

- `must_include` pin: for agents that need a specific tool regardless of search rank.
- LLM-router meta-tool (Goose-style `find_tools` the LLM can invoke mid-conversation) if K=10 ever proves too small.
- Per-agent permission filter: hard cap on what an agent may invoke. Applied *before* retrieval as a denylist on `tools_index`. Not needed in M2.5; not blocked.
- Agent-declared toolkits, system-curated over time. Today the host picks tools per-call by retrieving against the latest user message. Future SME agents (defined as markdown documents: role + domain + policy + prompt + toolkit) will carry a declared minimum toolkit: the positive list of tools the agent is expected to use to do its job. The host merges declared + retrieved (declared always present; retrieval supplies extras). The toolkit is initially human-authored; over time it's groomed by three Phase-2/3 systems working in concert:
  - Cogitate proposes adding existing tools the agent has been *observed* to need but didn't declare.
  - Toolsmith (Phase 3, the 7th system) proposes adding *new* tools (finding existing MCP servers or specifying ones to be built) when the agent needs a capability nothing in the inventory provides.
  - Curate flags declared-but-never-used tools for removal.
  
  The agent definitions themselves become living artifacts the system grooms: the same self-evolution discipline CoGrind applies to the Smalt, applied to its own agent roster. This generalizes the `must_include` pin (one tool → a managed list) and is what makes "define a new agent" be "write a markdown document," not "write code." Not in M2.5; the load-bearing path remains retrieval-driven for now. Don't hard-code agent-specific tool lists in the meantime; keep the migration path clean.
- Streaming response shape over MCP / HTTP for *external* agents. M2.5's host API is in-process; an HTTP `/v1/messages` endpoint is later.
- Provider implementations beyond Anthropic.

Patterns borrowed from Goose (which we studied as a precedent, but didn't use as a library, since it's Rust + opinionated as a coding-agent CLI):

- Per-session agent state. Each `host.run_agent(...)` call gets its own conversation history; multiple agents run concurrently. Maps onto cobalt-grinding's Scheduler: each agent invocation is one task.
- Streaming events. As the loop runs, the host emits progress events (`tool_use_started`, `tool_result_received`, `assistant_delta`, `end_turn`) through the existing scheduler progress channel into `wiki.task_status`. Already-existing infrastructure; we just add new event kinds.
- Provider abstraction. A thin `LLMProvider` interface (Anthropic in M2.5; future: OpenAI, OpenRouter, etc.). CoGrindd config picks one. Mirrors Goose's 15+ providers without ourselves implementing 15+.
- What we deliberately *don't* copy from Goose: its Tool Router (preview, Databricks-only). We build our own tool-selection using LanceDB hybrid retrieval, which we already have.

Child MCP server supervision (`src/cobalt_grinding/daemon/mcp_clients.py`): `McpClientManager`. Eager spawn at `cobalt-grinding` startup (predictable; daemon startup is once, first-call latency stays flat). Per-client stdio transport; restart on crash with capped exponential backoff (1s → 2s → 4s … cap 60s); log multiplexing into the daemon's logs with a `[client:<name>]` prefix; graceful shutdown on SIGTERM (kill children, flush).

Per-call and startup timeouts (defense against wedged children):
- `call_timeout` (default 30s, per `[mcp.clients.<name>]`): every dispatch enforces this. On timeout, the host feeds a `tool_result` with `is_error=true` back to the LLM (so the LLM can recover or fail gracefully) and does not auto-restart the child (a slow tool isn't a crashed child).
- `startup_timeout` (default 10s, per `[mcp.clients.<name>]`): handshake deadline at daemon startup. If a child fails to handshake in the window, the daemon logs it and starts *without* that child's tools. The supervisor keeps trying on the same backoff schedule. This solves the "misconfigured child pins daemon startup" failure mode.

Tool naming:
- `tool_prefix` per MCP client is mandatory. Defaults to the config section name. Avoids silent collisions when two child servers expose the same tool name (e.g. both a code-parser and a pdf-parser exposing `parse_file`). Audit / log lines like `tool call deco-assaying.parse_file failed` stay unambiguous.

Default config additions:

```toml
# Default ships with no autostart MCP children — cobalt-grinding core has no parsers.
# Once deco-assaying is installed (separate project; required by M3),
# uncomment to autostart it:
#
# [mcp.clients.deco-assaying]
# command         = "deco-assaying"
# # args          = []
# # tool_prefix   = "deco-assaying"                 # defaults to section name
# autostart       = true
# restart         = "on-failure"
# call_timeout    = 30
# startup_timeout = 10

[host]
# tools_top_k   = 10                            # default
# default_model = "claude-opus-4-7"             # falls back to [llm].model
# max_iters     = 20
```

`pyproject.toml` entry points (unchanged from M2):

```toml
# cobalt-grinding/pyproject.toml — daemon only
[project.scripts]
cobalt-grinding = "cobalt_grinding.daemon.main:run"
```

(The CLI binary `cogrind-workshop` ships from the sibling cogrind-workshop repo; M2.7 extracted it.)

New code in M2.5:

| Path | Purpose | Lines (rough) |
|---|---|---|
| `src/cobalt_grinding/host/api.py` | `run_agent(...)` public surface | ~60 |
| `src/cobalt_grinding/host/loop.py` | tool-use loop | ~150 |
| `src/cobalt_grinding/host/dispatch.py` | tool name → MCP client + call | ~80 |
| `src/cobalt_grinding/host/provider.py` | `LLMProvider` protocol + Anthropic impl | ~80 |
| `src/cobalt_grinding/host/tools_index.py` | LanceDB-backed tools index, hybrid retrieval | ~120 |
| `src/cobalt_grinding/daemon/mcp_clients.py` | `McpClientManager` (supervise / restart / timeouts) | ~250 |
| `tests/fixtures/stub_mcp_server.py` | tiny in-tree MCP server (e.g. `echo.greet`) for integration testing | ~60 |
| Existing files needing edits: `src/cobalt_grinding/app.py`, `src/cobalt_grinding/daemon/main.py`, `src/cobalt_grinding/daemon/server.py`, `src/cobalt_grinding/config.py`, `pyproject.toml` | n/a | small |

Internal dispatch shape (host-side only, NOT agent-facing):

```python
# src/cobalt_grinding/host/dispatch.py — internal only
@dataclass(frozen=True)
class DispatchResult:
    ok: bool
    content: list[dict]       # MCP tool_result content blocks (text / image / etc.)
    error_text: str | None    # human-readable, fed back to the LLM as a tool_result
    is_error: bool            # MCP flag so the LLM knows the call failed

async def dispatch(name: str, arguments: dict, *, mcp_clients, timeout: float) -> DispatchResult: ...
```

This is host machinery. Agents don't import it. The host turns `DispatchResult` into the right `tool_result` block shape and feeds it back into the LLM messages.

Tests (`tests/test_host_*.py`, `tests/test_daemon_mcp_clients.py`):

Unit (no LLM):
- `test_host_dispatch.py`: dispatch routes by tool prefix; unknown name → `DispatchResult(ok=False, is_error=True)`; timeout / child-died → retryable error.
- `test_host_loop.py`: with a fake provider scripting `tool_use → end_turn`, the loop dispatches once and returns; `tool_use → tool_use → end_turn` dispatches twice; iter cap raises clearly.
- `test_host_provider.py`: Anthropic provider serializes tools list correctly from MCP `tools/list` shapes; parses `messages.create()` response into the loop's expected structure.
- `test_host_tools_index.py`: index built at startup matches `tools/list`; child crash removes entries; child restored re-indexes; hybrid retrieval returns relevant tools (BM25 hits one, vector hits another, RRF fuses).
- `test_daemon_mcp_clients.py`: child supervision (startup_timeout, call_timeout, restart on crash, SIGTERM cleanup); uses `tests/fixtures/stub_mcp_server.py`, not a real production child.

Integration (`@integration`, opt-in by default per pyproject):
- `test_host_integration.py`: full end-to-end against the in-tree stub MCP server: `cobalt-grinding` starts with `[mcp.clients.stub]` pointed at `tests/fixtures/stub_mcp_server.py`; a test agent calls `await app.host.run_agent(system=..., messages=[{"role": "user", "content": "Greet Gary."}])`; the LLM emits `tool_use(stub.greet, ...)`; host dispatches; final assistant content reflects the stub's response. End-to-end against the real `deco-assaying` happens in M3 once that project ships.

Done when:

1. `cobalt-grinding` starts with the configured stub MCP child autostarted; `tools_index` includes the stub's tools (prefixed `stub.*`); `cobalt-grinding` started with no `[mcp.clients.*]` sections also starts cleanly with an empty `tools_index`.
2. `app.host.run_agent(...)` runs an end-to-end LLM tool-use loop using the stub child; the host picks tools via hybrid retrieval; final assistant message reflects the stub's response.
3. A wedged child at startup doesn't pin daemon startup; `startup_timeout` triggers; supervisor keeps retrying.
4. A wedged tool call returns a structured `tool_result(is_error=true)` to the LLM; supervisor does not restart the child.
5. Killing a child mid-loop triggers restart (capped exponential backoff); the next call succeeds; `tools_index` re-populates without restarting `cobalt-grinding`.
6. SIGTERM cleanly shuts every child; no orphan processes.
7. M3-style agents (`src/cobalt_grinding/ingest/handlers/code.py`) are *implementable* against `app.host.run_agent(system, messages)` with no MCP plumbing leakage and no tool-selection logic in agent code.

#### M3: Ingest (first impl)

Deliberately narrow scope: just enough to feed Retrieve and Converse so the end-to-end system can be tested.

Preconditions:
- [`smalt-mcp`](https://github.com/ParkviewLab/smalt-mcp) v0.5.0+ shipped (workstream-A cleave; storage-substrate-only surface). Ingest writes pages via `smalt.write_page` / `smalt.write_pages` / `smalt.add_link` / `smalt.add_claim`; no proposal tools on smalt-mcp.
- [`deco-assaying`](https://github.com/ParkviewLab/deco-assaying) installable. CoGrind core ships no parsers; M3's code handler reaches `deco-assaying.parse_file` through the host like any other capability.
- The default config's `[mcp.clients.smalt-mcp]` and `[mcp.clients.deco-assaying]` blocks are uncommented (or added by the user) once both are on `$PATH`.
- Ebony-enriching is not required for M3: Ingest writes pages, it doesn't propose; no proposal-substrate dependency.

Input scope: `cogrind-workshop --ingest <path>` auto-detects file vs. directory:

- Single local file → source-id `file:<file pathname>`. The file *is* the source (no section structure). Hard error if the file's type isn't in the supported list.
- Local directory → source-id depends on what the directory is:
  - `.git/` present → `git:<remote-url>` (uses `origin` remote URL; falls back to `dir:<pathname>` if no remote is configured)
  - `.obsidian/` present → `obsidian:<dir pathname>`
  - Both present → both flags set; both sets of metadata captured
  - Neither → `dir:<dir pathname>`
  
  Granularity: directory = one source, files within it = sections.

- No URLs in M3. No standalone-file ingestion via URL. Both deferred to a later milestone.

Supported file types in M3 (everything else is silently ignored in a directory and listed under `ignored:` in the source page's frontmatter; hard error if passed explicitly as a single file):

- Documents: `.md`, `.rtf`, `.txt`, `.pdf`
- Configs: `.json`, `.json5`, `.toml`
- Source code: `.py`, `.c`, `.cpp`, `.h`

Git metadata captured (best-effort; missing tools are silently skipped):

- `git remote -v` → parsed into `{name: url}` map
- `git rev-parse HEAD` → current SHA
- `git branch --show-current` → branch
- `git log -1 --format=%H%n%an%n%ae%n%aI%n%s` → HEAD commit details (sha, author, email, ISO date, subject)
- `git status --porcelain` → clean / dirty working-tree flag
- For GitHub remotes only (and only if `gh` is available): `gh repo view --json description -q .description` → repo description

Obsidian vault metadata captured:

- `.obsidian/` config: vault name, plugin list, settings (relevant subset)
- Existing `[[wikilinks]]` are parsed from the vault's markdown and preserved as edges in the Smalt, alongside CoGrind's own discovered links

Re-ingestion behavior in M3:

- When a source is re-ingested, CoGrind detects files that are *new* since the last ingestion and processes them. Existing file pages are *not* updated even if the underlying file's content has changed. SHA-based change detection on already-ingested files is post-Phase 1.

Sub-agents implemented in M3 (pipelined; see *Ingest subsystem* above for the full diagram):

`format_classifier`, `source_fetcher` (file/dir resolution + git/vault detection + metadata capture), `structure_extractor`, `chunker`, `summarizer`, `entity_extractor`, `glossary_extractor`, `link_resolver`, `frontmatter_writer`, `page_writer`, `indexer_caller`. Sub-agents that need an LLM call `await app.host.run_agent(system=..., messages=[...])`; the host retrieves relevant MCP tools from `tools_index` (BM25 + vector hybrid; top-K to the LLM). The code handler's prompt asks the LLM to use `parse_file` when it needs symbols; the host's retrieval surfaces the autostarted `deco-assaying.parse_file` automatically. Other handlers will pick up `pdf.extract_text`, `docx.extract_text`, etc. as those MCP children get added; no agent code change required.

Page output shape per source. One ingest of `tests/fixtures/sample_dir/` containing four files (one unsupported) produces:

- Source page: hybrid layout. Multi-file source: `pages/sources/<source-id>/index.md`. Single-file source: `pages/sources/<source-id>.md` (no directory). Body: LLM-written 2-3 paragraph "what this source is" synthesized from the section summaries, plus an auto TOC of section pages.
- Section pages: one per supported file, at `pages/sources/<source-id>/<file>.md`. `parent_source` frontmatter links back to the source page. Body: per-file LLM summary; for code files, also a deterministic symbol outline produced by `deco-assaying`:

  ```markdown
  ## Summary
  This module exposes …

  ## Symbols
  - `class Foo` — line 12 — "manages …"
  - `def bar(x)` — line 45

  ## Imports
  - `os`
  - `pathlib.Path`
  ```

- Entity pages at `pages/entities/<slug>.md`, minimal in M3: name, aliases, `entity_kind`, `domains: list[ConceptPageId]`, mentioned-in back-links list. Created if new; updated additively if existing (no deletion). `domains:` used for disambiguation when same-name entities differ across domains.
- Glossary concept pages at `pages/concepts/<slug>.md` with `glossary: true` and `domains: list[ConceptPageId]` (multi-domain by default). Body is the short definition (1-3 sentences) plus per-source evidence snippets.
- Domain concept pages at `pages/concepts/<slug>.md` with `is_domain: true`. Created on first reference (Ingest agent proposes a new domain when a source/term clearly belongs to one not yet in the Smalt; same propose-don't-act discipline). Domain hierarchy (CS is a sub-domain of computing) lives as `subdomain_of` labeled links, *not* in the `domains:` field.
- Auto-generated `pages/glossary.md`: an `IndexPage` over every `glossary: true` concept page. Same term across multiple meanings shows as multiple entries (one per ConceptPage), each tagged with its domains inline. Multi-domain entries (one meaning, several domains) show with all domains tagged on the single entry. Format example:
  ```
  - **Bayesian inference** (stats, ml, phil_sci) — A method for updating beliefs in light of evidence…
  - **Cell** (biology) — The smallest structural and functional unit of life.
  - **Cell** (spreadsheet) — A single addressable rectangle in a spreadsheet grid.
  - **Tree** (cs) — A recursive node-based data structure…
  - **Tree** (botany) — A perennial woody plant with a single main stem…
  ```
- Auto-generated `pages/domains.md`: an `IndexPage` over every `is_domain: true` concept page. Lists each domain with a one-line description and a count of glossary entries / source pages tagged in that domain. Domain hierarchy (parents/children via `subdomain_of` links) renders as nested tree.

Naming conventions:
- Source ID: `<kind>-<basename>-<8char-hash>` (e.g. `dir-sample_dir-a3f1`, `git-cobalt-grinding-9b2c`, `obsidian-myvault-7d12`). Hash over `location_uri` disambiguates same-named sources.
- Section ID: `<source-id>::<file-relative-path>` (e.g. `dir-sample_dir-a3f1::src/utils.py`).
- Entity / concept ID: `<prefix>-<slug>` (`ent-some-org`, `con-embedding`).
- Slug disambiguation rule (Wikipedia-style, agent-chosen). When a new ConceptPage's natural slug collides with an existing one of *different meaning* (e.g., `tree` for the CS data structure vs. `tree` for the botanical plant), the agent appends a free-form, human-readable meaning hint to the new page's slug: `tree-data-structure.md` and `tree-plant.md`, or whichever pairing the agent judges most readable. The `domains:` field carries domain info; the slug doesn't encode domain. Same rule applies to entity-name collisions and any other slug collision. Same-meaning-multi-domain doesn't trigger this: that's one ConceptPage with multiple domains in `domains:`.

SME ingest agent's domain-assignment job. When extracting a glossary term, the agent picks `domains:` based on three signals, in roughly this order of strength:
1. The source's own `domains:` (if the SourcePage is tagged `[cs]`, the default for terms extracted from it is `[cs]`).
2. Surrounding context in the source (a term mentioned in a CS-flavored paragraph).
3. The term itself (some terms are domain-specific, such as "monad", "homotopy", "chiral", and their names alone are strong signals).

Multi-tag liberally when context is genuinely mixed. Mark `domain_confidence: low` on the concept page when the assignment is uncertain; Curate periodically reviews low-confidence domain assignments as a drift signal.

`format_classifier` dispatch:
- Input: a `Path`. Single-file ingest: extension lookup → handler. Hard error if extension not in supported list.
- Directory ingest: walk, group by extension, dispatch each file to its handler. Unsupported extensions → recorded in `source.ignored` (no error).
- `.h` disambiguation runs once per directory ingest: scan source root for sibling `.cpp/.cc/.cxx/.hpp` (heuristic step 1) → content-sniff first few `.h` files for `namespace`/`class`/`template`/`extern "C"` (step 2) → otherwise C (step 3). Sets a per-ingest `h_lang ∈ {"c","cpp"}` used for all `.h` dispatches in that ingest. Overridable via `--lang-h=c|cpp`.
- Handler signature: `handle(file: Path, source_ctx: SourceContext, app: App) -> SectionResult`. Handlers that need an LLM call `await app.host.run_agent(...)`; the host runs the tool-use loop and picks tools via `tools_index` retrieval.

Surfaces:

- CLI: `cogrind-workshop --ingest <path>` (auto-detects file vs. directory). MCP-only via the daemon: no in-process fallback. Optional `--lang-h=c|cpp`.
- MCP tools registered: `wiki.ingest`, `wiki.list_sources`, `wiki.source_status`. `wiki.ingest` enqueues an ingestion task on the daemon's worker pool (set up in M2) and returns a `task_id`; clients poll via `wiki.task_status` for progress.

Concurrency:

- Multiple `wiki.ingest` calls run in parallel on the daemon's thread pool (configurable size, default `min(8, os.cpu_count())`).
- The expensive work (file I/O, LLM calls, fastembed inference, tree-sitter / pdf parsing) parallelizes: the GIL is released by the libraries doing the heavy lifting.
- The corpus-write step (page write + index update) goes through the single-writer mutex set up in M1.

Re-ingestion semantics (Phase 1):

- New files only. Existing section pages are *not* rewritten even if their underlying file's content has changed. SHA-based file-content change detection is post-Phase-1.
- The source page IS regenerated each run: refreshed `structure_inline`, refreshed `sections` list, refreshed `ignored` list (a previously-ignored `.docx` stays in `ignored:` until support arrives), refreshed `fetched_at`.
- Existing entity / concept page updates are additive (new section IDs appended to back-link / evidence lists). No deletion.
- Glossary `IndexPage` is regenerated by the indexer.
- Ingest is idempotent at the page-write layer: re-running with no file changes is a no-op (caught by the indexer's content-hash check).

Done when:

1. `cogrind-workshop --ingest tests/fixtures/sample_dir/` (a generic doc directory containing supported and unsupported file types) produces one source page at `pages/sources/<id>/index.md` with sections for the supported files and an `ignored:` list for the rest.
2. `cogrind-workshop --ingest tests/fixtures/sample_repo/` (a small git repo) produces a source page with the captured git metadata in frontmatter and section pages for its supported files. Code section pages include a deterministic symbol outline (from `deco-assaying`) in addition to the LLM summary.
3. `cogrind-workshop --ingest tests/fixtures/sample_vault/` (a small Obsidian vault, optionally also a git repo) produces a source page with vault config + git metadata, section pages, and the existing `[[wikilinks]]` preserved as edges.
4. `cogrind-workshop --ingest tests/fixtures/sample.md` (a single supported file) produces a single source page at `pages/sources/<id>.md` (flat, no directory).
5. `cogrind-workshop --ingest tests/fixtures/sample.docx` (a single unsupported file) errors with `type not supported`.
6. The ingest produces at least one entity page (`pages/entities/<slug>.md`) and at least one glossary `ConceptPage` (`pages/concepts/<slug>.md` with `glossary: true`); `pages/glossary.md` exists as an `IndexPage` with `auto_generated: true` and is regenerated on subsequent ingests.
7. Re-ingesting any of the above adds only new files; existing section pages are unchanged; the source page's `sections:` list grows; entity / concept pages have new sources appended.
8. A `.h` file in a directory with sibling `.cpp` parses as C++ (verified by the symbol kinds in its section page); the same file in a C-only fixture parses as C; `--lang-h=c` overrides the heuristic.
9. All of the above work identically when invoked via the `wiki.ingest` MCP tool from a connected client.

#### M4: Retrieve
- Retrieve pipeline with metadata filter + hybrid BM25/vector + RRF
- Graph expansion (1-hop)
- Gap detection
- Result caching
- `cogrind-workshop --query "<question>"` returns ranked pages with snippets
- MCP tools registered: `wiki.search`, `wiki.get_page`, `wiki.traverse`, `wiki.find_gaps`

Done when: queries against the seed corpus return relevant pages with reasonable ranking; gaps are detected when nothing matches; same retrieval available via MCP.

#### M5: Converse
- Converse orchestrator using Retrieve as a tool
- Citation checker
- `cogrind-workshop --ask "<question>"` returns a Converse answer with citations
- Conversational mode: `cogrind-workshop --chat` (interactive REPL evolves later from this same flag-driven shape)
- MCP tool registered: `wiki.ask`

Done when: end-to-end demo (ingest a doc, ask a question about it, get a cited answer); the same conversation works from Claude Desktop / Claude Code via the MCP server.

Phase 1 ends here. End-to-end working CoGrind: ingest → retrieve → converse, with MCP exposure.

---

#### M6: Research (first impl), Phase 2

*Scope to be discussed and locked in before this milestone starts (same way M3 was scoped).*

Preconditions: [`smalt-mcp`](https://github.com/ParkviewLab/smalt-mcp) v0.5.0+ (Ingest writes source pages on accept) AND [`ebony-enriching`](https://github.com/ParkviewLab/ebony-enriching) v0.1.0+ (proposals + gaps). Both substrates required.

Why M6 matters: it turns corpus growth into a flywheel (see *Cross-system flows* above for the full picture). Pre-M6, every source is hand-pointed by the user. Post-M6, two new modes become available: *reactive* (gap → proposal → accept → ingest) and *proactive* (`cogrind-workshop --research "<topic>"` seeds proposals). In both, Research *proposes only*; the user stays in the approval loop until trust is earned.

The intent of M6's first impl: stand up Research narrowly enough that a gap signal from any of the five emitters (Retrieve, Converse, Ingest, Curate, Cogitate) can produce a *proposal* for what to ingest next. Proposes only; does not auto-ingest.

Likely first-impl shape (placeholder, to be refined):
- Read gap signals from ebony-enriching (`ebony.list_gaps()`).
- Take an explicit research request via CLI / MCP (`cogrind-workshop --research "<topic>"` / `wiki.research`); the request enters the queue via `ebony.add_gap(query=...)` first, then Research processes it.
- Search a small set of source backends (web search via `WebSearch` tool, GitHub repo search via `gh`, possibly ArXiv); bounded budget per request.
- Evaluate candidates for relevance, authority, recency, accessibility.
- Write `ProposalPage`s of `proposal_kind: source_adoption` via `ebony.write_proposal(...)`, landing in ebony-enriching's `proposals/research/` (see *Proposal document shape and lifecycle*).
- *No auto-ingestion.* User accepts → cobalt-grinding orchestrates the cross-substrate publish: `smalt.write_page` to add the source page, `ebony.update_proposal_status(applied)`, `ebony.remove_gap` to clear the queue entry.

Proposal-as-hypothesis discipline. Each Research proposal frames an `Observation` (the gap signal it's responding to), a `Hypothesis` ("ingest this candidate source X"), a `Prediction` ("queries that hit gap Y will return non-empty results after this source is in the corpus"), and a `Test`: for cheap-tier candidates (the source has an accessible summary or abstract), Research runs a dry-retrieval against the would-be summary and reports whether the gap-signal query would now match. Expensive-tier candidates (full ingest required to know) are marked `test_status: untestable` with the user as the test. Status flows through the lifecycle (proposed → validated → applied; or proposed → rejected); test artifacts go to `ebony.write_experiment(...)`.

Done when: a gap signal from `ebony.list_gaps` produces a `ProposalPage` in ebony-enriching's `proposals/research/` (via `ebony.write_proposal`) with a ranked candidate list, reasoning, and (where cheap) a test result captured via `ebony.write_experiment`; user can accept a proposal and cobalt-grinding's orchestration flows the source through M3 ingestion (`smalt.write_page`) + transitions the proposal to `applied` (`ebony.update_proposal_status`) + removes the gap entry (`ebony.remove_gap`) + runs the apply-time post-mortem (see *Apply-time post-mortem: closing the learning loop*); the post-mortem itself lands as `ebony.write_experiment(input={kind:post_mortem}, ...)`, and where the lessons generalize, the agent extends `pages/concepts/research-methodology.md` via `smalt.add_claim`.

#### M7: Cogitate (first impl), Phase 2

*Scope to be discussed and locked in before this milestone starts.*

Preconditions: [`smalt-mcp`](https://github.com/ParkviewLab/smalt-mcp) v0.5.0+ (reads pages via `smalt.list_pages` / `smalt.read_page` / `smalt.traverse`) AND [`ebony-enriching`](https://github.com/ParkviewLab/ebony-enriching) v0.1.0+ (writes proposals + experiment records).

The intent: stand up Cogitate's narrow first impl so the Smalt starts producing emergent connections from what's already there. Proposes only; does not modify pages. Cogitate is the constructive counterpart to Curate (which is critical): Cogitate generates new structure, Curate flags problems with existing structure.

Cogitate's SME sub-agents apply the scientific method explicitly. The sub-agent breakdown is shaped around Observe → Hypothesize → Predict → Test → Validate:

- `observer`: walks the link graph + entity/concept pages in LanceDB; surfaces patterns and anomalies (densely-connected entity clusters lacking a parent concept; near-duplicate names; claim disagreements; recurring frontmatter keys not in SCHEMA.md).
- `hypothesis_generator`: proposes a structural change explaining the observation (a new edge with label X; a new concept named Y; a new schema field Z).
- `predictor`: pre-registers a measurable prediction the change should make true (e.g., "after adding edge X, query Q's recall rises from R1 to R2"; "after adding schema field Z, the N drift-flagged pages become conformant").
- `experimenter`: runs a cheap test where the cost tier permits (schema dry-run; query benchmark before/after on cached corpus; corpus re-link with the proposed edge). Marks `test_status: passed | failed`.
- `validator`: checks whether the prediction held; transitions the proposal to `validated` or `rejected`.

The sub-agents are SME agents in the M2.5-future-seam sense (defined as markdown documents with declared toolkits), once that machinery lands. Until then, Cogitate's M7 first impl runs them as in-process Python functions calling `host.run_agent(...)`; the structure is the same, the substrate evolves.

Likely first-impl scope (placeholder, to be refined):
- Walk the Smalt's link graph + entity/concept pages via `smalt.list_pages` + `smalt.traverse` + `smalt.read_page`.
- Detect a small set of patterns: clusters of densely-connected entities that lack a parent concept page; entity pages that share many incoming links with unrelated entity pages (suggesting a missing concept); claims about the same entity that disagree; recurring undeclared frontmatter keys (a `schema_addition` candidate).
- Write `ProposalPage`s with `proposal_kind ∈ {wiki_edge, concept_merge, novel_concept, schema_addition, contradiction}` via `ebony.write_proposal(...)`; ebony-enriching routes by `proposal_kind` / `proposed_by`:
  - schema-kinds (`schema_addition`, `schema_drift`, `schema_removal`) → ebony's `proposals/schema/`
  - everything else with `proposed_by: cogitate` → ebony's `proposals/cogitate/`
- Each proposal carries the full Observation / Hypothesis / Prediction / Test / Reasoning shape.
- Cheap-tier proposals (schema dry-run, query benchmark) are tested automatically; results captured via `ebony.write_experiment(...)`; cogitate transitions the proposal via `ebony.update_proposal_status(validated, test_status=passed)`.
- Run on demand via CLI / MCP (`cogrind-workshop --cogitate` / `wiki.cogitate`); daemon scheduled mode is later.
- More sophisticated synthesis (taxonomy building, multi-hop pattern detection, cross-source narrative reconciliation) is deferred to later Cogitate milestones.
- *Tension worth being aware of:* Cogitate benefits from a fuller corpus. Its first impl runs against whatever corpus exists at the time, which may be thin. The first-impl bar is "it works and produces sane, well-tested proposals on a small corpus"; sophistication grows as the corpus does.

Done when: running cogitate against a Smalt produces categorized `ProposalPage`s in the appropriate `EBONY_ENRICHING_DIR/proposals/*` directories (verifiable via `ebony.list_proposals(system=cogitate, ...)`), with cheap-tier proposals tested automatically (results recorded via `ebony.write_experiment` under `EBONY_ENRICHING_DIR/experiments/<proposal-id>/`) and shown to the user pre-validated; the user can review hypothesis + evidence together and accept or reject individually; lifecycle status updates flow through `ebony.update_proposal_status`. On apply (for schema/edge/concept proposals), cobalt-grinding orchestrates the cross-substrate publish (`smalt.write_page` to add the new page or update an existing one + `ebony.update_proposal_status(applied)`) and runs the apply-time post-mortem: for cheap-tier proposals, a Haiku-only summary lands as `ebony.write_experiment(input={kind:post_mortem}, ...)`; for medium/expensive-tier, the full pipeline runs, and where the lessons generalize, the agent extends `pages/concepts/cogitate-methodology.md` (or a kind-specific pattern page) via `smalt.add_claim`. See *Apply-time post-mortem: closing the learning loop*.

#### M8: Curate (first impl), Phase 2

*Scope to be discussed and locked in before this milestone starts.*

Preconditions: [`smalt-mcp`](https://github.com/ParkviewLab/smalt-mcp) v0.5.0+ (audits via `smalt.list_pages` / `smalt.read_page` / `smalt.incoming_links` / `smalt.traverse`) AND [`ebony-enriching`](https://github.com/ParkviewLab/ebony-enriching) v0.1.0+ (writes audit findings as proposals).

The intent: stand up Curate's narrow first impl so the Smalt starts auditing itself. Flags only; does not delete or modify pages. Curate is the critical counterpart to Cogitate (which is constructive): Cogitate proposes additions, Curate flags problems with what's already there.

Curate's findings are also `ProposalPage`s: same scientific-method discipline applied to *removal/correction* hypotheses rather than additive ones. Each finding frames the observed drift as a falsifiable claim ("these N pages use a `tags:` key SCHEMA.md doesn't mention" → testable by counting pages and checking SCHEMA.md). Predictions are usually about the corpus's current state; tests are usually corpus-walking queries. Most Curate proposals are cheap-tier and arrive at the user pre-validated.

Likely first-impl scope (placeholder, to be refined):
- Audit the Smalt via `smalt.list_pages` + `smalt.read_page` + `smalt.incoming_links` (the latter is the canonical "what links to this page" view added at smalt-mcp v0.3.0).
- Detect and flag a small set of issues, each as a `ProposalPage` of the appropriate kind:
  - `proposal_kind: orphan`: pages with no inbound links (via `smalt.incoming_links` returning empty) and no recent access
  - `proposal_kind: duplicate`: entity pages with very similar names/aliases (excluding multi-domain disambiguation cases)
  - `proposal_kind: broken_link`: internal links to nonexistent pages (via traverse + read_page round-trips)
  - `proposal_kind: staleness`: source pages with `fetched_at` older than a configurable threshold
  - `proposal_kind: schema_drift`: pages using a frontmatter key SCHEMA.md doesn't mention; required fields missing on existing pages
- Write findings via `ebony.write_proposal(..., proposed_by: curate)`: lands in ebony-enriching's `proposals/curate/`. Schema-drift findings stay there too (Curate flags drift; Cogitate is the system that proposes schema additions; keeps the constructive/critical split clean).
- Run on demand via CLI / MCP (`cogrind-workshop --curate` / `wiki.curate`); daemon scheduled mode is later.
- More sophisticated audits (extraordinary-claim flagging, dust-detection, low-confidence-domain audits, schema-removal candidates, fields whose tests fail under accumulated evidence, i.e., the `applied → re-proposed` lifecycle edge) are deferred to later Curate milestones.
- A separate audit detects an excessive rate of `untestable` proposals across all systems' queues (via `ebony.list_proposals(test_status="untestable")` cross-system); flags it as a discipline-slipping signal.

Done when: running curate against a Smalt corpus produces categorized `ProposalPage`s in ebony-enriching's `proposals/curate/` (verifiable via `ebony.list_proposals(system=curate)`) with concrete flagged pages, observations, and (where applicable) cheap-tier test results captured via `ebony.write_experiment`; user can review and accept/reject each finding individually; lifecycle status updates flow through `ebony.update_proposal_status`. On apply (e.g., a duplicate-merge accepted), cobalt-grinding orchestrates the cross-substrate publish (`smalt.remove_page` for the merge-into target + `smalt.write_page`/`smalt.update_claim` to consolidate + `ebony.update_proposal_status(applied)`) and runs the apply-time post-mortem (see *Apply-time post-mortem: closing the learning loop*): for medium/expensive-tier corrections, lessons go to `pages/concepts/curate-methodology.md` via `smalt.add_claim`; recurring drift patterns may warrant a new ConceptPage proposed via `ebony.write_proposal(..., proposal_kind: novel_synthesis)`.

#### M9+: beyond Phase 2's first impls

Phase 3 is the self-evolution phase: the systems CoGrind needs once it's mature enough to grow / judge / improve itself. To be milestoned when we get there:

- Toolsmith, the 7th agentic system. Closes the self-evolution loop on CoGrind's own capability surface. Reads tool-gap entries from ebony-enriching (`ebony.list_gaps`, fed by all six other systems via `ebony.add_gap`; see *Cross-system flows*), searches existing MCP servers (registries, GitHub, npm, PyPI), evaluates fit / maturity / license, and writes proposals via `ebony.write_proposal(..., proposed_by: "toolsmith")`, landing in `EBONY_ENRICHING_DIR/proposals/toolsmith/`, of two kinds: *adopt* (use this existing server) or *specify* (no fitting server exists; here's a requirements doc for one to be built: the deco-assaying pattern, codified). Same proposal-only discipline as Research; same search/evaluate engine, applied to capabilities instead of knowledge. Composes pieces of Research (search + evaluate), Curate (audit agent rosters' toolkits for unused tools), and Cogitate (propose toolkit additions for tools observed-but-not-declared). Reasonable timing: after Phase 2; needs Research / Curate / Cogitate as building blocks, and needs enough agents running for tool-usage signal to be real.
  - Pre-Toolsmith expectation: until Toolsmith exists, *we* (humans + Claude) play its role: when we hit a capability gap, we either find an existing MCP server or spawn a new project (deco-assaying was the first such, and is the proof point that this pattern works). Phase 1 / Phase 2 systems are built this way; Phase 3 is when the system takes over the curation of its own toolset.
- A source veracity / quality system (working name TBD; candidates: Vet, Appraise, Weigh). Eventually we'll want a dedicated agentic system that judges and rates sources for veracity and quality: authority of author / publisher, recency, evidence strength, peer review status, citation density, prior reliability of the source domain. It writes per-source quality + veracity scores into source-page frontmatter. Used by: Research (prefer high-quality candidates), Cogitate (weigh conflicting claims by source quality when surfacing contradictions), Curate (flag pages whose claims rest on low-quality sources). Reasonable timing: after enough corpus exists for "prior reliability of a domain" to be meaningful, probably alongside or after deeper Curate. (Together with Toolsmith, this is part of the Phase 3 self-evolution arc: Toolsmith judges *capabilities*, Vet judges *knowledge*.)
  - Schema implication for earlier milestones: the `Source` frontmatter model defined in M0 should reserve fields for `quality_score`, `veracity_score`, `evaluated_at`, and `evaluation_notes` (default `null` / `unrated`), so this future system can populate them without a schema migration.
- DuckDB analytical layer over the same Lance files (read-only).
- Image / multimodal ingestion (whiteboard, infographic, chart).
- *CoGrind-aware* Claude Code skill / plugin (distinct from the MCP server, which is in Phase 1).
- URL ingestion.
- Broader file-type support.
- Full re-ingestion change detection (SHA-based, beyond the new-files-only rule of M3).
- Deeper impls of Research, Cogitate, and Curate.

### Repo / file layout

```
Cobalt-Grinding/
  src/cobalt_grinding/                       # source tree shipping two binaries; no business logic
                                  # is shared between them — the CLI talks to the daemon
                                  # over MCP for everything substantive (only the config
                                  # loader and a few tiny types are common code)

    # ---- daemon-side core (used by the daemon's tool handlers; not imported by the CLI) ----
    # NOTE: cobalt-grinding ships ZERO storage layer. The Smalt schema +
    # indexer + LanceDB tables live inside smalt-mcp (separate repo); the
    # sciencing/lab-notebook schema lives inside ebony-enriching (separate
    # repo). cobalt-grinding's only storage-related code is the MCP-host machinery
    # that talks to those children.
    __init__.py
    config.py                   # layered TOML loader (incl. [mcp.clients.*] for substrate children)
    app.py                      # App: shared resources (LLM client, MCP-host) — M2
    host/                       # M2.5 — agent runtime + MCP-host client side
      api.py                    # public: await app.host.run_agent(system, messages, ...)
      loop.py                   # the LLM tool-use loop (LLM → tool_use → dispatch → tool_result → ...)
      dispatch.py               # internal: name → MCP client → result; never escapes the host
      provider.py               # LLMProvider protocol + Anthropic impl
      tools_index.py            # LanceDB-backed registry; hybrid BM25 + vector retrieval over tool descriptions
    # NOTE: code parsing lives in a separate project — deco-assaying
    # (https://github.com/ParkviewLab/deco-assaying). CoGrind core
    # ships zero parsers; deco-assaying is configured as an MCP child via
    # [mcp.clients.deco-assaying] and autostarted by cobalt-grinding once installed.
    ingest/                     # the Ingest subsystem (M3+)
      orchestrator.py
      format_classifier.py
      source_fetcher.py
      structure_extractor.py
      chunker.py
      handlers/
        text.py / markdown.py / config_files.py / html.py / pdf.py / code.py / github_repo.py
      agents/
        summarizer.py / entity_extractor.py / glossary_extractor.py / link_resolver.py / ...
    retrieve/                   # the Retrieve subsystem (M4)
      pipeline.py / query_classifier.py / hybrid.py / graph_expander.py / gap_detector.py / cache.py
    converse/                   # the Converse subsystem (M5)
      orchestrator.py / intent_classifier.py / answerer.py / citation_checker.py / novelty_detector.py
    research/                   # the Research subsystem (M6)
      orchestrator.py
    cogitate/                   # the Cogitate subsystem (M7)
      orchestrator.py
    curate/                     # the Curate subsystem (M8)
      orchestrator.py
    common/
      llm.py                    # Anthropic SDK wrappers
      prompts/                  # prompt templates per sub-agent

    # ---- daemon: cobalt-grinding binary ----
    daemon/                     # M2; M2.5 adds mcp_clients.py
      __init__.py
      main.py                   # entry point: cobalt-grinding
      bootstrap.py              # auto-init Smalt dir on first run
      server.py                 # MCP server side (HTTP + stdio transports)
      tools.py                  # wiki.* tool handlers — call into the shared core
      mcp_clients.py            # M2.5 — child MCP server supervisor (spawn/restart/shutdown)
      scheduler.py              # asyncio loop + thread-pool executor
      tasks.py                  # Task model, status tracking
      mutex.py                  # corpus-write single-writer mutex

    # ---- CLI: extracted to the cogrind-workshop sibling repo at M2.7 ----
    # (previously lived here as cogrind/cli/; see ParkviewLab/cogrind-workshop)

  tests/
    fixtures/
      seed_smalt/               # tiny pre-built Smalt for tests
      sample_sources/           # one file per format
      stub_mcp_server.py        # M2.5: in-tree stub MCP server used by host integration tests
    test_config.py
    test_schema.py
    test_markdown.py
    test_indexer.py             # tests the Indexer class directly with FakeEmbedder
    test_indexer_integration.py # @integration: real fastembed end-to-end
    test_daemon_*.py            # M2: bootstrap, scheduler, mutex, tool handlers
    # (CLI tests live in the cogrind-workshop sibling repo post-M2.7)
    test_host_*.py              # M2.5: dispatch, loop, provider, tools_index hybrid retrieval
    test_host_integration.py    # M2.5: @integration — end-to-end run_agent against in-tree stub MCP server
    test_daemon_mcp_clients.py  # M2.5: child supervision, restart, SIGTERM, timeouts
    test_ingest_text.py         # M3+
    test_retrieve.py            # M4+
    test_converse.py            # M5+
    test_research.py            # M6+
    test_cogitate.py            # M7+
    test_curate.py              # M8+
  pyproject.toml                # [project.scripts] declares cobalt-grinding (daemon only)
  README.md
  CLAUDE.md                     # how Claude Code should help develop this
```

```
~/Documents/Smalt/             # default SMALT_DIR (configurable via SMALT_DIR env or per-Smalt config). NOTE: no `raw/` — sources are not copied. Owned by smalt-mcp.
  pages/
    entities/
    concepts/                   # ConceptPages; glossary entries are concepts with `glossary: true`
    sources/                    # hybrid layout: single-file → <source-id>.md; multi-file → <source-id>/index.md + <source-id>/<file>.md
    syntheses/                  # cross-source pages (Phase 2 — written by Cogitate proposals after approval)
    glossary.md                 # auto-generated IndexPage — sorted TOC over every glossary=true concept
  structures/                   # sidecar JSON for source structures too large to inline (e.g., big repo dir trees)
    <source-id>.json
  schema/
    SCHEMA.md                   # canonical-knowledge schema (page types, frontmatter, link vocabulary)
    POLICY.md                   # how agents produce/modify Smalt pages
  index/
    lance/                      # pages, embeddings, links, claims, sources tables
  tasks/                        # reserved for future Smalt-internal task state (proposals / experiments / gaps
                                # live in ebony-enriching, NOT here — moved out at smalt-mcp v0.5.0)
```

```
~/Documents/EbonyEnriching/    # default EBONY_ENRICHING_DIR (configurable). Owned by ebony-enriching MCP server.
  proposals/                    # all systems' ProposalPages (see Proposal document shape and lifecycle)
    schema/                     # schema-addition / schema-drift / schema-removal proposals (Cogitate, Curate)
    cogitate/                   # other Cogitate proposals (edges, concepts, contradictions, novel-synthesis)
    curate/                     # Curate findings (drift, orphans, duplicates, broken-links, staleness)
    research/                   # Research source-adoption proposals (M6)
    toolsmith/                  # Phase 3: tool-adoption / tool-specification / toolkit-addition / -removal proposals
    converse/                   # Converse novelty-detector proposals (novel-synthesis candidates)
  experiments/                  # the lab notebook's experimental record
    <proposal-id>/
      <run-timestamp>.md        # what was tested, input, result; links back to triggering proposal
  gaps.md                       # unified knowledge-gap + tool-gap queue (Retrieve, Converse, Ingest, Curate,
                                # Cogitate, Toolsmith all fan in via ebony.add_gap)
  schema/
    SCHEMA.md                   # ProposalPage shape, lifecycle states, ExperimentRecord, GapEntry shapes
    POLICY.md                   # falsifiability + cost-tier discipline (the proposal-as-hypothesis rules)
  config.toml                   # per-EbonyEnriching config (optional)
```

### Verification

Each milestone has an end-to-end smoke test runnable from the CLI (and, where applicable, from an MCP client):

| Milestone | Verification |
|---|---|
| M0 | `cobalt-grinding` starts with all three MCP children (`smalt-mcp`, `ebony-enriching`, `deco-assaying`) autostarted; `smalt.bootstrap()` materializes the canonical SMALT_DIR layout (pages/, schema/SCHEMA.md, schema/POLICY.md, index/lance/ tables); `ebony.bootstrap()` materializes the canonical EBONY_ENRICHING_DIR layout (proposals/{schema,cogitate,curate,research,toolsmith,converse}/, experiments/, gaps.md, schema/SCHEMA.md+POLICY.md); `cogrind-workshop --status` reports both substrates exist; the daemon binary itself accepts an MCP client connection (no `wiki.*` tools yet). |
| M1 | Hand-write 3 pages, run `cogrind-workshop --index`, confirm LanceDB tables populated with rows, embeddings, FTS index, HNSW index; modify one page, re-index, confirm only the changed page reprocesses. CLI runs synchronously (no daemon yet). |
| M2 | `cobalt-grinding` starts cleanly, auto-initializes an empty Smalt dir (replacing the old `cogrind init`). `cogrind-workshop --status` from a separate shell connects via HTTP MCP and reports daemon state. `cogrind-workshop --index` runs M1's indexer as a daemon task; second invocation visibly faster (warm embedder + DB). `wiki.task_status` reports live progress; `wiki.task_cancel` interrupts cleanly. `cogrind-workshop --status` with no daemon running fails with a clear error pointing at `cobalt-grinding`. Multiple concurrent MCP requests interleave (long-running task + several quick status calls). Single-writer mutex serializes two concurrent corpus-write attempts. Claude Desktop can connect and call `wiki.status` / `wiki.index`. |
| M2.5 | `cobalt-grinding` autostarts the in-tree stub MCP child (`tests/fixtures/stub_mcp_server.py`); `tools_index` is populated at startup with the stub's tools (BM25 + embedding indexes built). `await app.host.run_agent(system="...", messages=[{"role":"user","content":"Greet Gary."}])` runs an end-to-end LLM tool-use loop using the stub child; the host picks tools via hybrid retrieval; the LLM emits `tool_use(stub.greet, ...)`; final assistant content reflects the stub's response. A fixture child whose handshake exceeds `startup_timeout` doesn't pin daemon startup; the supervisor keeps retrying it. Exceeding `call_timeout` mid-call returns a structured `tool_result(is_error=true)` to the LLM and does not restart the child. Killing the stub child mid-loop triggers a clean restart (capped exponential backoff); `tools_index` re-populates without restarting `cobalt-grinding`; the next call succeeds. SIGTERM to `cobalt-grinding` shuts every child cleanly: no orphan processes. End-to-end against the real `deco-assaying` is exercised in M3 once that project ships. |
| M3 | `cogrind-workshop --ingest tests/fixtures/sample_sources/sample.md` (against a running `cobalt-grinding`) → source page + entity pages + at least one glossary `ConceptPage` exist; `pages/glossary.md` exists as an `IndexPage` and is regenerated on subsequent ingests; code section pages include a deterministic symbol outline; a `.h` file's parse language follows the heuristic and the `--lang-h` override. Re-ingesting with a new file added produces only the new section page and grows the source page's `sections:` list. Same operations succeed via the `wiki.ingest` MCP tool from any client. |
| M4 | `cogrind-workshop --query "topic"` returns ranked pages; query for a known-absent topic returns a gap signal. Same retrieval available via the `wiki.search` MCP tool. |
| M5 | `cogrind-workshop --ask "what does X say about Y"` → cited answer; `citation_checker` validates citations point to supporting text. Same conversation works from Claude Desktop / Claude Code via `wiki.ask`. |
| M6 | A gap signal queued via `ebony.add_gap` (or an explicit `cogrind-workshop --research "<topic>"` / `wiki.research`) produces a `ProposalPage` in ebony-enriching's `proposals/research/` (verified via `ebony.list_proposals(system=research)`) with `proposal_kind: source_adoption`, observation, hypothesis, prediction, ranked candidate list, and (where cheap-tier) a test result captured via `ebony.write_experiment`; lifecycle status starts `proposed` or `validated` based on test outcome. Accepting a proposal triggers cobalt-grinding's cross-substrate orchestration: `smalt.write_page` (source page) + `ebony.update_proposal_status(applied)` + `ebony.remove_gap` + the apply-time post-mortem (`ebony.write_experiment(input={kind:post_mortem}, ...)`; on a medium/expensive-tier proposal also extending `pages/concepts/research-methodology.md` via `smalt.add_claim` if the lesson generalizes). No auto-ingestion. |
| M7 | `cogrind-workshop --cogitate` (or `wiki.cogitate`) walks the link graph (via `smalt.list_pages` + `smalt.traverse`) and writes `ProposalPage`s with proposal kinds (`wiki_edge`, `concept_merge`, `novel_concept`, `schema_addition`, `contradiction`) via `ebony.write_proposal`, landing in ebony-enriching's `proposals/cogitate/` (or `proposals/schema/` for schema kinds). Cheap-tier proposals are auto-tested with results recorded via `ebony.write_experiment` under `experiments/<proposal-id>/`. SME sub-agents (`observer`, `hypothesis_generator`, `predictor`, `experimenter`, `validator`) drive the lifecycle. No Smalt page modifications (apply step is cobalt-grinding's cross-substrate orchestration on user accept). Apply orchestration runs the post-mortem: every applied proposal produces an `ebony.write_experiment(input={kind:post_mortem}, ...)` record; medium/expensive-tier proposals additionally extend `pages/concepts/cogitate-methodology.md` (or a kind-specific pattern page) via `smalt.add_claim` when the lessons generalize. |
| M8 | `cogrind-workshop --curate` (or `wiki.curate`) walks the Smalt corpus (via `smalt.list_pages` + `smalt.read_page` + `smalt.incoming_links`) and writes `ProposalPage`s (orphans, duplicates, broken links, stale pages, schema-drift) via `ebony.write_proposal` into ebony-enriching's `proposals/curate/`. Each finding carries observation, hypothesis, and (for cheap-tier) a test result captured via `ebony.write_experiment`. No Smalt deletions or modifications until user accepts a proposal (then cobalt-grinding orchestrates `smalt.*` + `ebony.update_proposal_status` + apply-time post-mortem, lessons going to `pages/concepts/curate-methodology.md` via `smalt.add_claim` for medium/expensive-tier corrections). |

Per-milestone regressions: every previous milestone's smoke test must still pass.

Final Phase 1 acceptance: with a single `cobalt-grinding` running, ingest a curated mixed-format directory (markdown notes, plain text, PDFs, RTF, JSON / TOML configs, Python / C / C++ source files) plus a small local git repo and a small Obsidian vault (using only the file types M3's first impl supports), then have a 5-minute conversation with Converse via `cogrind-workshop --chat` *and* via Claude Desktop over MCP, both pointed at the same daemon. The conversation must demonstrate correct retrieval, correct citation, gap detection on out-of-corpus questions, and no hallucinated sources. The daemon stays warm throughout: ingestion of additional sources mid-conversation reuses the loaded fastembed model and LanceDB connection, and concurrent ingest + query + status calls interleave correctly. Glossary terms accumulate across the mixed-format ingest; the `pages/glossary.md` `IndexPage` is regenerated on every indexer run; ingest sub-agents reach every external capability through the M2.5 host (`await app.host.run_agent(...)`), with `deco-assaying` (separate project) autostarted as an MCP child and any other MCP children configured in `[mcp.clients.*]` discoverable via the host's `tools_index` retrieval.

## Decision record

| Date | Decision | Ruling |
|---|---|---|
| 2026-05-02 | The pattern | A wiki that a model compiles and maintains from its sources, rather than raw chunks retrieved at each query; extended with several agentic systems |
| 2026-05-02 | Sources of the first version | Text first: text and Markdown, structured configuration, markup, documents, code, private GitHub repositories, the keys of `.env` files; images, crawling, CSV and archives deferred |
| 2026-05-02 | The structure of sources | Kept as provenance, not as the primary index; pointers of the form `source@version:path#fragment`; a repository's directory tree and import graph both kept |
| 2026-05-02 | The store of record | Markdown canonical and every index derived; a pure database set aside |
| 2026-05-02 | The index | LanceDB for operations and DuckDB for analytics later; SQLite with extensions, Postgres, and separate search servers set aside |
| 2026-05-02 | Retrieval | Hybrid and in parallel, fused by reciprocal rank, with the metadata filter first, graph traversal as a mode of its own, reranking folded into the answer, a separate path for code, caching, a gap signal, and novel syntheses as a trigger for writing; sequential filtering set aside |
| 2026-05-02 | The agentic systems | Six orchestrators with sub-agents: ingestion, retrieval and the Sage in the first version, the custodian in 1.5, synthesis and the researcher in 2 |
| 2026-05-02 | The runtime | Python with the Claude Agent SDK, for its ecosystem, LanceDB's native Python and its orchestration of sub-agents |
| 2026-05-02 | Auditing systems | Propose, don't act, with a single writer to the corpus |
| 2026-05-02 | Images and multimodal sources | Deferred to version 2, text first giving about 80% of the value for under 40% of the effort |
| 2026-05-02 | Confidence | For each value, with its provenance, since data read from a chart is softer than data from a table |
| 2026-05-02 | The Sage | Bounded by what has been ingested, since an over-confident conversational agent is exactly the failure of invention the wiki exists to fix |
| 2026-05-17 | The plan's work plan | Milestones M0 to M9+ with their done-when lists, the planned layout and the verification table, as last revised; kept here as written |
