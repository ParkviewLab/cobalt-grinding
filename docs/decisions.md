<!--
SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# cobalt-grinding: decisions

This is the record of cobalt-grinding's decisions: dated entries that describe what was decided and why, kept as history. Entries stand in the order they were made, oldest first, and an entry is not rewritten when a later decision changes it; the later entry records the change. Each entry names the decision, the reason as it was recorded, and the alternative set aside where one was recorded; where the record gives no reason, the entry says so rather than supplying one.

Each date is the day the decision was first recorded: in the plan the project was built from (from 2026-05-02 to 2026-05-17), in the investigation of deco-assaying's output (2026-05-04), in the change that carried the decision out (2026-05-17 and 2026-05-18), or in conversation (2026-09-27). The dates before 2026-06-13 come from the development history kept before the repository was published. Several of the plan's decisions were changed when the code was written, and the later entries say which; where a decision was neither carried out nor changed by a later decision, its entry ends with a note of what the code does instead, dated 2026-09-27, the day this record was compiled. The architecture the decisions produced is described in [`architecture.md`](architecture.md); the reasoning of the design conversation of 2026-05-02, from which the first entries come, is in [`architecture-why.md`](architecture-why.md); and what was decided for systems not yet built stays open in [`in-flight_ideas.md`](in-flight_ideas.md).

## 2026-05-02: Markdown is canonical, indexes are derived

