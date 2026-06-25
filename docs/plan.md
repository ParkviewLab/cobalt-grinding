# Cobalt Grinding — Phase 1 Plan

> **This is a living plan.** It will be revised as we learn from implementation — at minimum after each milestone, and possibly mid-milestone when a discovery changes the picture. Significant changes (scope, system boundaries, naming, technology choices) get discussed before being made; smaller refinements (clarifications, fixes, additional verification steps) can be applied directly. Revision history is in git.
>
> **Naming note (post-M2.7):** Earlier drafts of this plan named the daemon `cogrindd` and a sibling CLI `cogrind`. The M2.7 cleave collapsed that into one binary per repo: the daemon is now **`cobalt-grinding`** (shipped here); the CLI was extracted to the sibling repo **`cogrind-workshop`**. Historical references to `cogrind init` / `cogrind status` / `cogrind mcp serve` describe the old combined-CLI surface — those verbs were dropped during the cleave; their function lives in `cogrind-workshop --status` / `wiki.*` MCP tools / the `cobalt-grinding` daemon binary itself.

## Context

Cobalt Grinding is an agentic LLM-Wiki system: a personal knowledge base that ingests heterogeneous sources (text, code, configs, docs, repos), digests them into an interlinked corpus of markdown pages with structured metadata, and serves the result through retrieval + a conversational interface. It follows Karpathy's LLM-Wiki pattern — markdown-canonical, LLM-maintained, human-readable — extended with a multi-agent architecture: **Ingest** and **Retrieve** and **Converse** in Phase 1, with **Cogitate**, **Curate**, and **Research** added in Phase 2.

The goal of Phase 1 is **a usable Smalt you can ingest into and talk to**. The maintenance and growth agentic systems come in Phase 2.

---

## Architecture overview

**Two substrates, both served by separate MCP servers; cobalt-grinding owns no storage layer.**

