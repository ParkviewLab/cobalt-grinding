<!--
SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# The architecture: the reasoning, the alternatives and the directions set aside

This file is the second half of the record of cobalt-grinding's architecture. The line between the two is what a reader needs: [`architecture.md`](architecture.md) describes what the code does and how it is put together, and this file keeps what is needed only to reopen the design, which is the reasoning of the design conversation of 2026-05-02 that produced the first plan, the alternatives it weighed and the directions it set aside. The decisions made since, in the plan and in the code, are in [`decisions.md`](decisions.md). Entries are in date order and the record at the foot summarises them.

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

The conversation ended with eight questions to settle early in implementation. The plan and the code answered six ([`decisions.md`](decisions.md)):

- The embedding model: the conversation's default was Voyage 3 large, against OpenAI's text-embedding-3-large and Anthropic's; the plan chose local embeddings with fastembed the same day.
- A command line or a daemon: the conversation had a command line only, with a daemon arriving for the custodian in version 1.5; the plan made one daemon the architecture from its first milestones, the same day.
- The storage location: the conversation proposed `~/.cobalt/wiki/`, configurable, with one wiki in the first version; the plan chose a visible directory in the Documents folder, configurable.
- Controlling the cost of embeddings by caching on content hashes: the Smalt's embeddings became smalt-mcp's concern on 2026-05-17.
- The model of concurrency: the conversation had one process in the first version and a daemon with workers later; the plan chose a daemon with a thread pool from the start.
- The threshold for ingesting automatically: the researcher always proposes, and adding automatically was left to a later version, if ever; the plan decided the same (Research proposes only).

The other two are open in [`in-flight_ideas.md`](in-flight_ideas.md): a seed corpus of ten to twenty diverse sources for development and regression tests, and the detection of extraordinary claims, which requires a prior over what the wiki already holds and was to start simply (numeric outliers, single-source claims held with high confidence, strong qualifiers in the language) and grow only if the simple version did not suffice.

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