Decided: the canonical form of the knowledge is Markdown files with YAML frontmatter; every index is derived from them and can be rebuilt from them. Reason: Markdown is readable and editable by a person, native to language models, diffable in git and portable, and it lets a person fix what the agent gets wrong. Set aside: a database as the store of record, and the other engines weighed in the design conversation of the same day ([`architecture-why.md`](architecture-why.md#2026-05-02-markdown-as-the-store-of-record-lancedb-as-the-index)).

## 2026-05-02: LanceDB as the primary index

Decided: LanceDB is the operational index, combining BM25, vector search and metadata filters, with versioning. Reason: it provides hybrid search in one engine built for this workload, its versioning is useful in development, and its Python SDK matches the agent layer. Set aside: SQLite with its vector and full-text extensions, Postgres, and separate search servers, weighed in the design conversation ([`architecture-why.md`](architecture-why.md#2026-05-02-markdown-as-the-store-of-record-lancedb-as-the-index)).

## 2026-05-02: DuckDB deferred

Decided: an analytical layer of DuckDB, reading the same Lance files, waits for Phase 2. Reason: analytics is a second step, and Phase 1 needs no cross-cutting SQL.

## 2026-05-02: Python, with the Claude Agent SDK

Decided: Python as the runtime and the Claude Agent SDK for orchestration. Reason: the strongest ecosystem for language models and agents, LanceDB's native Python SDK, and orchestration of sub-agents provided. Not carried out (found 2026-09-27): the daemon depends on the `anthropic` SDK and runs a tool-use loop of its own (the entry of 2026-05-03 on the host); the Claude Agent SDK is not a dependency.

## 2026-05-02: a single writer to the corpus

Decided: only Ingest writes pages; Curate, Cogitate and Research write proposals, into a `tasks/` directory. Reason: it avoids races and keeps the audit trail clear.

## 2026-05-02: propose, don't act

Decided: every system that audits or critiques writes proposals, never direct edits, and a person or Converse approves them. Reason: the cost of a wrong autonomous edit is greater than the cost of a slightly cluttered Smalt.

## 2026-05-02: the original structure kept as provenance

Decided: the organisation of every source (directory tree, table of contents, heading hierarchy) is captured at ingestion as provenance, inline in the source page's frontmatter, or in a sidecar file when it is large. Reason: locality is signal, and structure lost at ingestion cannot be recovered. Not carried out (found 2026-09-27): a directory's source-index page records its files, its ignored files and its git or Obsidian metadata, but no document's headings or table of contents are captured and no sidecar file is written.

## 2026-05-02: six systems, three in Phase 1

Decided: six agentic systems; Ingest, Retrieve and Converse in Phase 1, and Cogitate, Curate and Research in Phase 2. Reason: Cogitate and Research are the easiest to over-promise, and Curate needs an aged corpus to be useful, so the useful core comes first.

## 2026-05-02: image ingestion deferred

Decided: images and other multimodal sources wait for Phase 2. Reason: they add complexity out of proportion to their value in Phase 1; by the estimate recorded, text first gives about 80% of the value for under 40% of the effort.

## 2026-05-02: confidence and provenance for each value

Decided: confidence and provenance are kept for each value, not only for each page. Reason: a figure read from a chart is softer than one taken from a table, and the index needs to know the difference. Not carried out (found 2026-09-27): the daemon writes no claims or values of its own; its pages carry a source's location and hash, not a confidence for each value.

## 2026-05-02: no source copies

Decided: the Smalt stores no copy of what it ingests, only the source's location, its content hash at fetch time, the time of the fetch, its structure and the notes made about it; re-verifying a claim means fetching the source again. Reason: the Smalt is notes about sources, not a republication of them; this avoids concerns about redistribution, keeps the store small, forces discipline about source pointers and matches how a researcher works. The cost accepted: re-verification needs a fetch, and a pointer to a local file holds only on that machine.

## 2026-05-02: an MCP server from the start

Decided: the MCP server is built in Phase 1, from its first milestone, and each system exposes its tools as it is built. Reason: it gives a chat interface through Claude Desktop and Claude Code at almost no extra cost, and makes a custom interface unnecessary in Phase 1.

## 2026-05-02: local embeddings with fastembed

Decided: embeddings are local by default, with fastembed and `BAAI/bge-small-en-v1.5` (384 dimensions); hosted providers (Voyage, OpenAI) remain available through configuration. Reason: no API key, no cost per token, it runs offline, and private notes never leave the machine to be embedded. This settled the first of the plan's open questions. Not carried out in part (found 2026-09-27): the Smalt's embeddings are smalt-mcp's (the entry of 2026-05-17 on the storage); the daemon embeds only tool descriptions, with this default, and refuses the hosted providers when its agent runtime starts.

## 2026-05-02: TOML for configuration, in layers

Decided: the runtime configuration is TOML, in layers (built-in defaults, a user-global file, a file in the Smalt's directory, environment variables and command-line flags, the last winning); Markdown is kept for what agents read. Reason: configuration is typed key-value plumbing, which TOML is made for, whereas Markdown serves the documents in which prose and legibility to a model matter (the schema, the policy and the pages).

## 2026-05-02: secrets outside the configuration

Decided: an API key is never written to the configuration file; the file names the environment variable that holds it (`api_key_env`). Reason: it keeps secrets out of files, version control and backups, and works with direnv, 1Password's command line, pass and AWS Secrets Manager.

## 2026-05-02: the Smalt in the Documents folder

Decided: the default directory of the corpus is in the user's Documents folder, not in a hidden directory. Reason: it is visible in Finder or Explorer and opens in VS Code or Obsidian without remembering a hidden path; the point of a Markdown store is that people read its files. The directory was `~/Documents/Cobgrind` then, and became `~/Documents/Smalt` on 2026-05-17.

## 2026-05-02: no inline content

Decided: in Phase 1 every source has a stable location; content without one, such as a pasted e-mail or a chat log, is saved to a file first and the file is ingested. Reason: not recorded.

## 2026-05-02: no browsing interface of its own

Decided: CoGrind has no interface for browsing the corpus. Reason: the corpus is plain Markdown, which any Markdown editor (VS Code, Obsidian) already browses.

## 2026-05-02: Research proposes and never ingests on its own

Decided: Research, in its first implementation, only proposes sources; a person accepts or rejects each one, and an automatic mode may come later, once its judgement has proved trustworthy domain by domain. Reason: a poor source ingested automatically pollutes synthesis and contaminates every answer downstream, so a person's accept-or-reject step buys a great deal of safety.

## 2026-05-02: one daemon, systems in process, threads for parallelism

Decided: CoGrind runs as one long-running daemon that holds every system in its own process and parallelises with threads. Reason: one copy of the embedding model in memory (about 100 MB), one LanceDB connection, calls between systems as function calls, and one process to keep alive; threads give real parallelism because fastembed's ONNX runtime, LanceDB, pyarrow and network I/O release the GIL. Pools of subprocesses may come later for particular operations prone to crashing, such as some parsers, but not as the default. Set aside: a daemon for each system, and a subprocess for each system.

## 2026-05-02: a task scheduler from the start

Decided: the daemon has a task scheduler with a pool of workers from its first milestone, so that a long operation returns a task id and runs in the background. Reason: it sets the right factoring (no module-level globals, an injected `App`, asynchronous tasks) before ingestion needs it, so that ingestion is backed by the daemon from the start rather than retrofitted.

## 2026-05-02: two programs, one protocol

Decided: CoGrind is two programs, a daemon and a command-line client, with MCP as the only protocol between them. The client is purely an MCP client, with no business logic and no mode that works without the daemon; it fails at once, with a clear message, when no daemon runs; and initialising the corpus is part of the daemon's start-up, not a command of the client. Reason: the shape of `claude` and Claude Code, a polished human interface over a backend; one source of truth and no duplicated logic; no two instances racing on the corpus; no hazard of running an initialisation command in the wrong order; and every client, the command line included, uses the same surface. Set aside: a client that does the work itself when no daemon runs.

## 2026-05-03: the daemon as an MCP host

Decided: the daemon is an MCP host as well as a server: it starts and supervises child MCP servers, and its own agents reach external capabilities through them. Reason: every system is agentic and most will need external capabilities (parsers for new formats, web search, fetchers, archive lookups); establishing the host once gives each new capability a uniform way in rather than a retrofit, and the protocol CoGrind already serves is the natural one in which to consume capabilities.

## 2026-05-03: capability and infrastructure

Decided: a capability, anything done to external content (parsing a file, extracting the text of a PDF, searching the web), is an MCP child reached through the host; infrastructure, anything that keeps CoGrind's own state (LanceDB queries, embedder calls, page writes, the model client), stays a direct import. Reason: ingestion grows by adding capabilities, and this keeps that growth a matter of configuring a child rather than releasing CoGrind; sending infrastructure through MCP would add serialisation to hot paths for no gain, since infrastructure is not an extension point.

## 2026-05-03: the host runs the tool-use loop

Decided: the host runs the model's tool-use loop, and an agent sees only `run_agent(system, messages)`: the host chooses the tools, dispatches each call to the child that owns it, feeds the results back and returns the final message. Reason: running that loop is an MCP host's defining job and is the abstraction agents want; an earlier draft, in which agents called tools directly through a `ToolBundle`, reinvented it and coupled agent code to the tool registry. Set aside: the `ToolBundle`. Goose was studied as a precedent but not used as a library, being written in Rust and shaped as a coding agent's command line; from it came a conversation for each invocation, progress events, and a thin provider interface, Anthropic's being the first.

## 2026-05-03: tools chosen by hybrid retrieval

Decided: the host chooses the tools for each call by hybrid retrieval over an index of the children's tool descriptions: BM25 and vector search, fused by reciprocal rank, ten tools by default. Reason: the retrieval machinery already existed; the approach scales from five tools to five hundred without a change of code, and keeps agents independent of the registry. Set aside: tool lists declared by the caller, and Goose's Tool Router (a preview available only on Databricks), studied as the precedent.

## 2026-05-03: failed calls returned to the model

Decided: a failed tool call becomes a `DispatchResult` with `ok` false and `is_error` true inside the host, and reaches the model as a `tool_result` with `is_error` set. Reason: the model is what must recover, by retrying, choosing another tool or ending the turn, and this lets it do so without every call being wrapped in exception handling; telling a failed dispatch from a tool that reported an error is the supervisor's concern, not the agent's.

## 2026-05-03: code parsing in its own project

Decided: code parsing lives in a separate project, first named `parkview-codeparse-server` and named deco-assaying from the next day, not in CoGrind. Reason: parsers are capabilities; a separate repository grows its language coverage on its own release cadence, takes contributions without changes to CoGrind, and proves that any later parser or extractor plugs in the same way. Set aside: [`mcp-code-parser`](https://github.com/boxabirds/mcp-code-parser), studied as a precedent; a parser of CoGrind's own was built instead, for broader language coverage and an output shaped for ingestion.

## 2026-05-03: a tool prefix for every child

Decided: every child has a tool prefix, by default its section name in the configuration. Reason: two children exposing the same tool name, a code parser and a PDF parser both offering `parse_file` for instance, would otherwise collide silently, and log lines such as `tool call deco-assaying.parse_file failed` stay unambiguous.

## 2026-05-03: children started with the daemon, with timeouts

Decided: the daemon starts its children when it starts, unless a child sets `autostart = false`; each child has a start-up timeout (10 seconds by default) and a timeout for each call (30 seconds); a crashed child is restarted after a delay that doubles from 1 second to at most 60; and a call that times out does not restart its child. Reason: a long-running daemon pays the start-up cost once, keeps the latency of requests flat and shows a misconfigured child early; the start-up timeout keeps a wedged child from holding up the daemon, the call timeout keeps a wedged tool from holding up an agent, and a slow child is not a crashed one.

## 2026-05-03: ingestion in separate steps

Decided: ingestion runs focused steps (a summary, the entities, the glossary terms, the resolution of links against existing pages) as separate model calls, not one combined call for each file. Reason: cleaner prompts, each step evaluated on its own and easily swapped or skipped; the three or four times as many calls were to be offset by a small, fast model for the focused steps and by caching keyed on content hashes. Not carried out in full (found 2026-09-27): the summary, entity and glossary steps are separate calls, but all three use the configured model, nothing is cached, and there is no step that resolves links against existing pages.

## 2026-05-03: a glossary for the whole Smalt, of concept pages

Decided: ingestion builds one glossary for the whole Smalt, each entry a concept page with `glossary: true` rather than a page of a new type; new terms are added, known terms gather evidence, and nothing is pruned in Phase 1. Reason: a coherent vocabulary makes the corpus describe itself, and corroboration across sources adds value to definitions; a glossary term is a concept, so one flag suffices where a new page type would need plumbing of its own; pruning is left to Curate. Not carried out in full (found 2026-09-27): every term found becomes a new concept page, so a known term found again adds a page rather than evidence on the existing one.

## 2026-05-03: index pages, and the layout of source pages

Decided: generated indices, the glossary first, are pages of their own type (`IndexPage`), whose bodies the indexer rewrites from a stored query; a source of one file is one page, and a source of several files is an index page with a section page for each file. Reason: an index has different semantics from an authored page, and no other page is ever rewritten automatically; the layout mirrors the source's own shape, adds no directory for a single file, and can be browsed as it stands in VS Code or Obsidian.

## 2026-05-03: the scope of entities

Decided: in the first ingestion, entities are people, organisations, products, repositories and packages; functions, classes and files are not entities, and code symbols appear in the body of the section page instead. Reason: it keeps the entity space from filling with rows of little value on their own; a symbol can be promoted later if Cogitate finds that it matters across sources. Not carried out exactly (found 2026-09-27): the extraction prompt also admits places and named concepts; it excludes code identifiers as decided.

## 2026-05-03: ingesting again adds new files only

Decided: ingesting a source again in Phase 1 processes only the files new since the last run; existing section pages are not rewritten even when their file has changed; the source page is regenerated; entity and concept pages gain back-links additively; and a run with nothing changed changes nothing, through the indexer's check of content hashes. Reason: it matched the scope of the first ingestion and keeps ingestion idempotent; detecting changed content by hash was left until after Phase 1.

## 2026-05-03: the language of `.h` files decided once for each directory

Decided: whether the `.h` files of a directory are C or C++ is decided once for each directory ingest, by a heuristic (C++ if sibling files end in `.cpp`, `.cc`, `.cxx` or `.hpp`, or if a header's text contains `namespace`, `class`, `template` or `extern "C"`; C otherwise), which a `--lang-h` option overrides. Reason: it is cheap, predictable and auditable, and avoids treating the files of one source differently. Not carried out (found 2026-09-27): there is no heuristic; `wiki.ingest` takes `h_lang` from its caller, `c` by default, and applies it to every `.h` file.

## 2026-05-04: Toolsmith, a seventh system, in Phase 3

Decided: a seventh system, Toolsmith, does for capabilities what Research does for knowledge: given a tool-gap signal from the other systems, it finds an existing MCP server that fits, or writes the requirements of one to be built, and proposes it; it comes in Phase 3. Reason: CoGrind grooms its own Smalt, and Phase 3 extends the same discipline to its own capability surface; Toolsmith needs the first two phases running, to have agents to observe and an inventory of tools to judge against; until it exists, people and Claude do its work, deco-assaying being the first case; it only proposes, because adding an MCP server changes security, the supply chain and behaviour.

## 2026-05-04: a tool's analysis of a source is not a copy of it

Decided, in the investigation of deco-assaying's output that day (a self-index of deco-assaying's own repository, compared with what the plan expected of it): the rule against source copies covers the ingested artifact itself, the file at its location, and not analysis that a tool derives from it; a capability child's output may be stored in pages at discretion, and the same holds for any later extractor (PDF text, OCR, archives). Reason: a tool's output is analysis of the source, not the source; notes are notes whether a tool or a model wrote them, so the question for storing such output is whether it will be useful, not whether it is allowed. Set aside: the investigation's first recommendation, not to store deco-assaying's chunk text on the strength of that rule.

## 2026-05-04: code symbols stay out of entity pages; parser flags as signals

Decided, in the same investigation: deco-assaying's repository-wide symbol index does not change the scope of entities, so code symbols are not promoted to entity pages automatically; and its `is_generated`, `is_test` and `is_config` flags are informative, usable in a section page's frontmatter but never grounds for leaving a file out of an ingest. Reason: the rule of 2026-05-03 on the scope of entities stood; the flags are heuristics, useful as signals but not authoritative. Not carried out in part (found 2026-09-27): the daemon neither requests nor records the flags.

## 2026-05-05: proposals are hypotheses

Decided: every proposal, from every system, is a hypothesis with a falsifiable prediction: it carries an observation, a hypothesis, a prediction and a test, and moves through the statuses proposed, under test, validated, rejected, applied and superseded. Where a test is cheap, the system runs it before a person reviews the proposal, and the person reviews the hypothesis with its evidence; where a test is expensive or impossible, the proposal is marked untestable with a reason, and the person is the test. An applied finding can return to proposed when new evidence contradicts it, and a person's own edits to `SCHEMA.md` and `POLICY.md` meet the same bar. Reason: review of evidence rather than of opinion; truth kept provisional, which makes removing a schema field possible as well as adding one; Curate audits the rate of untestable proposals as a sign of slipping discipline; and the discipline applies to the system's own structure, so that the Smalt records how it knows as well as what it knows.

## 2026-05-05: SCHEMA.md and POLICY.md are living documents

Decided: the schema and policy documents are seeded by people and Claude on day 0 and thereafter groomed through proposals: Cogitate proposes additions, Curate flags drift and candidates for removal, a person approves, and the change is applied; the documents stay in step with the schema's Pydantic models, checked on each pass of the indexer, without generating one from the other. Reason: the Smalt documents its own structure and policies as well as its content, and both grow by the same discipline.

## 2026-05-05: a glossary across domains, with domains as concept pages

Decided: one meaning used in several domains is one concept page listing its domains in `domains:`; different meanings of one term are separate pages, whose slugs carry a readable distinction (`tree-data-structure`, `tree-plant`) and never a domain; a domain is itself a concept page with `is_domain: true`, and domains nest by `subdomain_of` links rather than through `domains:`; sources and entities carry `domains:` in the same way; a generated page lists the domains; and the ingestion agent assigns domains from the source's own domains, the surrounding text and the term itself, marking an uncertain assignment `domain_confidence: low` for Curate to review. Reason: what a page is about stays separate from what it sits under, and no separate taxonomy file is needed. Not carried out (found 2026-09-27): the daemon writes every page with an empty `domains` list and assigns no domains; smalt-mcp's schema and its generated page of domains support the model.

## 2026-05-05: the release checks the tag against the version

Decided: a release's CI verifies that the tag matches the version in `pyproject.toml` before anything is published, as a standing pattern for ParkviewLab's Python projects. Reason: a release driven by a tag could otherwise publish a package whose metadata disagrees with its tag, a state hard to notice and hard to undo cleanly.

## 2026-05-16: two substrates, smalt-mcp and ebony-enriching

Decided: the records of the scientific method (proposals, experiments and gaps) leave smalt-mcp for a second MCP server, ebony-enriching, the lab notebook, and neither server depends on the other; each substrate has its own pair of `SCHEMA.md` and `POLICY.md`, and the rules of falsifiability and cost tiers live in the notebook's `POLICY.md`. Reason: the storage of knowledge and the scientific record differ in shape (smalt-mcp is backed by LanceDB and oriented to search; the notebook is text on the file system, appended to with changes of status), so LanceDB and an embedder are wasted on a small workload of proposals; the two evolve at different rates; each separable concern becomes its own child, as capabilities do; and two smaller surfaces (17 tools and 13) develop in parallel and have cleaner permissions than one of 30. This changes the entry of 2026-05-02 on a single writer: proposals go to the lab notebook, not to `tasks/`, and the entry of 2026-05-05 on the living documents, which become one pair for each substrate.

## 2026-05-16: the lab notebook records and does not enforce

Decided: ebony-enriching stores proposals, experiments and gaps; it does not enforce falsifiability, run experiments or decide what to apply; cobalt-grinding's agents read the notebook's `POLICY.md`, follow it, and write what they did through the notebook's tools. Reason: it keeps the notebook small (files and Markdown, with no LanceDB, no embedder and no model client) and mirrors how scientists work: the notebook holds the record, and the scientist the method.

## 2026-05-16: cobalt-grinding orchestrates the apply

Decided: neither substrate has an `apply_proposal` tool; applying a validated proposal is a sequence of calls made by cobalt-grinding: write the Smalt page, mark the proposal applied, and remove the gap it answered, if any. Reason: such a tool on ebony-enriching would make it depend on smalt-mcp's surface, breaking the rule that neither substrate depends on another; the orchestration is a few lines on cobalt-grinding's side, and any reusable form of it belongs there, not in either server.

## 2026-05-16: a post-mortem on every applied proposal

Decided: when cobalt-grinding applies a validated proposal, it also reviews the proposal's journey from its start to its validation (its record, its experiments, the proposals it superseded and related proposals that were rejected); it writes the lessons to the notebook as an experiment record, and, where the lessons generalise, adds them to a methodology page of the Smalt with `add_claim`, or proposes a synthesis page through the ordinary loop of proposals; the depth of the review follows the proposal's cost tier. Reason: without it, the record of proposals is inert data; with it, each apply updates the system's judgement about how to propose, and the methodology pages inform the next run; the record exists even when nothing is kept, so that Curate can detect a system that is not learning.

## 2026-05-17: the client and the storage leave this repository

Decided, in the work of the milestone the plan called M2.7: the command-line client moves to its own repository, [cogrind-workshop](https://github.com/ParkviewLab/cogrind-workshop), and this repository ships only the daemon; the Smalt's storage (its schema, indexer and LanceDB tables) moves into the smalt-mcp child, reached by MCP like any other, and `wiki.status` and `wiki.index` become proxies for smalt-mcp's tools; the daemon keeps a state directory of its own for the tools index; and the host starts in two phases, the children first and the model's runtime only when an API key is present. Reason: LanceDB is local to one process, so two processes cannot share a store; starting the children without a key lets the daemon serve the tools that need no model. This changes the entry of 2026-05-02 on two programs, whose client now lives in another repository, and places the index of 2026-05-02 inside smalt-mcp.

## 2026-05-17: names aligned with the sibling servers

Decided: the package becomes `cobalt_grinding`, in a `src/` layout; the daemon's program becomes `cobalt-grinding`, the repository's name; the key of the Smalt's directory becomes `smalt_dir`, with the default `~/Documents/Smalt` and the flag `--smalt`; the environment variables take the prefix `COBALT_GRINDING_`; and no aliases are kept for the old names. Reason: the sibling servers' convention that a program bears its repository's name; the directory belongs to smalt-mcp, which reads the same `SMALT_DIR`; and there had been no public release, so nothing depended on the old names.

## 2026-05-17: every substrate and capability in the default configuration

Decided: ebony-enriching and deco-assaying join smalt-mcp in the default configuration, with `ebony_dir` given to ebony-enriching as `EBONY_ENRICHING_DIR`; bootstrap calls both substrates' `bootstrap` tools and does not wait for a child whose `autostart` is false; and over HTTP the daemon wraps FastMCP's lifespan in its own, so that its start-up and bootstrap run before the server accepts a connection. Reason: the plan's first milestone required all the children in the default configuration; FastMCP's HTTP application ignored the lifespan passed to it, so that over HTTP the host started only when first used.

## 2026-05-17: `wiki.ingest` returns when the ingest is done

Decided, with the first ingestion: `wiki.ingest` awaits the pipeline and returns its result, instead of submitting it to the scheduler and returning a task id; background tasks were left for when ingesting many files made the wait noticeable. Reason: not recorded beyond that condition. This changes, for ingestion, the entry of 2026-05-02 on the task scheduler.

## 2026-05-17: the source's text kept in its page

Decided, with the first ingestion: a source page's body holds the source's text, up to 1 MiB, in a fenced block; first as a placeholder and, from the next change the same day, beneath the model's summary. Reason: traceability, as the code records it. This departs from the entry of 2026-05-02 on source copies; the departure was reviewed on 2026-09-27.

## 2026-05-17: extraction calls the model directly

Decided: the summary, entity and glossary steps call the model's provider directly rather than through the host's `run_agent`; a later step that needs tools is to use `run_agent`. Reason: these steps are pure extraction and should reach for no tool; `run_agent` would offer the tools the index ranks highest, and the model might call one, `smalt.search` for instance, which is wrong in an extraction pass; a direct call is the smallest possible, one round trip without the loop, so it is faster and cheaper. This changes, for these steps, the entry of 2026-05-03 on the host running the loop.

## 2026-05-17: ingesting again by content hash; duplicates left to Curate

Decided: the SHA-256 of a file is stored on its source page, and an ingest of the same file is skipped when the hash is unchanged; an entity page is always created anew, so an entity found in several sources has several pages, and removing the duplicates is left to Curate. Reason: the rule of hashes was a decision on the scope of that phase of the work, recorded without a further reason; duplicate entities were accepted for simplicity. This changes the entry of 2026-05-03 on ingesting again.

## 2026-05-18: directories: one lookup, sections in sequence

Decided, with the ingestion of directories: an ingest looks for an earlier ingest of the same directory at the level of its source-index page only, a skip for each unchanged section being left as a later optimisation; the sections are processed one after another, processing them in parallel being left for later; and a directory ingested again gets a new index page, the old one being left as an orphan for Curate to remove. Reason: section pages, whose identifiers join the index page's identifier and the file's path, are written in place by smalt-mcp; the model calls for each section dominate the time, so parallel processing was expected to give a large gain later; removing duplicates belongs to Curate. Not carried out as intended (found 2026-09-27): because each ingest's index page has a new identifier, its section pages have new identifiers too, so the sections of an earlier ingest are not rewritten in place but remain beside the new ones.

## 2026-05-18: search returns what smalt-mcp finds

Decided, with search: the hits come from `smalt.search`, which the daemon does not re-rank; graph expansion is off unless asked for, and then starts from the three best hits only; when nothing matches, a gap is flagged in the result and never reported to the notebook automatically; smalt-mcp's filters pass through; a failed traversal from one hit is not fatal; and caching of results is left for later. Reason: ranking is smalt-mcp's job; limiting the starting points keeps a dense graph from multiplying the calls; and one query that found nothing is not a request to record a gap for Research, so the caller decides.

## 2026-05-18: answering in one round trip

Decided, with question answering: `wiki.ask` makes one retrieval pass and one model call, and checks the citations deterministically against the pages it gave the model, rather than running a loop in which the model searches through `run_agent`; each page is cut to 4,000 characters, and at most eight pages are used; follow-up questions carry the earlier turns in `prior_messages`, and only the current turn's pages count as valid citations. Reason: Phase 1's bar was an answer with citations from a fixed retrieval pass, or an admission that the Smalt does not know, and the agentic version was left as an enhancement for Phase 2; the limits keep the token budget below the point of diminishing return; and without the earlier turns a follow-up such as "tell me more" had no context.

## 2026-05-18: deco-assaying called directly, one file at a time

Decided, with the outlines of code symbols: each code file is sent to deco-assaying's `analyze_file` by a direct call through the supervisor, not through `run_agent`, with its content, name and language and without chunks; the answer is rendered as a short outline of the symbols (kind, name and first line), the imports, and the parse status when the parse was not clean; and when deco-assaying cannot be reached, the page is written without the outline. The child is configured as `[mcp.clients.deco-assaying]` with the prefix `deco`, as the investigation of 2026-05-04 had asked, and its tool is `analyze_file`, not the `parse_file` the plan had expected. Reason: parsing a code file is mechanical extraction, not discovery by a model, since both the tool and the use of its answer are known in advance; the route through `run_agent` remains for an agent that has to decide whether to parse at all. This answered the investigation's questions: the call for each file was taken; the investigation's preference for `index_repo` over a whole directory, for the repository-wide summaries, was left as a later enhancement; and its richer rendering (signatures, module documentation, references as links, metrics) and its proposal to give the chunks to the summariser were not taken up. These stay open in [`in-flight_ideas.md`](in-flight_ideas.md).

## 2026-05-18: repositories by shallow clone

Decided: a git URL is cloned with `--depth 1` into a temporary directory, with a limit of five minutes, and ingested as a directory, the clone's `origin` giving the source its location so that a later ingest of the same repository finds it; the call blocks until the ingest is done, progress reporting being left for when MCP's progress notifications are wired. Reason: ingestion needs the working tree, not the history; a repository too large to clone in time can be cloned by hand and ingested from its path.

## 2026-05-18: PDFs through flint-slating

Decided: PDFs are read by the flint-slating child, added to the default configuration: a local PDF's text with `pdf_read_text`, which gives the plain text that pypdf extracts; a PDF URL, recognised by its extension and checked before git URLs, through flint-slating's own fetch, with `pdf_info` for its hash, metadata and title; the hash of a local PDF computed by the daemon, like any other file's; and a local PDF that flint-slating cannot read written without its text. Reason: plain text was the first cut, at the cost of headings, tables and the reading order of columns, and Docling's Markdown (`pdf_read_markdown`, which runs as an asynchronous job for longer documents) is the path to improve it; computing the hash locally keeps re-ingestion consistent with other files; for a URL, the hash from `pdf_info` lets an unchanged PDF be skipped.

## 2026-05-18: the image bundles the children

Decided: the container image installs the sibling MCP servers beside the daemon, points the three directories at a `/data` volume and binds HTTP to all interfaces on port 7474, and the API key is given at run time. Reason: `docker run` then starts a daemon with the Smalt, the lab notebook and the code parser ready, with nothing more to install. flint-slating was not installed then.

## 2026-09-27: documents describe the code, and ideas what it should do

Decided, in conversation: the repository's in-flight ideas, and the organisation's proposals and tangents, record what the code should do, and every other document describes what the code does. The plan the project was built from (`docs/plan.md`) was therefore divided: what the code does went to [`architecture.md`](architecture.md), rewritten from the code; its decisions to this record; and what it proposed but did not build to [`in-flight_ideas.md`](in-flight_ideas.md); its work plan was dropped, git keeping it. The investigation of deco-assaying's output (`docs/m3-deco-assaying-findings.md`), whose questions the code had answered, gave its decisions to this record. The record of the design conversation of 2026-05-02 (`docs/ideation.md`) became [`architecture-why.md`](architecture-why.md), the record behind `architecture.md`, and its open points went to `in-flight_ideas.md`. The planning brief and the analysis of candidate MCP servers (`docs/existing_MCP_servers_to_consider/`), which record what the code might do, became the notebook [`mcp_servers_to_consider_ideas.md`](mcp_servers_to_consider_ideas.md), and the question and answer on what an agent should know about its model (`docs/what_an_agent_needs_to_know.md`) became the notebook [`agent_self_knowledge_ideas.md`](agent_self_knowledge_ideas.md). Reason, in the ruling's words: "in-flight, proposals and tangents are for recording what code should do. other docs in the repo should doc what the code does do."

## 2026-09-27: the source's text in pages left as it is

Decided, in conversation: the difference between the northstar, which says that CoGrind never copies the sources it ingests, and the code, which keeps each source's text in its pages, is left as it is; neither is changed, and the question is recorded as a tangent. Reason, in the ruling's words: the pages should be "keeping links to sources for tracability, but then how does it get those links? that's a problem for the future." Ruled the same day for every such difference: no code and no northstar is changed to make the two agree; each difference is recorded as a tangent.

## 2026-09-27: flint-slating in the image

Decided, in conversation: the image installs flint-slating beside the other three children, and the README names four. Reason: the daemon starts four children by default and flint-slating is its only reader of PDFs, so the image had no reader of PDFs. Set aside: not recorded.