**Smalt substrate (canonical knowledge) — served by [`smalt-mcp`](https://github.com/ParkviewLab/smalt-mcp):**
- `smalt/pages/` — markdown files with YAML frontmatter; the canonical knowledge corpus
- `smalt/structures/` — sidecar JSON files for source structures too large to inline (TOC, dir tree, heading hierarchy)
- `smalt/schema/` — `SCHEMA.md` (data shape) + `POLICY.md` (agent behavior) + Pydantic models inside `smalt-mcp`
- `smalt/index/lance/` — LanceDB store, derived, rebuildable
- `SMALT_DIR` env var (default `~/Documents/Smalt/`)

**Sciencing substrate (research-in-flight) — served by [`ebony-enriching`](https://github.com/ParkviewLab/ebony-enriching), the "lab notebook":**
- `proposals/` — ProposalPages (Cogitate / Curate / Research / Toolsmith / Converse-novelty outputs)
- `experiments/<proposal-id>/<run-timestamp>.md` — the experimental record
- `gaps.md` — knowledge-gap signals queued for Research to process
- `schema/{SCHEMA,POLICY}.md` — the lab notebook's own schema + falsifiability/cost-tier discipline
- `EBONY_ENRICHING_DIR` env var (default `~/Documents/EbonyEnriching/`)

Both substrates have **zero outbound dependencies**. Cobalt-grinding's cognitive systems read from both and orchestrate any cross-substrate writes (e.g., apply-a-validated-proposal-to-the-Smalt = `smalt.write_page` + `ebony.update_proposal_status(applied)` in sequence). Neither server knows the other exists.

**No source copies.** CoGrind does *not* store the original files it ingests. The Smalt captures: (a) a stable **location pointer** to where the source lives (URL, file path, `gh:owner/repo@sha:path:lines`), (b) the **content hash** at fetch time (so drift is detectable), (c) the **fetch timestamp**, (d) the **original structure** of the source (metadata about how it was organized, not the source itself), and (e) the **notes the system made** (summaries, entities, claims, links). Re-verification of a claim against its source requires re-fetching the source from its location.

**Engine choices:**
- Markdown + frontmatter as canonical
- **LanceDB** as the Smalt's operational index (hybrid BM25 + HNSW + metadata, time-travel) — lives inside `smalt-mcp`; ebony-enriching is text + filesystem only, no LanceDB
- **DuckDB** added in Phase 2 as a read-only analytical layer over the Smalt's Lance files
- **Python** runtime, Claude Agent SDK for orchestration
- **MCP children:** `smalt-mcp` (storage substrate), `ebony-enriching` (lab notebook), `deco-assaying` (code parsing) — three separate ParkviewLab repos, each with its own release cadence

**Surfaces (Phase 1):**

Two binaries, one protocol (MCP) between them:

- **`cobalt-grinding`** — the **daemon**. Long-running. Hosts the MCP server, the task scheduler, the worker pool, and all shared resources (fastembed model, LanceDB connection, LLM client). Owns every Smalt operation. Auto-initializes its configured Smalt directory on startup if it's empty.
- **`cogrind-workshop`** — the **CLI** (sibling repo [ParkviewLab/cogrind-workshop](https://github.com/ParkviewLab/cogrind-workshop)). A polished human interface that's *purely an MCP client* talking to the daemon over HTTP. Translates flags / args / prompts into MCP tool calls; renders results for a terminal. Initially flag-driven (`cogrind-workshop --ingest /path`, `cogrind-workshop --ask "..."`); evolves into an interactive REPL later.
- **MCP server** — the daemon's only external interface. Same tool surface for the CLI, for Claude Desktop, for Claude Code, for any future client. HTTP transport for long-running daemon clients (CLI), stdio for child-process clients (Claude Desktop).
- **No custom browse UI** — the corpus is plain markdown and is browseable in any markdown editor (VS Code, Obsidian, etc.).

**Process model: one daemon, one CLI binary, MCP between them; MCP children for every storage and capability surface.**

CoGrind runs as **one long-running daemon** (`cobalt-grinding`) that hosts all seven agentic subsystems as in-process modules **and supervises three child MCP servers** for its external state and capabilities: `smalt-mcp` (the storage substrate; SMALT_DIR), `ebony-enriching` (the lab notebook; EBONY_ENRICHING_DIR), and `deco-assaying` (stateless code parsing). The daemon owns the cognitive logic; the children own the data and capabilities. The CLI is purely an MCP client to the daemon's `wiki.*` server-side tools.

The daemon owns:

- **Shared resources loaded once**: the fastembed model, the LanceDB connection, the LLM client.
- **An `App` object** (`src/cobalt_grinding/app.py`) that holds these resources; every subsystem and tool handler takes one as a dependency. No module-level globals.
- **A task scheduler with a worker pool** (`src/cobalt_grinding/daemon/`): asyncio event loop + thread-pool executor. Long-running operations (M3 ingestion, M6 research, M7 cogitate, M8 curate) enqueue tasks, return `task_id` immediately, and the daemon runs them on workers. MCP tools `wiki.task_status` / `wiki.task_list` / `wiki.task_cancel` expose progress.
- **Single-writer discipline preserved**: the corpus-write step (atomic page write + index update) goes through one mutex inside the daemon so only one ingest commits to disk at a time. The expensive parts (LLM calls, fastembed inference, parsing) parallelize on workers; only the commit serializes.
- **Bootstrap on startup**: if the configured `smalt_dir` is empty or missing, `cobalt-grinding` creates the canonical layout (pages/, structures/, schema/, index/, tasks/), drops in fresh `SCHEMA.md` / `POLICY.md` templates, and creates the empty LanceDB tables — then begins serving.
- **MCP host role**: the daemon speaks MCP **both ways** and runs the LLM tool-use loop on behalf of its own agents. Server-side it exposes `wiki.*` tools to external clients (Claude Desktop, Claude Code, the CLI). Client-side it spawns and supervises configured child MCP servers (`[mcp.clients.<name>]`), captures each child's tool list at handshake, and indexes those tools into a LanceDB `tools_index` for hybrid (BM25 + vector) retrieval. CoGrind's internal agents call `await app.host.run_agent(system, messages, ...)`; the host (a) retrieves the top-K tools relevant to the message via the index, (b) hands them to the LLM, (c) on `tool_use` blocks dispatches to the owning child via the supervisor, (d) feeds `tool_result` back into the loop, (e) returns the final assistant message. Agents never touch MCP plumbing or do tool selection themselves. **Discipline — capability vs. infrastructure:** anything CoGrind invokes against external content (parse this file, extract text, search the web) is an MCP child whose tools the host's agents reach via `host.run_agent`; anything CoGrind uses for its own state (LanceDB queries, embedder calls, page writes, the LLM client) stays as a direct import. M3 ingest's first dependency on this dispatch path is `deco-assaying` (a separate project at https://github.com/ParkviewLab/deco-assaying) — CoGrind core ships zero parsers. See M2.5 for the full host design.

The CLI is **single-mode: an MCP client**, not a dual-mode tool. There is no in-process fallback. If you run `cogrind-workshop --ingest /path` and no `cobalt-grinding` is running, you get a clear error pointing you at `cobalt-grinding`. This is deliberate:

- One source of truth for every Smalt operation (the daemon).
- No code-path duplication to maintain.
- The single-writer mutex is genuinely single — there's no parallel process that could race it.
- The CLI evolves cleanly toward a richer agentic interface (REPL, conversational) without dragging business logic with it. Same shape as `claude` is to Claude Code: a polished human-facing MCP client, not a duplicate of the backend.

Why **one daemon, not six**: one fastembed copy in memory; one thing to keep alive; inter-system calls are function calls, not IPC. Why **threads, not subprocesses**: the heavy work — fastembed ONNX, lancedb Rust, pyarrow C++, network I/O — releases the GIL, so threads get real parallelism. Subprocess pools may arrive later for *specific* risky operations (PDF / code parsers prone to crashes), but not as the architecture's default.

**Seven agentic systems (verb-named for parallelism; each is an orchestrator + specialized sub-agents, not a single agent):**

| # | System | Purpose | Phase |
|---|---|---|---|
| 1 | **Ingest** | Source → pages: read a source at its location, produce summary + entities + concepts + claims with frontmatter linking to the source. Captures location + content hash + structure metadata; stores no source bytes. | Phase 1 |
| 2 | **Retrieve** | Query → ranked pages with snippets. Hybrid pipeline (metadata filter + parallel BM25 + vector + fuse + optional graph expansion). Emits gap signal when scores are weak. | Phase 1 |
| 3 | **Converse** | User question → cited answer. Uses Retrieve as a tool. Bounded by the corpus; says "I don't know" rather than hallucinate. | Phase 1 |
| 4 | **Cogitate** | Looks at the whole graph; proposes new labeled edges, names emergent concepts, sketches taxonomies, surfaces contradictions. Constructive. | Phase 2 |
| 5 | **Curate** | Looks at the whole graph; flags orphans, duplicates, staleness, drift, extraordinary unsupported claims, unvisited dust. Critical — proposes problems, never deletes. | Phase 2 |
| 6 | **Research** | Given a research request (from any of the other Phase 1 / Phase 2 systems — Retrieve, Converse, Ingest, Curate, Cogitate), finds and evaluates candidate sources, proposes adding them. Acquisitive. See *Cross-system flows* below for the request fan-in. | Phase 2 |
| 7 | **Toolsmith** | Given a tool gap signal (from any of the other six systems), finds existing MCP servers that fit, or specifies new ones to be developed. Proposes additions to agent toolkits and writes requirements docs for new MCP servers. **Same pattern as Research, applied to capabilities instead of knowledge.** Composes Research-like search/evaluate, Curate-like audit of agent rosters, and Cogitate-like proposing. *Phase 3* — needs Phase 1 + 2 in place to have agents to observe and a tool inventory to evaluate against. | Phase 3 |

**Substrate usage per system** (which MCP children each cognitive system talks to):

| System | smalt-mcp | ebony-enriching | Why |
|---|---|---|---|
| Ingest (M3)        | ✅ write pages/links/claims        | ❌                                      | Ingestion is direct knowledge capture — no hypothesis loop. |
| Retrieve (M4)      | ✅ read pages                      | ❌ (mostly)                             | Search/traverse over the corpus; emits gap signals → cobalt-grinding-side, then to ebony-enriching. |
| Converse (M5)      | ✅ read pages                      | ✅ write novel-synthesis proposals     | Answers from corpus; novelty detector flags candidates for Cogitate. |
| Cogitate (M7)      | ✅ read pages                      | ✅ write schema / edge / concept proposals | Walks the graph and proposes new structure. |
| Curate (M8)        | ✅ read pages                      | ✅ write orphan / duplicate / drift proposals | Audits the corpus and proposes fixes. |
| Research (M6)      | ✅ write (on apply only)           | ✅ read gaps + write source-adoption proposals | Gap → research → proposal; on user-approve, cobalt-grinding writes the source page to the Smalt and marks the proposal `applied`. |
| Toolsmith (Phase 3) | ❌                                | ✅ write tool-adoption / specification proposals | Capability-gap layer; doesn't read the corpus directly. |

**Cross-cutting principles:**
- Single-writer to the Smalt corpus: only **Ingest** writes Smalt pages (via `smalt-mcp`). Curate / Cogitate / Research (Phase 2) *propose* into the lab notebook (via `ebony-enriching`).
- "Propose, don't act" for any agent that audits or critiques. Proposals live in ebony-enriching, not in the Smalt.
- **Two substrates, zero outbound deps.** Neither MCP server talks to the other; cobalt-grinding orchestrates all cross-substrate flows (e.g., apply-validated-proposal = `smalt.write_page` + `ebony.update_proposal_status(applied)` in sequence).
- **No source copies.** Sources are referenced by location + content hash + fetch timestamp. The Smalt stores notes *about* sources, not copies of them.
- Original *structure* of every source is preserved as provenance (TOC, dir tree, heading hierarchy) — this is metadata about organization, not a copy of content.
- Confidence + provenance at the *value* level, not just the page level.
- Schema-flexible (frontmatter), schema-prescriptive (SCHEMA.md), schema-validated (Pydantic models inside `smalt-mcp` + indexer pass; ebony-enriching does the same for its own ProposalPage / ExperimentRecord / GapEntry shapes).
- The index is disposable: any choice between LanceDB / DuckDB / SQLite is reversible by rewriting the indexer.
- **Inline content not accepted in Phase 1.** If a source has no stable location (a pasted email, a chat log), save it to a file first and ingest the file.

---

## Cross-system flows

The seven systems aren't independent — several of them feed each other. There are **two fan-in patterns**, structurally identical (search → evaluate → propose; user approves; downstream system processes), operating on different substrates: *research requests* (knowledge gaps → sources to add) and *tool-gap signals* (capability gaps → MCP servers to add or specify).

**Research-request fan-in (knowledge):**

| Emitter | When it fires | Why (what kind of gap it sees) |
|---|---|---|
| **Retrieve** | Top scores for a query are weak | "find sources about <topic the Smalt doesn't cover>" — the corpus has no good answer |
| **Converse** | Notices, while answering, that context is missing | "find sources about <X>" — the user asked something the Smalt can't ground |
| **Ingest** | Encounters a citation in a newly-ingested source to a work the Smalt doesn't have | "find <cited work>" — follow the references |
| **Curate** | Flags stale sources, broken external links, or orphan pages | "freshen / replace / supplement" — what we have is rotting or alone |
| **Cogitate** | Flags concept areas that are under-supported, or contradictions that need more evidence | "fill / corroborate" — the synthesis we want to do needs more material |

All five fan into a single queue — **`ebony-enriching`'s `gaps.md`** (via the `ebony.add_gap(query, why?, source?)` MCP tool). Research reads gaps (via `ebony.list_gaps`), prioritizes, evaluates candidates, and writes ProposalPages back via `ebony.write_proposal(frontmatter={..., proposed_by: "research", proposal_kind: "source_adoption"})` — landing in ebony-enriching's `proposals/research/`. User reviews and accepts; on accept, cobalt-grinding orchestrates the cross-substrate publish (`smalt.write_page` to add the source page + `ebony.update_proposal_status(applied)` + `ebony.remove_gap` to clear the queue entry).

**Tool-gap fan-in (capabilities) — Phase 3:**

| Emitter | When it fires | Why (what kind of gap it sees) |
|---|---|---|
| **Ingest** | Hits a file type or source kind no available MCP tool can handle | "we need a parser/extractor for <format>" |
| **Retrieve** | Wants a structural query its current tools can't answer | "we need <kind of index/query backend>" |
| **Converse** | Needs a domain capability mid-answer that no tool provides | "we need <domain-specific tool, calculator, plotter, ...>" |
| **Curate** | Detects a class of audit it can't perform without a missing capability | "we need <linter/validator/checker>" |
| **Cogitate** | Wants to test a synthesis hypothesis but no tool gives it the data shape required | "we need <analysis/comparison tool>" |
| **Research** | Can't search a candidate-source surface (e.g. a paywalled DB, a niche forum) for lack of an adapter | "we need <search adapter for X>" |

All six fan into ebony-enriching as tool-gap entries (same `gaps.md` queue in v0; a dedicated `tool_gaps.md` could split later if volume warrants). **Toolsmith** reads via `ebony.list_gaps`, prioritizes, searches existing MCP registries / GitHub / npm / PyPI, evaluates candidates, and writes one of two proposal kinds back via `ebony.write_proposal(..., proposed_by: "toolsmith")` — landing in `proposals/toolsmith/`:

- *Adopt* — "use this existing MCP server `<X>`; here's its tool surface, license, maturity assessment." User approves → server added to `[mcp.clients.*]` config; cobalt-grinding autostarts it; `tools_index` picks it up; relevant agents' declared toolkits get the new tool added (proposed by Cogitate).
- *Specify* — "no fitting server exists; here's a requirements doc for `<X>` to be built." User approves → spawned as a new project (the deco-assaying pattern). Once that project ships, an *adopt* proposal follows automatically.

The "find existing or spec a new one" branch is the Toolsmith analog of Research's "ingest a found paper vs. write a literature-review of our own" — same engine, different output.

Like Research, Toolsmith **proposes only**. Adding an MCP server to the agent runtime is a security / supply-chain / behavior-shape change with a high cost-of-mistake — humans stay in the approval loop.

**Why this matters: M6 turns corpus growth into a flywheel.**

Pre-M6, every source has to be hand-pointed by the user via `cogrind-workshop --ingest`. Linear growth, paced by the user's attention.

Post-M6, two corpus-growth modes become available:

- **Reactive growth** — ask Converse a question; Retrieve emits a gap; Research proposes sources; user accepts; Ingest processes. The next ask gets a better answer (or surfaces the next gap). Each round of conversation can grow the corpus.
- **Proactive growth** — `cogrind-workshop --research "build me up on <topic>"` (or `wiki.research` from a chat client) seeds a batch of proposals before any question is asked. Useful when starting on a new area.

In both modes, Research **proposes only** — the user stays in the approval loop in M6's first impl. That's deliberate: the cost of a low-quality auto-ingested source is high (it pollutes synthesis and contaminates every downstream answer), so a human accept/reject step buys a lot of safety. An `--auto` mode can be added later, once Research's judgment is trustworthy on a per-domain basis.

Net effect: Phase 1 (M0–M5) requires you to manually point at every source. M6 onward, CoGrind helps you find what's missing, and the Smalt starts pulling itself forward.

**Looking further ahead:** a future system (M9+) will judge and rate the veracity and quality of sources. It will feed both Research (which can then prefer high-quality candidates when filling gaps) and Cogitate (which can weigh conflicting claims by source quality). Source-page frontmatter reserves space for these scores from M0 onward, even though they're unpopulated until that system lands. See the M9+ section.

---

## Configuration

CoGrind's runtime config is **TOML**, not markdown. Markdown is reserved for things LLM agents read (SCHEMA.md, POLICY.md, Smalt pages); TOML handles typed runtime plumbing (paths, model IDs, thresholds, transport settings).

**Layered config — last wins:**

```
1. Built-in defaults              (in code; no file required)
2. User-global config             ~/.config/src/cobalt_grinding/config.toml
3. Per-Smalt config                <wiki_root>/config.toml
4. Environment variables          COGRIND_WIKI_DIR, COGRIND_*
5. CLI flags                      --smalt <path>, --model <name>
```

Most users only edit (2). Per-Smalt config (3) exists for people running multiple Smalts with different settings (e.g., a personal-research Smalt with one model, a code-only Smalt with another). Env vars and CLI flags are for scripting and one-offs.

**Initial keys (M0):**

```toml
# ~/.config/src/cobalt_grinding/config.toml — user-global defaults

# Where the Smalt lives. Auto-created by `cobalt-grinding` on first run. Override per-invocation
# via --smalt <path> or COGRIND_SMALT_DIR.
smalt_dir = "~/Documents/Smalt"

[embedding]
provider     = "fastembed"                  # fastembed (local, default) | voyage | openai
model        = "BAAI/bge-small-en-v1.5"
dim          = 384                          # must match the model
# api_key_env = "VOYAGE_API_KEY"            # only needed when provider != "fastembed"

[llm]
provider     = "anthropic"
model        = "claude-opus-4-7"
api_key_env  = "ANTHROPIC_API_KEY"

[mcp]
transport    = "streamable-http"            # streamable-http | stdio
host         = "127.0.0.1"                  # only used by streamable-http
port         = 7474                         # only used by streamable-http

# Child MCP servers cobalt-grinding spawns and supervises (added in M2.5).
# Each section name is the default tool_prefix. The three ParkviewLab
# substrate / capability children below are the canonical set; user-added
# children plug in via the same shape.
#
# [mcp.clients.smalt-mcp]            # storage substrate; SMALT_DIR-backed
# command     = "smalt-mcp"
# autostart   = true
# restart     = "on-failure"
# env         = { SMALT_DIR = "~/Documents/Smalt" }
#
# [mcp.clients.ebony-enriching]      # lab notebook; EBONY_ENRICHING_DIR-backed
# command     = "ebony-enriching"
# autostart   = true
# restart     = "on-failure"
# env         = { EBONY_ENRICHING_DIR = "~/Documents/EbonyEnriching" }
#
# [mcp.clients.deco-assaying]        # code parsing (stateless)
# command     = "deco-assaying"
# autostart   = true
# restart     = "on-failure"
#
# [mcp.clients.example]              # the generic shape for user-added children
# command     = "uv"
# args        = ["run", "some-mcp-server"]
# # cwd        = "..."
# # env        = { ... }
# # tool_prefix = "example"          # defaults to section name
# # autostart   = true               # default
# # restart     = "on-failure"       # on-failure | always | never

[daemon]
max_workers  = 8                            # task-scheduler worker pool (1–64)

[logging]
level        = "info"                       # debug | info | warn | error
```

**Secrets policy:** API keys are never written to the config file. The `api_key_env` value names the environment variable that holds the key. This keeps secrets out of files, version control, and backups, and integrates cleanly with `direnv`, `1Password CLI`, `pass`, `aws secretsmanager`, etc.

**Keys added in later milestones** (placeholder, not in M0):

- `[ingest]` (M3) — supported file types, max-files-per-source, watched-folder inbox path (if added), `lang_h_default = "c"|"cpp"` (the `.h` disambiguation default when the heuristic is inconclusive), `glossary_extractor_model` (small/fast model override for the focused extractors).
- `[retrieve]` — `gap_score_threshold`, `cache_size`, `fts_weight` / `vector_weight` for RRF
- `[converse]` — `context_window`, `citation_strictness`
- `[research]` — `search_budget`, `enabled_source_backends` (M6)
- `[cogitate]` — proposal-frequency knobs (M7)
- `[curate]` — staleness thresholds, orphan-aging policy (M8)

**Default `smalt_dir` is `~/Documents/Smalt/`**, not `~/.src/cobalt_grinding/smalt/` — visible in Finder / Explorer and easy to open in VS Code or Obsidian without remembering it's hidden. The whole point of markdown-canonical is that humans want to read these files.

---

## Phase 1 scope (the hard cut)

**IN:**
- Storage substrate (markdown layout + LanceDB schema + indexer)
- **Ingest**: plain text, markdown, structured configs (json/toml/yaml/xml), HTML, PDFs, docx/rtf, source code + private GitHub repos
- **Retrieve**: hybrid pipeline (metadata filter → BM25 + vector in parallel → RRF fuse → optional graph expansion → rerank/answer)
- **Converse**: conversational front-end using Retrieve as a tool, with citations
- **MCP server** exposing the Smalt to any MCP-capable client (Claude Desktop, Claude Code, etc.) — built from Phase 1 milestone 0, growing tools as each subsystem comes online

**OUT (deferred to Phase 2):**
- **Cogitate**: emergent concept discovery, link proposing, contradiction surfacing, taxonomy building
- **Curate**: orphan / duplicate / staleness / drift / extraordinary-claim auditing
- **Research**: gap-driven source acquisition
- Image / multimodal ingestion
- DuckDB analytical layer
- Multi-user concurrent writes
- *CoGrind-aware* Claude Code skill/plugin (distinct from the MCP server, which is in Phase 1)
- Inline content ingestion (no stable location) — Phase 1 requires a saved file or URL
- Auto-archival of URLs (e.g., to archive.org snapshots) — only if Phase 2+ ever needs it

---

## Subsystems

### 1. Storage substrates (two: Smalt + Sciencing)

Cobalt-grinding ships **zero** storage layer. Both substrates are external MCP children, each in its own ParkviewLab repo with its own release cadence.

**Smalt substrate — `smalt-mcp`** (canonical knowledge):
- Pydantic models: `Page`, `EntityPage`, `ConceptPage`, `SourcePage`, `SynthesisPage`, **`IndexPage`** (auto-generated indices), `Claim`, `Link`, `Evidence`. Single source of schema truth, lives in `smalt_mcp/schema.py`.
- LanceDB tables: `pages`, `links`, `claims`, `embeddings`, `sources` — all under `SMALT_DIR/index/lance/`. Hybrid search (FTS + vector + alias, RRF-fused). Indexer regenerates `IndexPage` bodies from stored queries each run.
- MCP tool surface (17 tools at v0.5.0+): `status`, `list_pages`, `read_page`, `find_by_alias`, `incoming_links`, `traverse`, `search`, `list_domains`, `bootstrap`, `write_page`, `write_pages`, `add_link`, `add_claim`, `remove_page`, `update_claim`, `remove_claim`, `remove_link` — three permission tiers (READ_ONLY / READ_WRITE / REMOVE_DESTRUCTIVE).
- `SCHEMA.md` + `POLICY.md` placeholders live at `SMALT_DIR/schema/`; bootstrap drops them in.

**Sciencing substrate — `ebony-enriching`** (research-in-flight / lab notebook):
- Pydantic models: `ProposalPage` (the proposal-as-hypothesis shape; see *Proposal document shape and lifecycle* below), `ExperimentRecord`, `GapEntry`. Lives in `ebony_enriching/schema.py`.
- **No LanceDB**, no embeddings. Filesystem + markdown only; queries are filesystem walks. Proposals / experiments / gaps are few-in-count even on a mature Smalt.
- MCP tool surface (13 tools at v0.1.0): `status`, `read_proposal`, `list_proposals`, `read_experiment`, `list_experiments`, `list_gaps`, `bootstrap`, `write_proposal`, `update_proposal_status`, `supersede_proposal`, `write_experiment`, `add_gap`, `remove_gap` — two permission tiers (READ_ONLY / READ_WRITE; no REMOVE_DESTRUCTIVE in v0 — lab-notebook semantics are append-only with status transitions, not delete).
- Lives under `EBONY_ENRICHING_DIR/proposals/{schema,cogitate,curate,research,toolsmith,converse}/`, `EBONY_ENRICHING_DIR/experiments/<proposal-id>/<run-timestamp>.md`, `EBONY_ENRICHING_DIR/gaps.md`, `EBONY_ENRICHING_DIR/schema/{SCHEMA,POLICY}.md`.

Both substrates have **zero outbound deps**. Cobalt-grinding's cognitive systems orchestrate any cross-substrate flow (no `apply_proposal` tool on either server).

**Indexer (lives inside smalt-mcp; auto-runs on every write):** walks `SMALT_DIR/pages/`, hashes files, updates LanceDB tables, regenerates `IndexPage` bodies. Incremental on `content_hash`; rebuildable from scratch.

**Smalt-side `frontmatter_schema.py` details — notable per-type fields:**
  - `ConceptPage.domains: list[ConceptPageId] = []` — multi-domain by default. Empty list = undeclared.
  - `ConceptPage.is_domain: bool = False` — when true, this concept is itself a domain; the auto-generated `pages/domains.md` IndexPage lists all of these.
  - `SourcePage.domains: list[ConceptPageId] = []` — same field as on ConceptPage; gives the SME ingest agent a default-domain hint for terms it extracts from this source.
  - `SourcePage.parent_source: str | None` and `SourcePage.sections: list[str]` — link a section page to its parent source and let a source page enumerate its sections (the "hybrid layout" — see M3).
  - `EntityPage.domains: list[ConceptPageId] = []` — same field; used for disambiguation when same-name entities live in different domains (a CS researcher Smith vs. an economist Smith).
  - `ConceptPage.glossary: bool = False` — flags short-definition concept pages that participate in the Smalt-wide glossary; richer concepts (with parents/claims) leave it false.
  - `ConceptPage.evidence: list[Evidence]` — per-source `{source_id, snippet}` pairs so a glossary entry can show *where* a term was used.
  - `IndexPage` — auto-generated index pages. Initial set: `pages/glossary.md` (over `glossary: true` concepts) and `pages/domains.md` (over `is_domain: true` concepts); future entity-index, source-index follow. Frontmatter holds the stored query that defines the index and a regeneration timestamp; the indexer rewrites the body each run. Marked `auto_generated: true` so humans/agents know not to hand-edit them.
- **Domain hierarchy uses labeled links, not the `domains:` field.** "CS is a subdomain of computing" is a `subdomain_of` link from the CS ConceptPage to the computing ConceptPage. Keeps "what this is *about*" (`domains:`) cleanly separated from "what this is *under*" (`subdomain_of` link). Top-level domains have no `subdomain_of` link.

**Smalt-side `SCHEMA.md` / `POLICY.md` (live at `SMALT_DIR/schema/`) are living documents.** Day-0 are human-seeded with the page types, frontmatter keys, link-edge vocabulary, and behavioral policy. Day-1+: groomed via the proposal-as-hypothesis loop, which now lives in **ebony-enriching**: Cogitate proposes additions (M7), Curate flags drift / removal candidates (M8); user-approved changes get applied to SMALT_DIR/schema/ by cobalt-grinding's orchestration (a `smalt.write_page`-equivalent edit + `ebony.update_proposal_status(applied)` in sequence). The falsifiability + cost-tier discipline (proposal-as-hypothesis rules) lives in **ebony-enriching's own POLICY.md**, not the Smalt's — keeps lab-notebook discipline scoped to where the experiments live.

**Sciencing-side `SCHEMA.md` / `POLICY.md` (live at `EBONY_ENRICHING_DIR/schema/`)** document the lab notebook itself — what a `ProposalPage` looks like, what statuses are valid, falsifiability requirements, cost-tier rules. Equally living, on the same propose-test-approve loop applied to themselves.

### 2. Ingest subsystem

**Orchestrator** + sub-agents. Triggered by `cogrind-workshop --ingest <path-or-url-or-repo>` (CLI flag) or the `wiki.ingest` MCP tool (any client).

**Sub-agents — pipelined, not a single combined LLM call.** Each is a focused step with its own prompt and structured output. Cleaner prompts, easier independent evaluation, easier to swap or skip a single extractor. Trade-off: ~3-4× LLM calls per file vs. a combined prompt; mitigated by using a small/fast model (Haiku) for the focused extractors and a bigger model (Sonnet/Opus) only for `summarizer`, plus content-hash-keyed file-level caching so re-ingests are cheap.

```
source_fetcher (file/dir resolution + git/vault detection + metadata)
  ↓
format_classifier (per file → handler; .h disambiguation once per source)
  ↓ per-file (parallel on the worker pool)
[handler-specific extraction]    ← code handler reaches deco-assaying.parse_file via the host (MCP child)
  ↓
summarizer (per-file LLM summary, ~200 words)
  ↓
entity_extractor (people / orgs / products / repos / packages only)
  ↓
glossary_extractor (candidate terms + short definitions)
  ↓
link_resolver (dedup against existing entity / concept pages)
  ↓ all files joined
frontmatter_writer (deterministic, schema-validated)
  ↓
page_writer (atomic write through corpus mutex)
  ↓
indexer_caller (incremental indexer pass, regenerates IndexPages)
```

Each box is a sub-agent that takes `(input, app: App)` and returns a typed result. Sub-agents that need an LLM call `await app.host.run_agent(system=..., messages=[...])` — the host runs the tool-use loop, retrieves relevant MCP tools from `tools_index`, dispatches them, and returns the final assistant message. Sub-agents do *not* select tools or call MCP children directly; the host handles that.

**Notable sub-agent details:**
- `source_fetcher` — resolves the location (URL fetch, file read, `gh` clone), computes `source_content_hash`, records `fetched_at`. Holds source bytes in memory or a temp dir during ingestion only — *not* persisted to the Smalt.
- `structure_extractor` — captures original organization (dir tree, TOC, heading hierarchy) inline in source-page frontmatter, or as a sidecar at `smalt/structures/<source-id>.json` if too large.
- `entity_extractor` — narrowly scoped at M3 to people, organizations, products, repositories, packages. Code symbols (functions/classes/macros) live in section-page bodies, not as standalone entity pages. Promotion to entity-page on demand can come later via Cogitate.
- `glossary_extractor` — picks candidate terminology a source introduces; emits `[{term, short_definition, evidence_snippet}]`. Output flows through `link_resolver` for dedup; new terms become `ConceptPage(glossary=true)`; existing terms get the new source appended to their `evidence` list.
- `link_resolver` — entity / concept resolution against existing pages (exact + fuzzy + LLM-tiebreaker); proposes merges.
- `frontmatter_writer` / `page_writer` / `indexer_caller` — deterministic, schema-validated, atomic-write through the corpus mutex, then trigger an incremental indexer pass that also regenerates `IndexPage` bodies (the glossary index first).

**Format handlers (phased — see phases below):**
- text/markdown → trivial passthrough + structure
- json/toml/yaml/xml → parse-then-summarize structure (esp. lockfiles, manifests)
- html → strip + preserve heading hierarchy
- pdf → `pymupdf` for text + layout; OCR fallback (`tesseract`) for scanned PDFs flagged with `confidence: low`
- docx/rtf → `pandoc`
- code → [`deco-assaying`](https://github.com/ParkviewLab/deco-assaying), a separate project (tree-sitter–based MCP server, Python/C/C++ at v0). `cobalt-grinding` autostarts it via `[mcp.clients.deco-assaying]` once the user installs it. The code-handler agent calls `await app.host.run_agent(...)`; the host's `tools_index` retrieval surfaces `deco-assaying.parse_file` to the LLM, which calls it via the host's MCP plumbing. Same dispatch shape as future PDF / OCR / archive parsers — CoGrind core ships zero parsers; this is the first proof of the capability-vs-infrastructure line. Per-module pages; per-symbol pages only when referenced enough.
- github repo → `gh repo clone` → treat as code source; commits/PRs/issues as additional sources
- `.env` files → keys-only, never values, redaction-before-write

**Critical rules:**
- Ingestion is transactional — accumulate proposed page edits in memory, validate against schema, then atomically write the touched markdown files. If any step fails, no pages are written.
- **Source bytes are never persisted** to the Smalt. They're fetched, read, processed, and released. The Smalt retains the location pointer + content hash + structure metadata + the notes it produced — nothing more.
- **Glossary growth is monotonic in Phase 1.** New terms are added; existing terms accumulate evidence; nothing is pruned. Pruning (and renaming, merging) lands as Curate proposals in Phase 2.

#### Code parsing — `deco-assaying` (separate project)

Code handling depends on [`deco-assaying`](https://github.com/ParkviewLab/deco-assaying) — a separate project, not part of this repo. It's a tree-sitter–based MCP server (Python/C/C++ at v0; broader language coverage planned) that exposes `parse_file` and `supported_languages` as MCP tools. CoGrind core ships zero parsers; the user installs `deco-assaying` separately and configures it under `[mcp.clients.deco-assaying]`. `cobalt-grinding` autostarts it like any other configured MCP child; the host indexes its tools into `tools_index` so an ingest agent's `host.run_agent(...)` request retrieves `deco-assaying.parse_file` for the LLM via the normal hybrid-retrieval path.

**Why a separate project:** parsers are capabilities, not infrastructure (see M2.5's capability-vs-infrastructure line). Living in its own repo means deco-assaying can grow language coverage on its own release cadence, accept contributors without forcing changes through CoGrind, and serve as the proof point that any future contributor's parser/extractor (PDF, OCR, archives, fetchers) plugs in identically. We looked at [`mcp-code-parser`](https://github.com/boxabirds/mcp-code-parser) as precedent and chose to build our own for broader language coverage and an output shape (structured `ParseResult` with `Symbol` / `Import` lists) tuned for CoGrind's ingest pipeline.

**Contract M3 ingest depends on:** `deco-assaying.parse_file(path, language)` returns a result containing `symbols: [{name, kind, span, docstring, parent}]`, `imports: [{module, alias, span}]`, and `errors: [...]` for partial trees. Section pages render the `Symbols` and `Imports` lists deterministically; partial-tree files get `parse_status: partial` on the section page. Detailed schema, language coverage, and docstring rules live in deco-assaying's own README; CoGrind only depends on the surface contract.

**`.h` disambiguation lives in CoGrind's `format_classifier`, not in deco-assaying.** CoGrind passes an explicit `language` arg to `parse_file`. Heuristic, in order:
1. Sibling files in the source root contain `.cpp` / `.cc` / `.cxx` / `.hpp` → C++.
2. File content contains `namespace`, `class`, `template`, or `extern "C"` → C++.
3. Otherwise → C.

Heuristic runs **once per directory ingest** (not per file) and sets `h_lang ∈ {"c","cpp"}` for the run; overridable via `--lang-h=c|cpp`.

### 3. Retrieve subsystem

**Pipeline** (assembled in code, mostly LanceDB API calls):

```
query
  ↓
metadata filter (frontmatter, free)
  ↓
parallel:
  ├─ entity-name lookup (exact + fuzzy on titles/aliases)
  ├─ BM25 (FTS on body / identifiers for code)
  └─ vector similarity (HNSW on summary embeddings)
  ↓
RRF fuse
  ↓
optional 1-hop graph expansion
  ↓
rerank (or fold into answering LLM)
  ↓
emit "gap" if top scores weak (queued to ebony-enriching via ebony.add_gap for Phase 2 Research)
```

**Sub-agents:**
- `query_classifier` — entity? concept? code? cross-source synthesis?
- `searcher` — calls the LanceDB hybrid API
- `graph_expander` — walks 1-hop neighborhoods
- `gap_detector` — emits gap signal when scores are weak

**Cache:** `(query_hash, candidate_set) → ranked_order` keyed in LanceDB.

### 4. Converse subsystem

Conversational front-end. The user's primary interface in Phase 1.

**Sub-agents:**
- `intent_classifier` — question? task? exploration?
- `retriever` — calls Retrieve as a tool
- `answerer` — main user-facing LLM, cites pages by id
- `citation_checker` — validates that cited pages actually support the claim (catches hallucinations)
- `novelty_detector` — flags when an answer was a *new synthesis*; queues a `proposed_synthesis.md` for Cogitate (Phase 2) to consider — no auto-write in Phase 1

**Framing:** Converse is bounded by what's been ingested. The prompt makes that explicit. No claims about uningested topics.

---

## Proposal document shape and lifecycle

> **Substrate note.** Proposals live in **ebony-enriching's storage** (`EBONY_ENRICHING_DIR/proposals/<system>/<id>.md`), **not** the Smalt. They're read and written via `ebony.*` MCP tools (`ebony.write_proposal`, `ebony.list_proposals`, `ebony.read_proposal`, `ebony.update_proposal_status`, `ebony.supersede_proposal`), never via `smalt.*` tools. The Smalt contains only the canonical knowledge corpus.

Every system that doesn't directly write the corpus emits **proposals** — Curate, Cogitate, Research, Converse's novelty detector, Toolsmith (Phase 3). The discipline is the **scientific method**: a proposal is a hypothesis with a falsifiable prediction, the system tests where cheap, the user reviews hypothesis + evidence together. Truth is provisional.

This subsection specifies the shared `ProposalPage` shape that all systems' proposals conform to, the lifecycle states, and the cost-tier rules that govern when the system tests automatically vs. defers to the user. See `northstar.md` → *How the Smalt evolves: hypothesis, test, truth* for the why.

**Frontmatter shape** (`ProposalPage`, in `ebony_enriching/schema.py`):

```yaml
type: proposal
proposal_kind: schema_addition | schema_drift | schema_removal | wiki_edge | concept_merge
              | source_adoption | tool_adoption | tool_specification
              | toolkit_addition | toolkit_removal | novel_synthesis | ...
status: proposed | under_test | validated | rejected | applied | superseded
proposed_by: <system_name>      # Cogitate, Curate, Research, Converse, Toolsmith, ...
proposed_at: <ISO timestamp>
test_status: untested | passed | failed | untestable
test_cost: trivial | cheap | medium | expensive
related_pages: [<page_ids>]     # what this proposal references
supersedes: <proposal_id> | null
superseded_by: <proposal_id> | null
```

**Body sections (ordered):**

1. **Observation** — what evidence triggered this proposal. Concrete pointers: page IDs, trace excerpts, drift counts, gap signals. *No hypothesis yet — just what was seen.*
2. **Hypothesis** — the proposed change in specific terms (the new schema field, the new edge, the toolkit addition, the source to ingest, etc.).
3. **Prediction** — what should *measurably* change if the hypothesis is applied. Numeric where possible. Pre-registered (no goalpost-moving after the test).
4. **Test** — design + result. If cheap and run: "Ran X; observed Y; prediction held / didn't hold." If untested: "Test design: …; Cost: medium/expensive; deferred." If untestable: "Untestable because Z; user is the test."
5. **Reasoning** — free-text justification connecting observation to hypothesis.

**Cost tiers** govern whether the system runs the test automatically before flagging the proposal for user review:

| Tier | Examples | System auto-tests? |
|---|---|---|
| **trivial** | typo fix, rename for clarity, alphabetize a list | No test required; user-approve and apply |
| **cheap** | schema dry-run against existing pages, vocabulary read-time check, query benchmark before/after on cached corpus | Yes, automatically |
| **medium** | policy replay over historical agent traces, corpus re-link with proposed edge | If compute budget allows; otherwise `test_pending` |
| **expensive** | re-run an agent against held-out inputs, sandbox a candidate MCP server, full re-ingest with new toolkit | Run only on user request; otherwise the user is the test |

**Lifecycle transitions:**

```
proposed ──(cheap test passes)──→ validated ──(user approves)──→ applied
   │                                  │
   │                                  └──(user rejects)──→ rejected
   │
   ├──(test fails)──→ rejected
   │
   ├──(test untestable)──→ user-reviewed (no validated state) ──→ applied | rejected
   │
   └──(superseded by later proposal)──→ superseded

applied ──(later evidence contradicts)──→ proposed (re-test under new evidence)
```

The `applied → proposed` edge is what keeps **truth provisional**. Schema *removal* travels this path: a field is `applied` based on early evidence, accumulated evidence later contradicts the prediction (Curate detects), a removal-proposal is filed (`schema_removal`), tested, and applied if validated.

**Falsifiability discipline.** Proposals whose predictions cannot be reformulated into something measurable are marked `test_status: untestable` with a reason; the user becomes the test. *Untestable* is not a free pass — Curate periodically audits the rate of `untestable` proposals; a high rate signals the discipline is slipping. The user's own direct edits to SCHEMA.md / POLICY.md are held to the same bar (the user, like the system, declares a prediction or accepts that the change is being made on judgment alone).

**The experimental record.** Test runs leave artifacts at `EBONY_ENRICHING_DIR/experiments/<proposal-id>/<run-timestamp>.md` — what was tested, the input, the result, links back to the triggering proposal. Written via `ebony.write_experiment`. The lab notebook carries its own scientific record so re-evaluation under new evidence is grounded in observation, not opinion.

**Per-system test mechanics** (sketch — fleshed out in each system's milestone; all proposals stored in ebony-enriching):

| System | Typical proposal-kinds | Typical test mechanics | Stored in (subdir under `EBONY_ENRICHING_DIR/proposals/`) |
|---|---|---|---|
| Cogitate (M7) | `schema_addition`, `wiki_edge`, `concept_merge`, `novel_concept` | Schema dry-run; query benchmark before/after; corpus re-link | `schema/` (for schema-kinds) or `cogitate/` (everything else) |
| Curate (M8) | `schema_drift`, `schema_removal`, `orphan`, `duplicate`, `staleness` | Drift counts, link reachability, age thresholds | `curate/` |
| Research (M6) | `source_adoption` | Dry retrieval against the candidate's would-be summary; coverage check against the gap signal | `research/` |
| Toolsmith (M9+) | `tool_adoption`, `tool_specification`, `toolkit_addition`, `toolkit_removal` | Sandbox replay of agent traces; tool-use error-rate before/after | `toolsmith/` |
| Converse novelty | `novel_synthesis` | Citation re-check on the proposed synthesis page | `converse/` |

---

## Apply-time post-mortem: closing the learning loop

When a validated proposal transitions to `applied`, cobalt-grinding's orchestration doesn't just write the page and update the status — it also runs a **post-mortem on the proposal's full journey from initiation to validation**, captures what worked and what didn't, and where the lessons generalize, adds them to the Smalt as knowledge. This is the concrete mechanism behind northstar.md's *CoGrind learns how to learn better.*

**Post-mortem steps** (cobalt-grinding agent-side, run after the substantive write but before the proposal's status transition finalizes):

1. **Read the proposal's full lifecycle:** original frontmatter (`proposed_at`, `proposed_by`, observation, hypothesis, prediction body), every experiment record (`ebony.list_experiments(proposal_id)` + `ebony.read_experiment(...)`), any superseded predecessors (walk the `supersedes` chain via `ebony.read_proposal`), and any user-review notes captured during validation.
2. **Read related rejected proposals** — siblings from the same observation, near-duplicates that took a different angle (`ebony.list_proposals(system=<proposed_by>, status=rejected)` filtered by approximate timestamp window). Sees the full decision space, not just the winning path.
3. **Run an LLM-driven synthesis** (a sub-agent — `post_mortem` — defined the same way as Cogitate's `observer` / `hypothesis_generator`). Output: a 1–3 paragraph **lessons-learned summary** plus a structured signal block (`{system, proposal_kind, pattern, false_starts_count, time_to_validate, ...}`).
4. **Judgment call — is this knowledge worth keeping?** The agent decides whether the lessons generalize:
   - **Yes** — the pattern is novel; the path was longer than typical; the proposal succeeded after notable false starts; the system's prior failure modes are illuminated; a class of proposals just got cheaper.
   - **No** — the path was routine; the lesson is "small concept addition went smoothly"; nothing generalizes.
5. **If yes, the agent picks the write target:**
   - **Extend an existing methodology ConceptPage** — e.g., `pages/concepts/cogitate-methodology.md` accumulates lessons-learned across runs (add a claim with `source_ref` pointing at the proposal id); use `smalt.add_claim`. This is the common case.
   - **Extend an existing pattern ConceptPage** — e.g., `pages/concepts/schema-addition-patterns.md` for kind-specific patterns; same `smalt.add_claim` shape.
   - **Propose a novel SynthesisPage** — in the rare case the lesson is broad enough to deserve its own cross-cutting page, use `ebony.write_proposal(..., proposed_by: cogitate, proposal_kind: novel_synthesis)`. The system doesn't get to write Smalt syntheses directly; the proposal-as-hypothesis loop still applies.
6. **Always: write the post-mortem itself to ebony-enriching as an experiment record** — `ebony.write_experiment(proposal_id, input={"kind": "post_mortem"}, result={lessons_summary, signal_block, smalt_writes: [...]})`. The system's record of its own learning trajectory is then queryable later via `ebony.list_experiments`. Even when step 5 returns "no, skip the Smalt write," the experiment record exists so Curate can later detect "we're not actually learning anything from these proposals — discipline is slipping."

**Why this matters: it makes the proposal track record alive instead of inert.** Pre-post-mortem: each apply transitions a status field and moves on; the accumulated history of proposals is just data. Post-post-mortem: each apply is a chance for the system to update its own judgment about how to propose better, and the methodology ConceptPages become part of the next Cogitate / Curate / Research run's context. **The system learns from itself.**

**Cost discipline.** Post-mortems are not free — they're another LLM call (typically Sonnet/Opus for the synthesis step, plus Haiku-tier reads of the lifecycle). Heuristics:
- **`trivial`-tier proposals skip post-mortem entirely** (typo fixes, alphabetizations — nothing to learn from).
- **`cheap`-tier proposals get a Haiku-only post-mortem** (read the lifecycle, write a one-paragraph summary into the experiment record; skip step 5's "should I update the Smalt?" branch unless the signal-block flags novelty).
- **`medium` / `expensive`-tier proposals get the full pipeline** (Haiku reads + Sonnet/Opus synthesis + judgment call + optional Smalt write).
- **Override:** a `[post_mortem]` config block can raise/lower the cost-tier threshold or disable post-mortem entirely (for very-low-budget deployments — a degraded mode, not the default).

**Cross-system insight aggregation.** Once methodology ConceptPages exist, Curate's later milestones can audit *them* — flag pages whose lessons contradict (the system is learning conflicting things), or pages that haven't been read by their owning system's prompts (the lessons are accumulating but not influencing behavior). That closes the loop: the system that audits the corpus also audits the corpus's record of its own learning.

---

## Implementation milestones

Phase 1 covers M0–M5 (Bootstrap → Indexer → Daemon shape → Ingest → Retrieve → Converse). Phase 2 begins at M6: Research, then Cogitate (M7), then Curate (M8). Each milestone ends in a demoable state. Milestones are roughly sequential; some can overlap.

### M0 — Bootstrap (foundations, no agents yet)

> **Substrate note.** As of M2.7 (smalt-mcp v0.5.0+) and the workstream-B ebony-enriching launch, cobalt-grinding ships **zero storage layer**. The "Pydantic models / SCHEMA.md / LanceDB schema" work in M0 now lives in the **smalt-mcp** repo (the canonical-knowledge substrate) and the **ebony-enriching** repo (the lab notebook). M0 here is downgraded to "bootstrap-as-MCP-host" — cobalt-grinding wires up its MCP children, runs their `bootstrap` tools, and brings up its own thin scaffolding.

- Repo skeleton: `src/cobalt_grinding/` Python package (cognitive systems + MCP host; no storage layer; no schema models).
- **MCP children configured under `[mcp.clients.*]`:** `smalt-mcp` (SMALT_DIR-backed), `ebony-enriching` (EBONY_ENRICHING_DIR-backed), `deco-assaying` (stateless). cobalt-grinding autostarts all three on first run.
- **Substrate bootstrap on startup:** cobalt-grinding calls `smalt.bootstrap()` to materialize SMALT_DIR's canonical layout (`pages/`, `schema/SCHEMA.md`, `schema/POLICY.md`, `index/lance/` tables) and `ebony.bootstrap()` to materialize EBONY_ENRICHING_DIR's canonical layout (`proposals/{schema,cogitate,curate,research,toolsmith,converse}/`, `experiments/`, `gaps.md`, `schema/SCHEMA.md`, `schema/POLICY.md`). Both calls are idempotent.
- Schema work (page-type models, `ProposalPage` with lifecycle states, falsifiability + cost-tier rules) is owned by the substrate repos, not by cobalt-grinding. SCHEMA.md/POLICY.md placeholders ship with each substrate's bootstrap; humans + Claude seed them on Day 0; thereafter they're groomed via the proposal-as-hypothesis loop.
- Basic CLI shell: `cogrind-workshop --status` (queries `smalt.status` + `ebony.status` via the daemon).
- **MCP server scaffolding** (the daemon binary itself) — empty `wiki.*` tool registry; tools are added in later phases as each cognitive system comes online.
- Test fixtures: a tiny seed Smalt + a tiny seed EbonyEnriching for cross-substrate scenario tests.

**Done when:** `cobalt-grinding` starts cleanly with all three MCP children autostarted; `smalt.bootstrap()` materializes the canonical SMALT_DIR layout; `ebony.bootstrap()` materializes the canonical EBONY_ENRICHING_DIR layout; `cogrind-workshop --status` reports both substrates green; the daemon binary itself accepts an MCP client connection (no `wiki.*` tools yet — added per cognitive system).

### M1 — Indexer (single-shot, in-process)

The indexer turns markdown pages into queryable LanceDB rows. M1 ships it as a single-shot CLI command — the daemon shape comes in M2 and lifts this work into a long-running process. The indexer's code is written with strict discipline (no module-level globals, all state passed as arguments, lazy resource construction) so M2 can wrap it as a daemon task without redesign.

- Walk `smalt/pages/`, parse frontmatter, validate against schema
- Compute content hash, populate LanceDB `pages`, `links`, `claims` tables
- Generate embeddings using **fastembed** with `BAAI/bge-small-en-v1.5` (384-dim) by default — local, ONNX-quantized, no API keys, no per-token cost, runs offline. Provider is configurable; hosted alternatives (Voyage, OpenAI) are supported via the `[embedding]` config block but not the default. (Cold-loaded each invocation in M1 — fixed in M2.)
- FTS index on body + title
- HNSW index on embeddings
- Incremental: only re-process files whose `content_hash` has changed
- `cogrind-workshop --index [--full]` CLI command (synchronous, blocks until done)

**Done when:** hand-write 3 markdown pages → `cogrind-workshop --index` → LanceDB has the rows + embeddings + FTS + HNSW; modify one page → `cogrind-workshop --index` → only that page reprocesses; querying LanceDB directly returns the indexed pages. The indexer code follows the no-globals discipline so M2 can lift it without refactor.

### M2 — Daemon + CLI split

The big architectural slice for Phase 1: split CoGrind into **two binaries** with a single MCP protocol between them, and lift M1's indexer behind the daemon's tool handler.

**Two binaries, one protocol — no shared business logic:**

- `cobalt-grinding` — the daemon. New entry point. Long-running. Hosts the MCP server, the task scheduler, the worker pool, and all Smalt business logic.
- `cogrind-workshop` — the CLI (sibling repo, extracted at M2.7). Pure MCP client. No business logic; no `App`; no in-process work. Calls the daemon's `wiki.*` tools and renders results. Lives in [`ParkviewLab/cogrind-workshop`](https://github.com/ParkviewLab/cogrind-workshop); this repo no longer ships a CLI binary.

**Daemon (`cobalt-grinding`):**

- `src/cobalt_grinding/daemon/main.py` — entry point: `cobalt-grinding`. Loads config, runs the bootstrap step, starts the MCP server (HTTP transport for daemon clients; stdio variant for child-process clients like Claude Desktop), starts the task scheduler.
- `src/cobalt_grinding/app.py` — `App` class holding shared resources: `Config`, fastembed model, LanceDB connection, LLM client (constructed lazily). All subsystems take an `App` as a dependency; no module-level globals.
- `src/cobalt_grinding/daemon/scheduler.py` — task scheduler + worker pool. Asyncio event loop + `concurrent.futures.ThreadPoolExecutor`. A `Task` model (`task_id`, `kind`, `status` ∈ {queued, running, succeeded, failed, cancelled}, `progress`, `result`, `error`, `submitted_at`, `started_at`, `finished_at`).
- `src/cobalt_grinding/daemon/bootstrap.py` — first-run Smalt initialization. On startup, if the configured `smalt_dir` is empty/missing, the daemon creates the canonical layout, drops in fresh `SCHEMA.md` / `POLICY.md` templates, and creates the empty LanceDB tables. Replaces the old `cogrind init` CLI verb (the old daemon-startup pattern, now obsolete).
- `src/cobalt_grinding/daemon/mutex.py` — single-writer corpus mutex (used by M3+ ingest workers).
- Refactor M1's indexer to take an `App` and run as a daemon task via the `wiki.index` MCP tool. The Indexer class itself doesn't change — it's already no-globals, args-in-constructor.
- MCP tools registered in M2: `wiki.index`, `wiki.status`, `wiki.task_status`, `wiki.task_list`, `wiki.task_cancel`. Subsequent milestones add `wiki.ingest` (M3), `wiki.search` (M4), `wiki.ask` (M5), and the Phase 2 tools.

**CLI (`cogrind-workshop`):**

- (cogrind-workshop sibling repo) `src/cogrind_workshop/main.py` — entry point: `cogrind-workshop`. Click for flags and args.
- `src/cobalt_grinding/cli/client.py` — thin MCP-over-HTTP client. Discovers a running `cobalt-grinding`, calls tools, optionally tails task progress.
- `src/cobalt_grinding/cli/formatters.py` — render task results, status tables, gap reports, etc. for a terminal.
- **Initial flag-style interface** (M2-era): `cogrind-workshop --status`, `cogrind-workshop --index`, `cogrind-workshop --ingest /path` (M3 onwards). One verb per invocation. Long-running operations show live task progress.
- **Future**: an interactive REPL (`cogrind-workshop` with no flags) — same shape as `claude`. Out of M2 scope.
- **No daemon → clear error**: every Smalt-operation flag fails with a friendly message ("no `cobalt-grinding` running — start one with `cobalt-grinding &`").

**`pyproject.toml` entry points** (post-M2.7 cleave: one binary per repo, not two from this one):

```toml
# cobalt-grinding/pyproject.toml — the daemon binary only
[project.scripts]
cobalt-grinding = "cobalt_grinding.daemon.main:run"

# cogrind-workshop/pyproject.toml — the CLI binary (sibling repo)
[project.scripts]
cogrind-workshop = "cogrind_workshop.main:main"
```

**What gets removed in M2:**

- `cogrind init` CLI verb — replaced by daemon-startup auto-init (M2.7+ this lives entirely in smalt-mcp's `bootstrap` tool).
- `cogrind status` CLI verb — replaced by the `wiki.status` MCP tool, called by `cogrind-workshop --status`.
- `cogrind mcp serve` CLI verb — the daemon binary `cobalt-grinding` is the MCP server itself; no "serve" subcommand needed.
- The original `cogrind/cli.py` + `cogrind/commands/` flat structure — replaced by a daemon-only `src/cobalt_grinding/daemon/` subpackage, with the CLI extracted entirely to the cogrind-workshop sibling repo at M2.7.

**What stays (and is just relocated or reused):**

- `src/cobalt_grinding/ingest/`, `src/cobalt_grinding/retrieve/`, `src/cobalt_grinding/converse/` — cognitive-system business logic. The daemon's `wiki.*` tool handlers call into these; the CLI never does.
- `src/cobalt_grinding/config.py` — same loader; both binaries read the same config layers.
- **Storage layer is OUT** (post-cleave: smalt-mcp v0.5.0+). The Smalt schema, indexer, and LanceDB plumbing now live in **smalt-mcp**; the lab-notebook schema lives in **ebony-enriching**. cobalt-grinding's `wiki.index` tool handler is a thin shim that calls `smalt.bootstrap` (which auto-runs the indexer on every write). Cobalt-grinding ships no `src/cobalt_grinding/storage/` or `src/cobalt_grinding/schema/` packages anymore.

**Done when:**

1. `cobalt-grinding` starts cleanly; pointed at an empty / missing Smalt dir, it auto-creates the canonical layout (the M0 layout, plus M1's tables) without needing any prior CLI invocation.
2. `cogrind-workshop --status` from a separate shell connects over HTTP MCP and returns the daemon's view of the Smalt (tables, page counts, scheduler state).
3. `cogrind-workshop --index` over MCP runs M1's indexer as a daemon task; second invocation is visibly faster than the first (warm fastembed + LanceDB connection).
4. `wiki.task_status(task_id)` returns live state for an in-flight indexer run; `wiki.task_cancel(task_id)` interrupts a running task cleanly.
5. `cogrind-workshop --status` (or any Smalt-operation flag) with no daemon running fails with a clear error pointing at `cobalt-grinding`.
6. Multiple concurrent MCP requests are handled correctly — e.g., a long-running `wiki.index` task and several quick `wiki.task_status` calls interleave without serializing.
7. Single-writer mutex test: two concurrent ingest-equivalent operations serialize on the corpus-write step.
8. Claude Desktop can connect (via either stdio child or HTTP) and successfully call `wiki.status` / `wiki.index` as it would any other MCP server.

### M2.5 — Daemon as MCP host + agent runtime

M2 made the daemon an MCP **server** (it answers requests). M2.5 makes it an MCP **host** (it also makes them) *and* gives it an internal agent runtime that runs the LLM tool-use loop on behalf of CoGrind's own subsystems. Lands before M3 so M3's ingest sub-agents are written against `app.host.run_agent(...)` natively.

**The line this milestone draws — capability vs. infrastructure:**

- **Capabilities** — anything CoGrind invokes against *external content* (parse this file, extract text from this PDF, search the web, fetch this URL, OCR this image). These run as MCP child servers; their tools are discovered by the host at startup, indexed for retrieval, and reach the LLM via the agent runtime. Adding a new file type or a new search backend later is a new MCP child server in someone's config, not a code change in CoGrind core.
- **Infrastructure** — anything CoGrind uses internally to maintain *its own state* (LanceDB queries, embedder calls, page writes, the LLM client). These stay direct imports. Forcing them through MCP would pay serialization cost on hot paths for no extensibility benefit, since they're not extension points.

This line is what makes ingest growth tractable. Five of CoGrind's six subsystems are agentic; ingest in particular will keep meeting new file types (`.docx`, `.epub`, `.org`, `.rst`, `.ipynb`, image OCR, table extraction, archives, audio transcription, …). Each one is a parser + extractor capability. Shipping all of them inside CoGrind core means every new format needs a CoGrind release. Shipping each as an MCP child server means a new format is a new package, configured into `[mcp.clients.<name>]`, picked up automatically by the host. Different language ecosystems (a Rust PDF library, a Go archive walker) can contribute parsers without growing CoGrind's polyglot dependency tree.

**Two intertwined capabilities `cobalt-grinding` gains:**

1. **MCP host (client side)** — spawns and supervises configured child MCP servers; collects their tools at handshake; dispatches tool calls to the right child.
2. **Agent runtime** — exposes `await app.host.run_agent(system, messages, ...)` to CoGrind's own subsystems. Implementation runs the full LLM tool-use loop (LLM → `tool_use` → MCP child → `tool_result` → LLM → … → `end_turn`). Agents never see MCP plumbing or do tool selection themselves.

CoGrind core ships zero MCP children. The first child CoGrind's M3 ingest will configure is [`deco-assaying`](https://github.com/ParkviewLab/deco-assaying) (separate project), but it's not built or tested as part of M2.5 — M2.5's integration test uses an in-tree stub MCP server.

**Agent API (what CoGrind's own subsystems call):**

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

**Tool selection — hybrid retrieval over a `tools_index`, reusing M1 infrastructure:**

1. **Tools-index, populated at daemon startup.** When `McpClientManager` finishes handshakes with each `[mcp.clients.*]` child (MCP `tools/list`), the host writes every discovered tool's `{prefixed_name, description, input_schema, owning_child}` into a LanceDB table `tools_index`. Description is embedded via the existing fastembed embedder; FTS index built on description. Same plumbing pattern as `smalt-mcp`'s LanceDB tables — different table, same patterns. (Note: this `tools_index` LanceDB lives inside `cobalt-grinding`'s own state dir, not in SMALT_DIR or EBONY_ENRICHING_DIR — it's about cobalt-grinding's own routing, not corpus content.) Refreshed on supervisor events (child crashed → entries removed; child restored → re-indexed).

   Note: cobalt-grinding's own `wiki.*` MCP server-side tools are *not* in `tools_index`. Those are served to external clients (CLI, Claude Desktop). The tools index covers *child capabilities* the host's own agents might use.

2. **At agent invocation, hybrid BM25 + vector search.** The host takes the latest user turn (or a synthesized agent-purpose string — small experiment) and queries `tools_index`:

   ```
   query
     ↓
   BM25 over description ‖ vector similarity over description embedding
     ↓
   RRF fuse → top-K (default K=10, configurable)
     ↓
   LLM `tools` parameter for this call
   ```

   Identical pattern to how Retrieve will work over Smalt pages in M4 — same code path, different corpus. Same pattern bronze-scribing already proves out.

3. **Render and dispatch.** Top-K tools go straight into Anthropic's `messages.create(tools=[...])` (same `{name, description, input_schema}` shape — zero translation). On `tool_use` blocks, the host strips the prefix, dispatches to the owning child via `McpClientManager`, feeds `tool_result` back into the loop. Loop until `end_turn` or iter cap.

**Why this shape is right** (and less code than caller-declared globs would have been):

- The retrieval infrastructure is **already built**. M1 ships LanceDB + fastembed + FTS + hybrid query patterns. Pointing a new table at the same machinery is a small Δ, not a new system.
- Agents are simpler — describe the work, hand it to the host. No coupling between agent code and the tool registry. New tool added → agents pick it up automatically through retrieval.
- Open-ended agents (e.g. future Research) work without special-casing — same retrieval picks relevant tools whether the agent knows what's available or not.
- Scales naturally from 5 tools to 500 with no behavior change. No "static now, semantic later" two-phase migration.
- Same pattern Retrieve will use over pages in M4. Building it once for tools means M4 inherits the muscle.

**Future seams (deferred):**

- **`must_include` pin** — for agents that need a specific tool regardless of search rank.
- **LLM-router meta-tool** (Goose-style `find_tools` the LLM can invoke mid-conversation) if K=10 ever proves too small.
- **Per-agent permission filter** — hard cap on what an agent may invoke. Applied *before* retrieval as a denylist on `tools_index`. Not needed in M2.5; not blocked.
- **Agent-declared toolkits, system-curated over time.** Today the host picks tools per-call by retrieving against the latest user message. Future SME agents (defined as markdown documents — role + domain + policy + prompt + toolkit) will carry a **declared minimum toolkit**: the positive list of tools the agent is expected to use to do its job. The host merges declared + retrieved (declared always present; retrieval supplies extras). The toolkit is initially human-authored; over time it's groomed by three Phase-2/3 systems working in concert:
  - **Cogitate** proposes adding existing tools the agent has been *observed* to need but didn't declare.
  - **Toolsmith** (Phase 3, the 7th system) proposes adding *new* tools — finding existing MCP servers or specifying ones to be built — when the agent needs a capability nothing in the inventory provides.
  - **Curate** flags declared-but-never-used tools for removal.
  
  The agent definitions themselves become living artifacts the system grooms — the same self-evolution discipline CoGrind applies to the Smalt, applied to its own agent roster. This generalizes the `must_include` pin (one tool → a managed list) and is what makes "define a new agent" be "write a markdown document," not "write code." Not in M2.5; the load-bearing path remains retrieval-driven for now. Don't hard-code agent-specific tool lists in the meantime — keep the migration path clean.
- **Streaming response shape over MCP / HTTP for *external* agents.** M2.5's host API is in-process; an HTTP `/v1/messages` endpoint is later.
- **Provider implementations beyond Anthropic.**

**Patterns borrowed from Goose** (which we studied as a precedent — but didn't use as a library, since it's Rust + opinionated as a coding-agent CLI):

- **Per-session agent state.** Each `host.run_agent(...)` call gets its own conversation history; multiple agents run concurrently. Maps onto cobalt-grinding's Scheduler — each agent invocation is one task.
- **Streaming events.** As the loop runs, the host emits progress events (`tool_use_started`, `tool_result_received`, `assistant_delta`, `end_turn`) through the existing scheduler progress channel into `wiki.task_status`. Already-existing infrastructure; we just add new event kinds.
- **Provider abstraction.** A thin `LLMProvider` interface (Anthropic in M2.5; future: OpenAI, OpenRouter, etc.). CoGrindd config picks one. Mirrors Goose's 15+ providers without ourselves implementing 15+.
- **What we deliberately *don't* copy from Goose** — its Tool Router (preview, Databricks-only). We build our own tool-selection using LanceDB hybrid retrieval, which we already have.

**Child MCP server supervision (`src/cobalt_grinding/daemon/mcp_clients.py`):** `McpClientManager`. Eager spawn at `cobalt-grinding` startup (predictable; daemon startup is once, first-call latency stays flat). Per-client stdio transport; restart on crash with capped exponential backoff (1s → 2s → 4s … cap 60s); log multiplexing into the daemon's logs with a `[client:<name>]` prefix; graceful shutdown on SIGTERM (kill children, flush).

**Per-call and startup timeouts (defense against wedged children):**
- `call_timeout` (default 30s, per `[mcp.clients.<name>]`): every dispatch enforces this. On timeout, the host feeds a `tool_result` with `is_error=true` back to the LLM (so the LLM can recover or fail gracefully) and does **not** auto-restart the child (a slow tool isn't a crashed child).
- `startup_timeout` (default 10s, per `[mcp.clients.<name>]`): handshake deadline at daemon startup. If a child fails to handshake in the window, the daemon logs it and starts *without* that child's tools. The supervisor keeps trying on the same backoff schedule. This solves the "misconfigured child pins daemon startup" failure mode.

**Tool naming:**
- **`tool_prefix` per MCP client is mandatory.** Defaults to the config section name. Avoids silent collisions when two child servers expose the same tool name (e.g. both a code-parser and a pdf-parser exposing `parse_file`). Audit / log lines like `tool call deco-assaying.parse_file failed` stay unambiguous.

**Default config additions:**

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

**`pyproject.toml` entry points (unchanged from M2):**

```toml
# cobalt-grinding/pyproject.toml — daemon only
[project.scripts]
cobalt-grinding = "cobalt_grinding.daemon.main:run"
```

(The CLI binary `cogrind-workshop` ships from the sibling cogrind-workshop repo; M2.7 extracted it.)

**New code in M2.5:**

| Path | Purpose | Lines (rough) |
|---|---|---|
| `src/cobalt_grinding/host/api.py` | `run_agent(...)` public surface | ~60 |
| `src/cobalt_grinding/host/loop.py` | tool-use loop | ~150 |
| `src/cobalt_grinding/host/dispatch.py` | tool name → MCP client + call | ~80 |
| `src/cobalt_grinding/host/provider.py` | `LLMProvider` protocol + Anthropic impl | ~80 |
| `src/cobalt_grinding/host/tools_index.py` | LanceDB-backed tools index, hybrid retrieval | ~120 |
| `src/cobalt_grinding/daemon/mcp_clients.py` | `McpClientManager` (supervise / restart / timeouts) | ~250 |
| `tests/fixtures/stub_mcp_server.py` | tiny in-tree MCP server (e.g. `echo.greet`) for integration testing | ~60 |
| Existing files needing edits: `src/cobalt_grinding/app.py`, `src/cobalt_grinding/daemon/main.py`, `src/cobalt_grinding/daemon/server.py`, `src/cobalt_grinding/config.py`, `pyproject.toml` | — | small |

**Internal dispatch shape (host-side only, NOT agent-facing):**

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

**Tests (`tests/test_host_*.py`, `tests/test_daemon_mcp_clients.py`):**

Unit (no LLM):
- `test_host_dispatch.py` — dispatch routes by tool prefix; unknown name → `DispatchResult(ok=False, is_error=True)`; timeout / child-died → retryable error.
- `test_host_loop.py` — with a fake provider scripting `tool_use → end_turn`, the loop dispatches once and returns; `tool_use → tool_use → end_turn` dispatches twice; iter cap raises clearly.
- `test_host_provider.py` — Anthropic provider serializes tools list correctly from MCP `tools/list` shapes; parses `messages.create()` response into the loop's expected structure.
- `test_host_tools_index.py` — index built at startup matches `tools/list`; child crash removes entries; child restored re-indexes; hybrid retrieval returns relevant tools (BM25 hits one, vector hits another, RRF fuses).
- `test_daemon_mcp_clients.py` — child supervision (startup_timeout, call_timeout, restart on crash, SIGTERM cleanup) — uses `tests/fixtures/stub_mcp_server.py`, not a real production child.

Integration (`@integration`, opt-in by default per pyproject):
- `test_host_integration.py` — full end-to-end against the in-tree stub MCP server: `cobalt-grinding` starts with `[mcp.clients.stub]` pointed at `tests/fixtures/stub_mcp_server.py`; a test agent calls `await app.host.run_agent(system=..., messages=[{"role": "user", "content": "Greet Gary."}])`; the LLM emits `tool_use(stub.greet, ...)`; host dispatches; final assistant content reflects the stub's response. End-to-end against the real `deco-assaying` happens in M3 once that project ships.

**Done when:**

1. `cobalt-grinding` starts with the configured stub MCP child autostarted; `tools_index` includes the stub's tools (prefixed `stub.*`); `cobalt-grinding` started with no `[mcp.clients.*]` sections also starts cleanly with an empty `tools_index`.
2. `app.host.run_agent(...)` runs an end-to-end LLM tool-use loop using the stub child; the host picks tools via hybrid retrieval; final assistant message reflects the stub's response.
3. A wedged child at startup doesn't pin daemon startup; `startup_timeout` triggers; supervisor keeps retrying.
4. A wedged tool call returns a structured `tool_result(is_error=true)` to the LLM; supervisor does not restart the child.
5. Killing a child mid-loop triggers restart (capped exponential backoff); the next call succeeds; `tools_index` re-populates without restarting `cobalt-grinding`.
6. SIGTERM cleanly shuts every child; no orphan processes.
7. M3-style agents (`src/cobalt_grinding/ingest/handlers/code.py`) are *implementable* against `app.host.run_agent(system, messages)` with no MCP plumbing leakage and no tool-selection logic in agent code.

### M3 — Ingest (first impl)

**Deliberately narrow scope** — just enough to feed Retrieve and Converse so the end-to-end system can be tested.

**Preconditions:**
- [`smalt-mcp`](https://github.com/ParkviewLab/smalt-mcp) **v0.5.0+ shipped** (workstream-A cleave; storage-substrate-only surface). Ingest writes pages via `smalt.write_page` / `smalt.write_pages` / `smalt.add_link` / `smalt.add_claim`; no proposal tools on smalt-mcp.
- [`deco-assaying`](https://github.com/ParkviewLab/deco-assaying) installable. CoGrind core ships no parsers; M3's code handler reaches `deco-assaying.parse_file` through the host like any other capability.
- The default config's `[mcp.clients.smalt-mcp]` and `[mcp.clients.deco-assaying]` blocks are uncommented (or added by the user) once both are on `$PATH`.
- Ebony-enriching is **not** required for M3 — Ingest writes pages, it doesn't propose; no proposal-substrate dependency.

**Input scope** — `cogrind-workshop --ingest <path>` auto-detects file vs. directory:

- **Single local file** → source-id `file:<file pathname>`. The file *is* the source (no section structure). Hard error if the file's type isn't in the supported list.
- **Local directory** → source-id depends on what the directory is:
  - `.git/` present → `git:<remote-url>` (uses `origin` remote URL; falls back to `dir:<pathname>` if no remote is configured)
  - `.obsidian/` present → `obsidian:<dir pathname>`
  - Both present → both flags set; both sets of metadata captured
  - Neither → `dir:<dir pathname>`
  
  Granularity: **directory = one source, files within it = sections.**

- **No URLs in M3.** No standalone-file ingestion via URL. Both deferred to a later milestone.

**Supported file types in M3** (everything else is silently ignored in a directory and listed under `ignored:` in the source page's frontmatter; hard error if passed explicitly as a single file):

- Documents: `.md`, `.rtf`, `.txt`, `.pdf`
- Configs: `.json`, `.json5`, `.toml`
- Source code: `.py`, `.c`, `.cpp`, `.h`

**Git metadata captured** (best-effort; missing tools are silently skipped):

- `git remote -v` → parsed into `{name: url}` map
- `git rev-parse HEAD` → current SHA
- `git branch --show-current` → branch
- `git log -1 --format=%H%n%an%n%ae%n%aI%n%s` → HEAD commit details (sha, author, email, ISO date, subject)
- `git status --porcelain` → clean / dirty working-tree flag
- For GitHub remotes only (and only if `gh` is available): `gh repo view --json description -q .description` → repo description

**Obsidian vault metadata captured:**

- `.obsidian/` config: vault name, plugin list, settings (relevant subset)
- Existing `[[wikilinks]]` are parsed from the vault's markdown and preserved as edges in the Smalt, alongside CoGrind's own discovered links

**Re-ingestion behavior in M3:**

- When a source is re-ingested, CoGrind detects files that are *new* since the last ingestion and processes them. Existing file pages are *not* updated even if the underlying file's content has changed. SHA-based change detection on already-ingested files is post-Phase 1.

**Sub-agents implemented in M3** (pipelined; see *Ingest subsystem* above for the full diagram):

`format_classifier`, `source_fetcher` (file/dir resolution + git/vault detection + metadata capture), `structure_extractor`, `chunker`, `summarizer`, `entity_extractor`, `glossary_extractor`, `link_resolver`, `frontmatter_writer`, `page_writer`, `indexer_caller`. Sub-agents that need an LLM call `await app.host.run_agent(system=..., messages=[...])`; the host retrieves relevant MCP tools from `tools_index` (BM25 + vector hybrid; top-K to the LLM). The code handler's prompt asks the LLM to use `parse_file` when it needs symbols; the host's retrieval surfaces the autostarted `deco-assaying.parse_file` automatically. Other handlers will pick up `pdf.extract_text`, `docx.extract_text`, etc. as those MCP children get added — no agent code change required.

**Page output shape per source.** One ingest of `tests/fixtures/sample_dir/` containing four files (one unsupported) produces:

- **Source page** — hybrid layout. Multi-file source: `pages/sources/<source-id>/index.md`. Single-file source: `pages/sources/<source-id>.md` (no directory). Body: LLM-written 2-3 paragraph "what this source is" synthesized from the section summaries, plus an auto TOC of section pages.
- **Section pages** — one per supported file, at `pages/sources/<source-id>/<file>.md`. `parent_source` frontmatter links back to the source page. Body: per-file LLM summary; for code files, also a deterministic symbol outline produced by `deco-assaying`:

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

- **Entity pages** at `pages/entities/<slug>.md` — minimal in M3: name, aliases, `entity_kind`, `domains: list[ConceptPageId]`, mentioned-in back-links list. Created if new; updated additively if existing (no deletion). `domains:` used for disambiguation when same-name entities differ across domains.
- **Glossary concept pages** at `pages/concepts/<slug>.md` with `glossary: true` and `domains: list[ConceptPageId]` (multi-domain by default). Body is the short definition (1-3 sentences) plus per-source evidence snippets.
- **Domain concept pages** at `pages/concepts/<slug>.md` with `is_domain: true`. Created on first reference (Ingest agent proposes a new domain when a source/term clearly belongs to one not yet in the Smalt — same propose-don't-act discipline). Domain hierarchy (CS is a sub-domain of computing) lives as `subdomain_of` labeled links, *not* in the `domains:` field.
- **Auto-generated `pages/glossary.md`** — an `IndexPage` over every `glossary: true` concept page. Same term across multiple meanings shows as multiple entries (one per ConceptPage), each tagged with its domains inline. Multi-domain entries (one meaning, several domains) show with all domains tagged on the single entry. Format example:
  ```
  - **Bayesian inference** (stats, ml, phil_sci) — A method for updating beliefs in light of evidence…
  - **Cell** (biology) — The smallest structural and functional unit of life.
  - **Cell** (spreadsheet) — A single addressable rectangle in a spreadsheet grid.
  - **Tree** (cs) — A recursive node-based data structure…
  - **Tree** (botany) — A perennial woody plant with a single main stem…
  ```
- **Auto-generated `pages/domains.md`** — an `IndexPage` over every `is_domain: true` concept page. Lists each domain with a one-line description and a count of glossary entries / source pages tagged in that domain. Domain hierarchy (parents/children via `subdomain_of` links) renders as nested tree.

**Naming conventions:**
- Source ID: `<kind>-<basename>-<8char-hash>` (e.g. `dir-sample_dir-a3f1`, `git-cobalt-grinding-9b2c`, `obsidian-myvault-7d12`). Hash over `location_uri` disambiguates same-named sources.
- Section ID: `<source-id>::<file-relative-path>` (e.g. `dir-sample_dir-a3f1::src/utils.py`).
- Entity / concept ID: `<prefix>-<slug>` (`ent-some-org`, `con-embedding`).
- **Slug disambiguation rule (Wikipedia-style, agent-chosen).** When a new ConceptPage's natural slug collides with an existing one of *different meaning* (e.g., `tree` for the CS data structure vs. `tree` for the botanical plant), the agent appends a free-form, human-readable meaning hint to the new page's slug: `tree-data-structure.md` and `tree-plant.md`, or whichever pairing the agent judges most readable. The `domains:` field carries domain info; **the slug doesn't encode domain.** Same rule applies to entity-name collisions and any other slug collision. Same-meaning-multi-domain doesn't trigger this — that's one ConceptPage with multiple domains in `domains:`.

**SME ingest agent's domain-assignment job.** When extracting a glossary term, the agent picks `domains:` based on three signals, in roughly this order of strength:
1. The source's own `domains:` (if the SourcePage is tagged `[cs]`, the default for terms extracted from it is `[cs]`).
2. Surrounding context in the source (a term mentioned in a CS-flavored paragraph).
3. The term itself (some terms are domain-specific — "monad", "homotopy", "chiral" — and their names alone are strong signals).

Multi-tag liberally when context is genuinely mixed. Mark `domain_confidence: low` on the concept page when the assignment is uncertain — Curate periodically reviews low-confidence domain assignments as a drift signal.

**`format_classifier` dispatch:**
- Input: a `Path`. Single-file ingest: extension lookup → handler. **Hard error** if extension not in supported list.
- Directory ingest: walk, group by extension, dispatch each file to its handler. Unsupported extensions → recorded in `source.ignored` (no error).
- `.h` disambiguation runs **once per directory ingest**: scan source root for sibling `.cpp/.cc/.cxx/.hpp` (heuristic step 1) → content-sniff first few `.h` files for `namespace`/`class`/`template`/`extern "C"` (step 2) → otherwise C (step 3). Sets a per-ingest `h_lang ∈ {"c","cpp"}` used for all `.h` dispatches in that ingest. Overridable via `--lang-h=c|cpp`.
- Handler signature: `handle(file: Path, source_ctx: SourceContext, app: App) -> SectionResult`. Handlers that need an LLM call `await app.host.run_agent(...)`; the host runs the tool-use loop and picks tools via `tools_index` retrieval.

**Surfaces:**

- CLI: `cogrind-workshop --ingest <path>` (auto-detects file vs. directory). MCP-only via the daemon — no in-process fallback. Optional `--lang-h=c|cpp`.
- MCP tools registered: `wiki.ingest`, `wiki.list_sources`, `wiki.source_status`. `wiki.ingest` enqueues an ingestion task on the daemon's worker pool (set up in M2) and returns a `task_id`; clients poll via `wiki.task_status` for progress.

**Concurrency:**

- Multiple `wiki.ingest` calls run in parallel on the daemon's thread pool (configurable size, default `min(8, os.cpu_count())`).
- The expensive work (file I/O, LLM calls, fastembed inference, tree-sitter / pdf parsing) parallelizes — the GIL is released by the libraries doing the heavy lifting.
- The corpus-write step (page write + index update) goes through the single-writer mutex set up in M1.

**Re-ingestion semantics (Phase 1):**

- New files only. Existing section pages are *not* rewritten even if their underlying file's content has changed. SHA-based file-content change detection is post-Phase-1.
- The **source page IS regenerated** each run: refreshed `structure_inline`, refreshed `sections` list, refreshed `ignored` list (a previously-ignored `.docx` stays in `ignored:` until support arrives), refreshed `fetched_at`.
- Existing entity / concept page updates are **additive** (new section IDs appended to back-link / evidence lists). No deletion.
- Glossary `IndexPage` is regenerated by the indexer.
- Ingest is **idempotent at the page-write layer**: re-running with no file changes is a no-op (caught by the indexer's content-hash check).

**Done when:**

1. `cogrind-workshop --ingest tests/fixtures/sample_dir/` (a generic doc directory containing supported and unsupported file types) produces one source page at `pages/sources/<id>/index.md` with sections for the supported files and an `ignored:` list for the rest.
2. `cogrind-workshop --ingest tests/fixtures/sample_repo/` (a small git repo) produces a source page with the captured git metadata in frontmatter and section pages for its supported files. Code section pages include a deterministic symbol outline (from `deco-assaying`) in addition to the LLM summary.
3. `cogrind-workshop --ingest tests/fixtures/sample_vault/` (a small Obsidian vault, optionally also a git repo) produces a source page with vault config + git metadata, section pages, and the existing `[[wikilinks]]` preserved as edges.
4. `cogrind-workshop --ingest tests/fixtures/sample.md` (a single supported file) produces a single source page at `pages/sources/<id>.md` (flat — no directory).
5. `cogrind-workshop --ingest tests/fixtures/sample.docx` (a single unsupported file) errors with `type not supported`.
6. The ingest produces at least one entity page (`pages/entities/<slug>.md`) and at least one glossary `ConceptPage` (`pages/concepts/<slug>.md` with `glossary: true`); `pages/glossary.md` exists as an `IndexPage` with `auto_generated: true` and is regenerated on subsequent ingests.
7. Re-ingesting any of the above adds only new files; existing section pages are unchanged; the source page's `sections:` list grows; entity / concept pages have new sources appended.
8. A `.h` file in a directory with sibling `.cpp` parses as C++ (verified by the symbol kinds in its section page); the same file in a C-only fixture parses as C; `--lang-h=c` overrides the heuristic.
9. All of the above work identically when invoked via the `wiki.ingest` MCP tool from a connected client.

### M4 — Retrieve
- Retrieve pipeline with metadata filter + hybrid BM25/vector + RRF
- Graph expansion (1-hop)
- Gap detection
- Result caching
- `cogrind-workshop --query "<question>"` returns ranked pages with snippets
- MCP tools registered: `wiki.search`, `wiki.get_page`, `wiki.traverse`, `wiki.find_gaps`

**Done when:** queries against the seed corpus return relevant pages with reasonable ranking; gaps are detected when nothing matches; same retrieval available via MCP.

### M5 — Converse
- Converse orchestrator using Retrieve as a tool
- Citation checker
- `cogrind-workshop --ask "<question>"` returns a Converse answer with citations
- Conversational mode: `cogrind-workshop --chat` (interactive REPL evolves later from this same flag-driven shape)
- MCP tool registered: `wiki.ask`

**Done when:** end-to-end demo — ingest a doc, ask a question about it, get a cited answer; the same conversation works from Claude Desktop / Claude Code via the MCP server.

**Phase 1 ends here.** End-to-end working CoGrind: ingest → retrieve → converse, with MCP exposure.

---

### M6 — Research (first impl) — Phase 2

*Scope to be discussed and locked in before this milestone starts (same way M3 was scoped).*

**Preconditions:** [`smalt-mcp`](https://github.com/ParkviewLab/smalt-mcp) v0.5.0+ (Ingest writes source pages on accept) AND [`ebony-enriching`](https://github.com/ParkviewLab/ebony-enriching) v0.1.0+ (proposals + gaps). Both substrates required.

**Why M6 matters: it turns corpus growth into a flywheel** (see *Cross-system flows* above for the full picture). Pre-M6, every source is hand-pointed by the user. Post-M6, two new modes become available — *reactive* (gap → proposal → accept → ingest) and *proactive* (`cogrind-workshop --research "<topic>"` seeds proposals). In both, Research *proposes only*; the user stays in the approval loop until trust is earned.

The intent of M6's first impl: stand up Research narrowly enough that a gap signal from any of the five emitters (Retrieve, Converse, Ingest, Curate, Cogitate) can produce a *proposal* for what to ingest next. **Proposes only — does not auto-ingest.**

Likely first-impl shape (placeholder, to be refined):
- Read gap signals from ebony-enriching (`ebony.list_gaps()`).
- Take an explicit research request via CLI / MCP (`cogrind-workshop --research "<topic>"` / `wiki.research`) — the request enters the queue via `ebony.add_gap(query=...)` first, then Research processes it.
- Search a small set of source backends (web search via `WebSearch` tool, GitHub repo search via `gh`, possibly ArXiv) — bounded budget per request.
- Evaluate candidates for relevance, authority, recency, accessibility.
- Write `ProposalPage`s of `proposal_kind: source_adoption` via `ebony.write_proposal(...)` — landing in ebony-enriching's `proposals/research/` (see *Proposal document shape and lifecycle*).
- *No auto-ingestion.* User accepts → cobalt-grinding orchestrates the cross-substrate publish: `smalt.write_page` to add the source page, `ebony.update_proposal_status(applied)`, `ebony.remove_gap` to clear the queue entry.

**Proposal-as-hypothesis discipline.** Each Research proposal frames an `Observation` (the gap signal it's responding to), a `Hypothesis` ("ingest this candidate source X"), a `Prediction` ("queries that hit gap Y will return non-empty results after this source is in the corpus"), and a `Test` — for cheap-tier candidates (the source has an accessible summary or abstract), Research runs a dry-retrieval against the would-be summary and reports whether the gap-signal query would now match. Expensive-tier candidates (full ingest required to know) are marked `test_status: untestable` with the user as the test. Status flows through the lifecycle (proposed → validated → applied; or proposed → rejected); test artifacts go to `ebony.write_experiment(...)`.

**Done when:** a gap signal from `ebony.list_gaps` produces a `ProposalPage` in ebony-enriching's `proposals/research/` (via `ebony.write_proposal`) with a ranked candidate list, reasoning, and (where cheap) a test result captured via `ebony.write_experiment`; user can accept a proposal and cobalt-grinding's orchestration flows the source through M3 ingestion (`smalt.write_page`) + transitions the proposal to `applied` (`ebony.update_proposal_status`) + removes the gap entry (`ebony.remove_gap`) + runs the apply-time post-mortem (see *Apply-time post-mortem: closing the learning loop*) — the post-mortem itself lands as `ebony.write_experiment(input={kind:post_mortem}, ...)`, and where the lessons generalize, the agent extends `pages/concepts/research-methodology.md` via `smalt.add_claim`.

### M7 — Cogitate (first impl) — Phase 2

*Scope to be discussed and locked in before this milestone starts.*

**Preconditions:** [`smalt-mcp`](https://github.com/ParkviewLab/smalt-mcp) v0.5.0+ (reads pages via `smalt.list_pages` / `smalt.read_page` / `smalt.traverse`) AND [`ebony-enriching`](https://github.com/ParkviewLab/ebony-enriching) v0.1.0+ (writes proposals + experiment records).

The intent: stand up Cogitate's narrow first impl so the Smalt starts producing emergent connections from what's already there. **Proposes only — does not modify pages.** Cogitate is the **constructive** counterpart to Curate (which is critical) — Cogitate generates new structure, Curate flags problems with existing structure.

**Cogitate's SME sub-agents apply the scientific method explicitly.** The sub-agent breakdown is shaped around Observe → Hypothesize → Predict → Test → Validate:

- `observer` — walks the link graph + entity/concept pages in LanceDB; surfaces patterns and anomalies (densely-connected entity clusters lacking a parent concept; near-duplicate names; claim disagreements; recurring frontmatter keys not in SCHEMA.md).
- `hypothesis_generator` — proposes a structural change explaining the observation (a new edge with label X; a new concept named Y; a new schema field Z).
- `predictor` — pre-registers a measurable prediction the change should make true (e.g., "after adding edge X, query Q's recall rises from R1 to R2"; "after adding schema field Z, the N drift-flagged pages become conformant").
- `experimenter` — runs a cheap test where the cost tier permits (schema dry-run; query benchmark before/after on cached corpus; corpus re-link with the proposed edge). Marks `test_status: passed | failed`.
- `validator` — checks whether the prediction held; transitions the proposal to `validated` or `rejected`.

The sub-agents are SME agents in the M2.5-future-seam sense (defined as markdown documents with declared toolkits) — once that machinery lands. Until then, Cogitate's M7 first impl runs them as in-process Python functions calling `host.run_agent(...)`; the structure is the same, the substrate evolves.

Likely first-impl scope (placeholder, to be refined):
- Walk the Smalt's link graph + entity/concept pages via `smalt.list_pages` + `smalt.traverse` + `smalt.read_page`.
- Detect a small set of patterns: clusters of densely-connected entities that lack a parent concept page; entity pages that share many incoming links with unrelated entity pages (suggesting a missing concept); claims about the same entity that disagree; recurring undeclared frontmatter keys (a `schema_addition` candidate).
- Write `ProposalPage`s with `proposal_kind ∈ {wiki_edge, concept_merge, novel_concept, schema_addition, contradiction}` via `ebony.write_proposal(...)` — ebony-enriching routes by `proposal_kind` / `proposed_by`:
  - schema-kinds (`schema_addition`, `schema_drift`, `schema_removal`) → ebony's `proposals/schema/`
  - everything else with `proposed_by: cogitate` → ebony's `proposals/cogitate/`
- Each proposal carries the full Observation / Hypothesis / Prediction / Test / Reasoning shape.
- Cheap-tier proposals (schema dry-run, query benchmark) are tested automatically; results captured via `ebony.write_experiment(...)`; cogitate transitions the proposal via `ebony.update_proposal_status(validated, test_status=passed)`.
- Run on demand via CLI / MCP (`cogrind-workshop --cogitate` / `wiki.cogitate`); daemon scheduled mode is later.
- More sophisticated synthesis (taxonomy building, multi-hop pattern detection, cross-source narrative reconciliation) is deferred to later Cogitate milestones.
- *Tension worth being aware of:* Cogitate benefits from a fuller corpus. Its first impl runs against whatever corpus exists at the time, which may be thin. The first-impl bar is "it works and produces sane, well-tested proposals on a small corpus" — sophistication grows as the corpus does.

**Done when:** running cogitate against a Smalt produces categorized `ProposalPage`s in the appropriate `EBONY_ENRICHING_DIR/proposals/*` directories (verifiable via `ebony.list_proposals(system=cogitate, ...)`), with cheap-tier proposals tested automatically (results recorded via `ebony.write_experiment` under `EBONY_ENRICHING_DIR/experiments/<proposal-id>/`) and shown to the user pre-validated; the user can review hypothesis + evidence together and accept or reject individually; lifecycle status updates flow through `ebony.update_proposal_status`. On apply (for schema/edge/concept proposals), cobalt-grinding orchestrates the cross-substrate publish (`smalt.write_page` to add the new page or update an existing one + `ebony.update_proposal_status(applied)`) **and runs the apply-time post-mortem** — for cheap-tier proposals, a Haiku-only summary lands as `ebony.write_experiment(input={kind:post_mortem}, ...)`; for medium/expensive-tier, the full pipeline runs, and where the lessons generalize, the agent extends `pages/concepts/cogitate-methodology.md` (or a kind-specific pattern page) via `smalt.add_claim`. See *Apply-time post-mortem: closing the learning loop*.

### M8 — Curate (first impl) — Phase 2

*Scope to be discussed and locked in before this milestone starts.*

**Preconditions:** [`smalt-mcp`](https://github.com/ParkviewLab/smalt-mcp) v0.5.0+ (audits via `smalt.list_pages` / `smalt.read_page` / `smalt.incoming_links` / `smalt.traverse`) AND [`ebony-enriching`](https://github.com/ParkviewLab/ebony-enriching) v0.1.0+ (writes audit findings as proposals).

The intent: stand up Curate's narrow first impl so the Smalt starts auditing itself. **Flags only — does not delete or modify pages.** Curate is the **critical** counterpart to Cogitate (which is constructive) — Cogitate proposes additions, Curate flags problems with what's already there.

**Curate's findings are also `ProposalPage`s** — same scientific-method discipline applied to *removal/correction* hypotheses rather than additive ones. Each finding frames the observed drift as a falsifiable claim ("these N pages use a `tags:` key SCHEMA.md doesn't mention" → testable by counting pages and checking SCHEMA.md). Predictions are usually about the corpus's current state; tests are usually corpus-walking queries. Most Curate proposals are cheap-tier and arrive at the user pre-validated.

Likely first-impl scope (placeholder, to be refined):
- Audit the Smalt via `smalt.list_pages` + `smalt.read_page` + `smalt.incoming_links` (the latter is the canonical "what links to this page" view added at smalt-mcp v0.3.0).
- Detect and flag a small set of issues, each as a `ProposalPage` of the appropriate kind:
  - `proposal_kind: orphan` — pages with no inbound links (via `smalt.incoming_links` returning empty) and no recent access
  - `proposal_kind: duplicate` — entity pages with very similar names/aliases (excluding multi-domain disambiguation cases)
  - `proposal_kind: broken_link` — internal links to nonexistent pages (via traverse + read_page round-trips)
  - `proposal_kind: staleness` — source pages with `fetched_at` older than a configurable threshold
  - `proposal_kind: schema_drift` — pages using a frontmatter key SCHEMA.md doesn't mention; required fields missing on existing pages
- Write findings via `ebony.write_proposal(..., proposed_by: curate)` — lands in ebony-enriching's `proposals/curate/`. Schema-drift findings stay there too (Curate flags drift; **Cogitate** is the system that proposes schema additions — keeps the constructive/critical split clean).
- Run on demand via CLI / MCP (`cogrind-workshop --curate` / `wiki.curate`); daemon scheduled mode is later.
- More sophisticated audits (extraordinary-claim flagging, dust-detection, low-confidence-domain audits, **schema-removal candidates** — fields whose tests fail under accumulated evidence — i.e., the `applied → re-proposed` lifecycle edge) are deferred to later Curate milestones.
- A separate audit detects an excessive rate of `untestable` proposals across all systems' queues (via `ebony.list_proposals(test_status="untestable")` cross-system); flags it as a discipline-slipping signal.

**Done when:** running curate against a Smalt corpus produces categorized `ProposalPage`s in ebony-enriching's `proposals/curate/` (verifiable via `ebony.list_proposals(system=curate)`) with concrete flagged pages, observations, and (where applicable) cheap-tier test results captured via `ebony.write_experiment`; user can review and accept/reject each finding individually; lifecycle status updates flow through `ebony.update_proposal_status`. On apply (e.g., a duplicate-merge accepted), cobalt-grinding orchestrates the cross-substrate publish (`smalt.remove_page` for the merge-into target + `smalt.write_page`/`smalt.update_claim` to consolidate + `ebony.update_proposal_status(applied)`) **and runs the apply-time post-mortem** (see *Apply-time post-mortem: closing the learning loop*) — for medium/expensive-tier corrections, lessons go to `pages/concepts/curate-methodology.md` via `smalt.add_claim`; recurring drift patterns may warrant a new ConceptPage proposed via `ebony.write_proposal(..., proposal_kind: novel_synthesis)`.

### M9+ — beyond Phase 2's first impls

Phase 3 is the **self-evolution phase**: the systems CoGrind needs once it's mature enough to grow / judge / improve itself. To be milestoned when we get there:

- **Toolsmith — the 7th agentic system.** Closes the self-evolution loop on CoGrind's own capability surface. Reads tool-gap entries from ebony-enriching (`ebony.list_gaps`, fed by all six other systems via `ebony.add_gap` — see *Cross-system flows*), searches existing MCP servers (registries, GitHub, npm, PyPI), evaluates fit / maturity / license, and writes proposals via `ebony.write_proposal(..., proposed_by: "toolsmith")` — landing in `EBONY_ENRICHING_DIR/proposals/toolsmith/` — of two kinds: *adopt* (use this existing server) or *specify* (no fitting server exists; here's a requirements doc for one to be built — the deco-assaying pattern, codified). Same proposal-only discipline as Research; same search/evaluate engine, applied to capabilities instead of knowledge. Composes pieces of Research (search + evaluate), Curate (audit agent rosters' toolkits for unused tools), and Cogitate (propose toolkit additions for tools observed-but-not-declared). Reasonable timing: after Phase 2 — needs Research / Curate / Cogitate as building blocks, and needs enough agents running for tool-usage signal to be real.
  - **Pre-Toolsmith expectation:** until Toolsmith exists, *we* (humans + Claude) play its role: when we hit a capability gap, we either find an existing MCP server or spawn a new project (deco-assaying was the first such — and is the proof point that this pattern works). Phase 1 / Phase 2 systems are built this way; Phase 3 is when the system takes over the curation of its own toolset.
- **A source veracity / quality system** (working name TBD — candidates: **Vet**, **Appraise**, **Weigh**). Eventually we'll want a dedicated agentic system that judges and rates sources for veracity and quality — authority of author / publisher, recency, evidence strength, peer review status, citation density, prior reliability of the source domain. It writes per-source quality + veracity scores into source-page frontmatter. Used by: **Research** (prefer high-quality candidates), **Cogitate** (weigh conflicting claims by source quality when surfacing contradictions), **Curate** (flag pages whose claims rest on low-quality sources). Reasonable timing: after enough corpus exists for "prior reliability of a domain" to be meaningful — probably alongside or after deeper Curate. (Together with Toolsmith, this is part of the Phase 3 self-evolution arc — Toolsmith judges *capabilities*, Vet judges *knowledge*.)
  - **Schema implication for earlier milestones:** the `Source` frontmatter model defined in M0 should reserve fields for `quality_score`, `veracity_score`, `evaluated_at`, and `evaluation_notes` (default `null` / `unrated`), so this future system can populate them without a schema migration.
- DuckDB analytical layer over the same Lance files (read-only).
- Image / multimodal ingestion (whiteboard, infographic, chart).
- *CoGrind-aware* Claude Code skill / plugin (distinct from the MCP server, which is in Phase 1).
- URL ingestion.
- Broader file-type support.
- Full re-ingestion change detection (SHA-based, beyond the new-files-only rule of M3).
- Deeper impls of Research, Cogitate, and Curate.

---

## Decisions made (with why)

| Decision | Why |
|---|---|
| Markdown canonical, indexes derived | Human-readable + editable; LLM-native; git-diffable; portable; lets a human fix what the agent breaks |
| LanceDB as primary index | Built-in hybrid (BM25 + vector + filter), purpose-built for this workload, time-travel useful for dev, Python SDK matches agent layer |
| DuckDB deferred to Phase 2 | Analytics is a step-2 feature; Phase 1 doesn't need cross-cutting SQL |
| Python runtime, Claude Agent SDK | Best LLM/agent ecosystem, LanceDB native Python, sub-agent orchestration out-of-box |
| Single-writer to corpus | Ingest is the only writer; Curate / Cogitate / Research propose into `tasks/`. Avoids race conditions and keeps audit trail clear |
| "Propose, don't act" for auditors | Curate / Cogitate / Research write proposals; user or Converse approves. Cost of a wrong autonomous edit > cost of a slightly cluttered Smalt |
| Original structure preserved as provenance | Locality is signal; lost at ingestion time it can't be recovered |
| Seven systems, three in Phase 1 (Ingest / Retrieve / Converse), three in Phase 2 (Research / Cogitate / Curate), one in Phase 3 (Toolsmith) | Cogitate and Research are the most easily over-promised; Curate needs an aged corpus to be useful. Toolsmith needs the Phase 1 + 2 systems running to have agents to observe and a tool inventory to evaluate against. Deliver the boring-but-useful core first. |
| **Toolsmith as the 7th system, paralleling Research** | CoGrind grooms its own Smalt; Phase 3 extends the same discipline to grooming its own *capability surface*. Until Toolsmith exists, humans + Claude play its role: capability gaps get filled by hand-finding an existing MCP server or spawning a new project (deco-assaying was the first). The 7th system is when that work moves from human-driven to system-driven, with humans staying in the approval loop. Same proposal-only safety as Research, since adding an MCP server is a security / supply-chain / behavior-shape change. |
| **`SCHEMA.md` and `POLICY.md` are living documents** (one pair per substrate) | Both substrates ship their own `SCHEMA.md` + `POLICY.md` placeholders at bootstrap time. Day-0 they're seeded by humans + Claude (the "0-day CoGrind"); thereafter they're groomed via the proposal-as-hypothesis queue (which lives in ebony-enriching) — Cogitate proposes additions (M7), Curate flags drift and removal candidates (M8), the user approves edits, cobalt-grinding orchestrates the apply (write the substrate's doc file + update_proposal_status). The Smalt documents not only its content but its own structure and policies; both grow by the same discipline. Updated in lockstep with the Pydantic models inside each substrate (cross-validation on indexer pass; no codegen). |
| **Multi-domain glossary; domains as first-class ConceptPages** | Same-meaning-spanning-domains lives in one ConceptPage with `domains: list[ConceptPageId]`; different-meanings-same-term lives in separate ConceptPages with Wikipedia-style human-readable disambiguator slugs (`tree-data-structure.md` vs `tree-plant.md`). Domains themselves are ConceptPages with `is_domain: true` — no separate taxonomy file; domain hierarchy uses `subdomain_of` labeled links, not `domains:`. Auto-generated `pages/domains.md` IndexPage. Sources and entities carry `domains:` the same way. The slug doesn't encode domain; only meaning, when needed for disambiguation. |
| **Proposals are hypotheses with falsifiable predictions** | Every proposal across every system carries `Observation`, `Hypothesis`, `Prediction`, `Test`, and a lifecycle status (`proposed → under_test → validated → rejected → applied → superseded`). Where the test is cheap (schema dry-run, query benchmark), the system runs it automatically before the user reviews; the user reviews **hypothesis + evidence together**, not opinion. Where the test is expensive or impossible, the proposal is `untestable` with a reason — the user becomes the test, but Curate audits the rate of `untestable` for discipline drift. **Truth is provisional**: accepted findings can re-enter the queue under contradicting evidence (the `applied → proposed` edge), which is what makes schema *removal* possible alongside addition. The user's own direct edits to SCHEMA.md/POLICY.md are held to the same falsifiability bar — keeps both sides honest. The discipline applies recursively to the system's own structure: the Smalt documents not only what it knows but how it knows. |
| **Two substrate MCP servers: `smalt-mcp` (storage) + `ebony-enriching` (lab notebook)** | The "storage substrate" and "scientific method" surfaces are two separate concerns with different shapes — Smalt is LanceDB-backed and search-oriented; sciencing is filesystem-text and append-with-status-transitions. Originally bundled in smalt-mcp; cleaved at smalt-mcp v0.5.0 because (a) the LanceDB / embedder cost is irrelevant for a proposal-tracking workload (no FTS / vector needed; volume is tiny), (b) the two surfaces evolve at different rates (smalt-mcp's storage tools stabilize toward 1.0; the sciencing schema will iterate as cognitive systems land), and (c) it mirrors the agent-vs-framework split for capabilities — each separable concern is its own MCP child. Smaller per-server surface (17 + 13 vs. one 30-tool monolith); parallel development across the two repos; cleaner permission boundaries. Cobalt-grinding consumes both as MCP children. |
| **Lab-notebook framing for `ebony-enriching`** — it **records**; it doesn't enforce | Ebony-enriching is the scientific-method *substrate*: it stores proposals, experiments, gaps. It does *not* enforce falsifiability discipline, run experiments, or decide what to apply. That work lives in cobalt-grinding's cognitive agents — they read POLICY.md (which lives in `EBONY_ENRICHING_DIR/schema/POLICY.md`), follow it, and write the resulting hypothesis/prediction/test/result via ebony-enriching's MCP tools. Keeps ebony-enriching small (filesystem + markdown, ~20 source files at v0.1.0; no LanceDB, no embedder, no LLM client) and parallels how scientists actually work — the notebook holds the record, the scientist holds the method. |
| **No cross-server `apply_proposal` tool — cobalt-grinding orchestrates the publish** | When a validated proposal transitions to `applied`, the "publish" is a multi-step cross-substrate flow (write the Smalt page, transition the proposal status, optionally remove a gap entry). We deliberately do **not** put `ebony.apply_proposal` as a tool — that would make ebony-enriching depend on smalt-mcp's surface and break the "both substrates have zero outbound deps" property. Instead, cobalt-grinding's cognitive agent calls `smalt.write_page(...)` + `ebony.update_proposal_status(applied)` + (optionally) `ebony.remove_gap(...)` in sequence. The orchestration logic is a few lines of Python on cobalt-grinding's side; if a class of cross-substrate publish emerges that wants reuse, it lifts into cobalt-grinding-side helpers, not into either MCP server. |
| **Apply-time post-mortem on every applied proposal** | When cobalt-grinding orchestrates a proposal's apply, it doesn't just write the page and update the status — it also runs a post-mortem on the proposal's lifecycle from initiation through validation: reads the full history (frontmatter, all experiment records, superseded predecessors, related rejected siblings), synthesizes lessons-learned (what worked, what didn't, what false starts there were, what system failure modes were illuminated), and decides whether the lessons generalize. Where they do, the agent extends a methodology ConceptPage (e.g., `pages/concepts/cogitate-methodology.md`) via `smalt.add_claim`, or — for sufficiently broad lessons — proposes a new SynthesisPage via `ebony.write_proposal(..., proposal_kind: novel_synthesis)` (the system doesn't get to write Smalt syntheses without going through the proposal loop). The post-mortem itself always lands in ebony-enriching as `ebony.write_experiment(input={kind: post_mortem}, ...)`, so the record of the system's own learning trajectory is queryable later — even when the post-mortem decides nothing was worth keeping (Curate can later audit "rate of post-mortems that produced Smalt writes" as a discipline-slipping signal). Cost-tiered (trivial → skip; cheap → Haiku-only summary; medium/expensive → full pipeline with judgment call). This is the concrete mechanism behind northstar.md's *CoGrind learns how to learn better*: without it, the proposal track record is inert data; with it, each apply updates the system's judgment about how to propose better, and the methodology pages become part of the next run's context. See *Apply-time post-mortem: closing the learning loop*. |
| Image ingestion deferred to Phase 2 | Adds multimodal complexity; Phase 1 covers ~80% value at <40% effort |
| Per-value confidence + provenance | Hard data from charts is softer than from CSVs; the index needs to know |
| **No source copies** — only location + content hash + structure metadata | The Smalt is *notes about* sources, not a republication of them. Avoids redistribution concerns; smaller footprint; forces source-pointer discipline; matches how a human researcher actually works. Tradeoff: re-verification requires re-fetch, and local-file pointers are machine-bound. |
| **MCP server in Phase 1, not deferred** | Each subsystem exposes both CLI and MCP tools as it's built. Unlocks excellent chat UX (Claude Desktop / Claude Code) for nearly zero extra cost; avoids building any custom UI in Phase 1. |
| **fastembed + `BAAI/bge-small-en-v1.5` as the default embedding** | Local, ONNX-quantized — no API keys, no per-token cost, runs offline, your private notes never leave the machine for embedding. Same pattern bronze-scribing already uses. Hosted providers (Voyage, OpenAI) remain supported via config for users who want higher-quality vectors and accept the cost. |
| **TOML for config (`config.toml`); markdown only for agent-readable rule docs** | Config is typed key-value runtime plumbing — TOML is built for that. Markdown is reserved for SCHEMA.md / POLICY.md / Smalt pages where prose and LLM-readability matter. |
| **One daemon, in-process subsystems, threads for parallelism** (not six daemons; not subprocess-per-system) | Single fastembed model load (~100MB), single LanceDB connection, function-call inter-system communication, one process to keep alive. Threads get real parallelism because fastembed (ONNX), lancedb (Rust), pyarrow, and network I/O all release the GIL. Subprocess pools may arrive later for *specific* crash-prone operations (PDF / code parsers) — not as the default architecture. |
| **Task scheduler with worker pool from M1, not deferred** | Establishes the right factoring (no module globals, dependency-injected `App`, async tasks) before M3's first ingestion needs it. M1 doesn't strictly need parallelism for the indexer, but having the scheduler in place means M3 ingestion is daemon-backed by default rather than a retrofit. |
| **Two binaries (`cobalt-grinding` daemon + `cogrind-workshop` CLI) — single MCP protocol between them** | Same shape as `claude` ↔ Claude Code: the CLI is a polished human interface, the daemon is the backend agent. The CLI is *purely* an MCP client — no in-process fallback, no business-logic duplication. One source of truth (the daemon). The CLI evolves cleanly toward a richer interactive UX (REPL) without dragging business logic with it. External clients (Claude Desktop, Claude Code, future web UIs) hit the *same* tool surface the CLI does — nothing is privileged. |
| **Bootstrap (init) is daemon-startup behavior, not a CLI verb** | `cobalt-grinding` auto-creates the canonical Smalt layout on first run if `smalt_dir` is empty. Removes a CLI/daemon ordering hazard ("did I run init before starting the daemon?") and ensures bootstrap-and-serve is one operation. |
| **CLI fails fast when no daemon is running** | A Smalt-operation flag invoked with no daemon prints a clear error pointing at `cobalt-grinding` rather than silently doing in-process work. Forces good daemon UX (must be easy to start, reliable) and eliminates "two App instances racing on the corpus" entirely. |
| **`cobalt-grinding` is an MCP host (server *and* client side)** — M2.5 establishes this | All seven subsystems (six in Phase 1+2, plus Phase 3 Toolsmith) are agentic and most will eventually want external capabilities (parsers for new file types, web search, fetchers, archive lookups). Establishing the host pattern once means new capabilities slot in through a uniform interface rather than a retrofit later. The same protocol CoGrind already serves to clients is the natural shape for CoGrind's own agents to consume external capabilities. |
| **Capability vs. infrastructure** — capabilities run as MCP children, reached via the host's agent runtime; infrastructure stays direct imports | Capabilities are anything CoGrind invokes against *external content* (parse this file, extract text from this PDF, search the web). Infrastructure is anything CoGrind uses to maintain *its own state* (LanceDB queries, embedder calls, page writes, LLM client). The line matters because ingest grows by adding capabilities — keeping that growth path as "add an MCP child server in config" rather than "ship a CoGrind release" is the architectural payoff. Forcing infrastructure through MCP would pay serialization cost on hot paths for no extensibility benefit, since infrastructure isn't an extension point. |
| **Tool dispatch errors return as `DispatchResult(ok=False, is_error=True)` host-internally, surfaced to the LLM as `tool_result(is_error=true)`** | The host runs the tool-use loop, not the agent — so the LLM is the one that needs to recover from tool errors. Feeding back a structured `tool_result` with `is_error=true` lets the LLM retry, switch tools, or fail the turn gracefully without CoGrind code wrapping every call in try/except. Distinguishing dispatch-layer failures (timeout, child gone) from tool-reported failures (the tool ran and returned an error) is the supervisor's job, not the agent's. |
| **Code parsing lives in a separate project ([`deco-assaying`](https://github.com/ParkviewLab/deco-assaying)), not in CoGrind core** | Parsers are capabilities, not infrastructure (per the M2.5 line). Living outside CoGrind's repo means deco-assaying grows language coverage on its own cadence, accepts contributors without forcing changes through CoGrind, and serves as the proof point that any future parser/extractor (PDF, OCR, archives, fetchers) plugs in identically. We studied [`mcp-code-parser`](https://github.com/boxabirds/mcp-code-parser) as precedent; built our own for broader language coverage and an output shape (structured `ParseResult` with `Symbol` / `Import` lists) tuned to CoGrind's ingest pipeline. |
| **Mandatory `tool_prefix` per MCP child** | Avoids silent collisions when two child servers expose the same tool name (e.g. both a code-parser and a pdf-parser exposing `parse_file`). Audit / log lines like `tool call deco-assaying.parse_file failed` stay unambiguous. |
| **Eager spawn of MCP children at daemon startup** (with `autostart=false` escape hatch) + per-call and startup timeouts | `cobalt-grinding` is a long-running daemon — pay startup cost once; keep request-time latency flat; surface misconfigured children early rather than at first call. `startup_timeout` (default 10s) prevents a wedged child from pinning daemon startup; `call_timeout` (default 30s) prevents a wedged tool from blocking an agent loop indefinitely. Timeout doesn't auto-restart the child (slow ≠ crashed). |
| **`cobalt-grinding` runs the LLM tool-use loop; agents see only `host.run_agent(system, messages)`** | An MCP host's defining job is to orchestrate the LLM tool-use loop — that *is* the abstraction agents want. Earlier drafts of M2.5 had agents calling a `ToolBundle` directly; that reinvented what an MCP host already does and coupled agent code to the tool registry. The host shape (Anthropic-`messages`-compatible) keeps agents tiny and decouples them from the MCP plumbing entirely. |
| **Tool selection via hybrid retrieval over `tools_index`** (LanceDB BM25 + vector + RRF, K=10) | The retrieval infrastructure is already built — M1 ships LanceDB + fastembed + FTS. Pointing a second table at the same machinery is a small Δ, not a new system; same pattern bronze-scribing already proves out and that Retrieve will use over Smalt pages in M4. Scales naturally from 5 tools to 500 with no code change; agents stay decoupled from the registry. Goose's Tool Router (preview, Databricks-only) was the precedent we studied; we built our own using infrastructure we already own. |
| **Ingest builds a Smalt-wide glossary** as a cross-cutting concern | A coherent vocabulary makes the corpus self-describing; cross-source corroboration is value-add for definitions; glossary growth is monotonic in Phase 1 with Phase-2 Curate handling pruning. |
| **Glossary entries are `ConceptPage` rows with `glossary: true`** (not a new page type) | A glossary term IS a concept — one-line schema addition, no new page-type plumbing. The flag distinguishes short-definition entries from richer concepts (parents, claims). |
| **Ingest sub-agents are pipelined** (separate `summarizer`, `entity_extractor`, `glossary_extractor`, `link_resolver`), not a single combined LLM call per file | Cleaner prompts, easier independent evaluation, easier to swap or skip a single extractor. ~3-4× LLM calls per file is mitigated by using a small/fast model for the focused extractors and content-hash-keyed file-level caching. |
| **Hybrid section-page layout**: single-file source → `pages/sources/<source-id>.md`; multi-file source → `pages/sources/<source-id>/index.md` + `pages/sources/<source-id>/<file>.md` per section | Mirrors the actual source shape; no spurious directories for single files; nested layout for directories is browseable as-is in VS Code/Obsidian. |
| **New `IndexPage` page type** for auto-generated indices (glossary first; entity-index, source-index later) | Distinct semantics from human/LLM-authored pages. Indexer regenerates `IndexPage` bodies from a stored query each run; non-index pages are never auto-rewritten. |
| **Entity scope at M3: people, organizations, products, repositories, packages** — not functions/classes/files-as-entities | Avoids flooding entity space with low-standalone-value rows. Code symbols still appear in section-page bodies (symbol outline). Promotion to entity-page can come later via Cogitate flagging cross-cutting importance. |
| **Re-ingest is "new files only" in Phase 1**; the source page IS regenerated each run; entity / concept updates are additive | Matches the M3 spec. Keeps ingest idempotent (content-hash check at the indexer makes no-change re-runs a no-op). SHA-based file-content change detection on already-ingested files is post-Phase-1. |
| **`format_classifier` runs `.h` disambiguation once per directory ingest** (not per file) | Cheap, predictable, auditable. Sets a per-ingest `h_lang ∈ {"c","cpp"}` for the run. Overridable via `--lang-h=c|cpp`. Avoids re-deciding per-file and risking divergent treatment within a single source. |
| **Release CI verifies tag matches `pyproject.toml` version before publishing** — standing pattern across every Python/uv project in ParkviewLab (CoGrind, smalt-mcp, deco-assaying, future MCP servers) | A tag-driven release (`git tag v0.1.5 && git push --tags` → CI publishes) can silently disagree with the package's declared version if `pyproject.toml` wasn't bumped before tagging. The wheel goes out as `0.1.5` by tag intent but its metadata says `0.1.4`. Inconsistent state, hard to notice, hard to recover from cleanly. The CI step strips the leading `v` from the tag, reads the version from `pyproject.toml` via `tomllib`, and fails the build on mismatch. Pattern lifted from deco-assaying's `.github/workflows/release.yml`; copied into smalt-mcp's release workflow at M2.7 and into CoGrind's release workflow when CoGrind starts publishing. |

---

## Open questions (resolve early in implementation)

1. ~~**Embedding model**~~ — *resolved.* Default is **fastembed + `BAAI/bge-small-en-v1.5`** (local, no API keys). Hosted alternatives (Voyage, OpenAI) supported via config when higher quality is wanted.
2. **Surfaces in Phase 1** — CLI + MCP server are committed. Daemon mode (for background Curate / Cogitate runs in Phase 2) — design CLI to make that path easy without committing to it now.
3. **Storage location** — `~/.src/cobalt_grinding/smalt/` default? Configurable? Multi-Smalt support? Phase 1: single Smalt, configurable path.
4. **Embedding cost control** — re-embed on every content change? Cache by content hash. Default: yes, hash-keyed.
5. **Concurrency** — single-process in Phase 1; daemon + worker model arrives in Phase 2 with Curate.
6. **Test corpus** — need a small, diverse seed (10–20 sources covering each format) for development and regression testing. Build alongside M0.
7. **Phase 1 input surface** — CLI is committed; MCP `wiki.ingest` is committed; *additional* ergonomic input modes (watched-folder inbox, OS context menu, browser extension) are TBD — to be discussed before M3.

---

## Repo / file layout

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

---

## Verification

Each milestone has an end-to-end smoke test runnable from the CLI (and, where applicable, from an MCP client):

| Milestone | Verification |
|---|---|
| M0 | `cobalt-grinding` starts with all three MCP children (`smalt-mcp`, `ebony-enriching`, `deco-assaying`) autostarted; `smalt.bootstrap()` materializes the canonical SMALT_DIR layout (pages/, schema/SCHEMA.md, schema/POLICY.md, index/lance/ tables); `ebony.bootstrap()` materializes the canonical EBONY_ENRICHING_DIR layout (proposals/{schema,cogitate,curate,research,toolsmith,converse}/, experiments/, gaps.md, schema/SCHEMA.md+POLICY.md); `cogrind-workshop --status` reports both substrates exist; the daemon binary itself accepts an MCP client connection (no `wiki.*` tools yet). |
| M1 | Hand-write 3 pages, run `cogrind-workshop --index`, confirm LanceDB tables populated with rows, embeddings, FTS index, HNSW index; modify one page, re-index, confirm only the changed page reprocesses. CLI runs synchronously (no daemon yet). |
| M2 | `cobalt-grinding` starts cleanly, auto-initializes an empty Smalt dir (replacing the old `cogrind init`). `cogrind-workshop --status` from a separate shell connects via HTTP MCP and reports daemon state. `cogrind-workshop --index` runs M1's indexer as a daemon task; second invocation visibly faster (warm embedder + DB). `wiki.task_status` reports live progress; `wiki.task_cancel` interrupts cleanly. `cogrind-workshop --status` with no daemon running fails with a clear error pointing at `cobalt-grinding`. Multiple concurrent MCP requests interleave (long-running task + several quick status calls). Single-writer mutex serializes two concurrent corpus-write attempts. Claude Desktop can connect and call `wiki.status` / `wiki.index`. |
| M2.5 | `cobalt-grinding` autostarts the in-tree stub MCP child (`tests/fixtures/stub_mcp_server.py`); `tools_index` is populated at startup with the stub's tools (BM25 + embedding indexes built). `await app.host.run_agent(system="...", messages=[{"role":"user","content":"Greet Gary."}])` runs an end-to-end LLM tool-use loop using the stub child; the host picks tools via hybrid retrieval; the LLM emits `tool_use(stub.greet, ...)`; final assistant content reflects the stub's response. A fixture child whose handshake exceeds `startup_timeout` doesn't pin daemon startup; the supervisor keeps retrying it. Exceeding `call_timeout` mid-call returns a structured `tool_result(is_error=true)` to the LLM and does **not** restart the child. Killing the stub child mid-loop triggers a clean restart (capped exponential backoff); `tools_index` re-populates without restarting `cobalt-grinding`; the next call succeeds. SIGTERM to `cobalt-grinding` shuts every child cleanly — no orphan processes. End-to-end against the real `deco-assaying` is exercised in M3 once that project ships. |
| M3 | `cogrind-workshop --ingest tests/fixtures/sample_sources/sample.md` (against a running `cobalt-grinding`) → source page + entity pages + at least one glossary `ConceptPage` exist; `pages/glossary.md` exists as an `IndexPage` and is regenerated on subsequent ingests; code section pages include a deterministic symbol outline; a `.h` file's parse language follows the heuristic and the `--lang-h` override. Re-ingesting with a new file added produces only the new section page and grows the source page's `sections:` list. Same operations succeed via the `wiki.ingest` MCP tool from any client. |
| M4 | `cogrind-workshop --query "topic"` returns ranked pages; query for a known-absent topic returns a gap signal. Same retrieval available via the `wiki.search` MCP tool. |
| M5 | `cogrind-workshop --ask "what does X say about Y"` → cited answer; `citation_checker` validates citations point to supporting text. Same conversation works from Claude Desktop / Claude Code via `wiki.ask`. |
| M6 | A gap signal queued via `ebony.add_gap` (or an explicit `cogrind-workshop --research "<topic>"` / `wiki.research`) produces a `ProposalPage` in ebony-enriching's `proposals/research/` (verified via `ebony.list_proposals(system=research)`) with `proposal_kind: source_adoption`, observation, hypothesis, prediction, ranked candidate list, and (where cheap-tier) a test result captured via `ebony.write_experiment`; lifecycle status starts `proposed` or `validated` based on test outcome. Accepting a proposal triggers cobalt-grinding's cross-substrate orchestration: `smalt.write_page` (source page) + `ebony.update_proposal_status(applied)` + `ebony.remove_gap` + the apply-time post-mortem (`ebony.write_experiment(input={kind:post_mortem}, ...)`; on a medium/expensive-tier proposal also extending `pages/concepts/research-methodology.md` via `smalt.add_claim` if the lesson generalizes). No auto-ingestion. |
| M7 | `cogrind-workshop --cogitate` (or `wiki.cogitate`) walks the link graph (via `smalt.list_pages` + `smalt.traverse`) and writes `ProposalPage`s with proposal kinds (`wiki_edge`, `concept_merge`, `novel_concept`, `schema_addition`, `contradiction`) via `ebony.write_proposal` — landing in ebony-enriching's `proposals/cogitate/` (or `proposals/schema/` for schema kinds). Cheap-tier proposals are auto-tested with results recorded via `ebony.write_experiment` under `experiments/<proposal-id>/`. SME sub-agents (`observer`, `hypothesis_generator`, `predictor`, `experimenter`, `validator`) drive the lifecycle. No Smalt page modifications (apply step is cobalt-grinding's cross-substrate orchestration on user accept). Apply orchestration runs the post-mortem: every applied proposal produces an `ebony.write_experiment(input={kind:post_mortem}, ...)` record; medium/expensive-tier proposals additionally extend `pages/concepts/cogitate-methodology.md` (or a kind-specific pattern page) via `smalt.add_claim` when the lessons generalize. |
| M8 | `cogrind-workshop --curate` (or `wiki.curate`) walks the Smalt corpus (via `smalt.list_pages` + `smalt.read_page` + `smalt.incoming_links`) and writes `ProposalPage`s (orphans, duplicates, broken links, stale pages, schema-drift) via `ebony.write_proposal` into ebony-enriching's `proposals/curate/`. Each finding carries observation, hypothesis, and (for cheap-tier) a test result captured via `ebony.write_experiment`. No Smalt deletions or modifications until user accepts a proposal (then cobalt-grinding orchestrates `smalt.*` + `ebony.update_proposal_status` + apply-time post-mortem, lessons going to `pages/concepts/curate-methodology.md` via `smalt.add_claim` for medium/expensive-tier corrections). |

**Per-milestone regressions:** every previous milestone's smoke test must still pass.

**Final Phase 1 acceptance:** with a single `cobalt-grinding` running, ingest a curated mixed-format directory (markdown notes, plain text, PDFs, RTF, JSON / TOML configs, Python / C / C++ source files) plus a small local git repo and a small Obsidian vault — using only the file types M3's first impl supports — then have a 5-minute conversation with Converse via `cogrind-workshop --chat` *and* via Claude Desktop over MCP, both pointed at the same daemon. The conversation must demonstrate correct retrieval, correct citation, gap detection on out-of-corpus questions, and no hallucinated sources. The daemon stays warm throughout: ingestion of additional sources mid-conversation reuses the loaded fastembed model and LanceDB connection, and concurrent ingest + query + status calls interleave correctly. Glossary terms accumulate across the mixed-format ingest; the `pages/glossary.md` `IndexPage` is regenerated on every indexer run; ingest sub-agents reach every external capability through the M2.5 host (`await app.host.run_agent(...)`), with `deco-assaying` (separate project) autostarted as an MCP child and any other MCP children configured in `[mcp.clients.*]` discoverable via the host's `tools_index` retrieval.
