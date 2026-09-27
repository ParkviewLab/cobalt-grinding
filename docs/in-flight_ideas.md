# In-flight ideas

Scratchpad for ideas under consideration: questions, not commitments (see the handbook's `documentation.md`). Don't act on an entry silently. What the code does now is described in [`architecture.md`](architecture.md), and the decisions already taken about these ideas, with their reasons, are in [`decisions.md`](decisions.md).

## What triggers the move from Phase 1 to Phase 2?

Phase 1 (Ingest, Retrieve and Converse) is what the daemon does now; Research, Cogitate and Curate make up Phase 2, and Toolsmith Phase 3 (the decisions of 2026-05-02 and 2026-05-04). What marks Phase 1 as done enough to start Phase 2: a list of milestones, a threshold of use of the daemon, or the maintainer's own judgement? An explicit trigger would let the README state a current condition rather than an open-ended "next."

## When does Research (M6) start reading the knowledge-gap queue?

`wiki.find_gaps` and `wiki.report_gap` already read and fill a queue of knowledge gaps in the lab notebook, and nothing but a client reads it. Research (M6), the consumer the design gives that queue, belongs to Phase 2. Is the queue meant to accumulate unread until Phase 2 begins, or is there value in a lighter, earlier consumer, even a manual review of the queue, before M6?

## How should Research (M6) work?

The design sketched for its first implementation, to be settled before the work starts. Research reads gaps from the lab notebook (`ebony.list_gaps`) and takes explicit requests (a `wiki.research` tool, and a `--research "<topic>"` flag in cogrind-workshop), each request entering the queue through `ebony.add_gap` first. For each gap it searches a small set of source backends within a bounded budget (web search, GitHub's repository search, possibly arXiv), judges the candidates for relevance, authority, recency and accessibility, and writes a proposal of the kind `source_adoption`, with a ranked list of candidates and its reasoning, to the notebook's `proposals/research/`. Each proposal is framed as a hypothesis: the observation is the gap, the hypothesis is "ingest candidate X", and the prediction is that queries hitting the gap will then return results. Where a candidate has an accessible summary or abstract, a cheap test runs a dry retrieval against the would-be summary; otherwise the proposal is untestable and a person is the test. It proposes only (decided 2026-05-02): when a person accepts, cobalt-grinding ingests the source, marks the proposal applied, removes the gap, and runs the apply-time post-mortem, extending a `research-methodology` page where the lessons generalise.

Its value is that the corpus's growth becomes a flywheel. Today every source is pointed at by hand. With Research, growth can be reactive (a question leaves a gap, Research proposes a source, a person accepts it, Ingest processes it, and the next answer is better) or proactive (a request to "build me up on <topic>" seeds proposals before any question is asked). Proposed configuration: `[research] search_budget` and `enabled_source_backends`. An `--auto` mode could follow once Research's judgement has proved trustworthy for a domain; the design conversation of 2026-05-02 left adding sources automatically to a later version, if ever ([`architecture-why.md`](architecture-why.md)).

## How should Cogitate (M7) work?

Cogitate is the constructive counterpart to Curate: it looks at the whole graph and proposes new structure, and it never modifies a page. Its sub-agents follow the scientific method. An observer walks the link graph and the entity and concept pages for patterns and anomalies: densely connected entities without a parent concept, entities that share many incoming links with unrelated ones, near-duplicate names, claims about one entity that disagree, recurring frontmatter keys that `SCHEMA.md` does not declare. A hypothesis generator proposes a structural change that explains the observation (an edge with a label, a concept, a schema field). A predictor pre-registers a measurable prediction ("after adding edge X, the recall of query Q rises from R1 to R2"). An experimenter runs a cheap test where the cost tier allows (a schema dry-run, a query benchmark before and after on a cached corpus, a re-link of the corpus with the proposed edge). A validator decides whether the prediction held. These sub-agents would be defined as documents with declared toolkits once that machinery exists, and until then as functions calling the host.

Proposals of the kinds `wiki_edge`, `concept_merge`, `novel_concept`, `schema_addition` and `contradiction` go to the notebook, the schema kinds to `proposals/schema/` and the rest to `proposals/cogitate/`; the cheap ones are tested and arrive validated. Cogitate runs on demand (a `wiki.cogitate` tool, and a flag in cogrind-workshop), with a scheduled mode later. On apply, cobalt-grinding writes the change and runs the post-mortem, extending a `cogitate-methodology` page. Taxonomy building, patterns across several hops and reconciling narratives across sources are for later versions. Cogitate benefits from a fuller corpus, so its first bar is sane, well-tested proposals on a small one. Two points from the design conversation of 2026-05-02: finding contradictions is among the most valuable things the system can do, but only if it captures the axis of the disagreement and not merely the fact of it; and a background system tracks what changed since its last run rather than analysing every page again. Proposed configuration: a `[cogitate]` block for how often it proposes.

## How should Curate (M8) work?

Curate is the critical counterpart: it audits the Smalt and flags problems as proposals, and it never deletes or modifies a page. Its first audits: orphans (pages with no incoming links, found through `smalt.incoming_links`, and no recent access); duplicates (entity pages with very similar names or aliases, other than deliberate distinctions between domains); broken links (to pages that do not exist); staleness (sources fetched longer ago than a threshold); and schema drift (frontmatter keys that `SCHEMA.md` does not mention, required fields missing). Curate flags drift while Cogitate proposes schema additions, which keeps the constructive and critical roles apart. A further audit watches the rate of untestable proposals across all systems as a sign of slipping discipline.

Its findings are proposals of the ordinary shape, most of them cheap and so validated before review, written to `proposals/curate/`. On apply, a merge of duplicates for instance, cobalt-grinding makes the change in the Smalt and runs the post-mortem, extending a `curate-methodology` page. Later audits: extraordinary claims (a claim that contradicts what the Smalt already holds needs more sources before it spreads), unvisited pages, low-confidence assignments of domains, and fields whose tests fail as evidence accumulates, the candidates for removal from the schema. Detecting extraordinary claims needs a prior over what the Smalt already holds; the design conversation of 2026-05-02 proposed starting simply (numeric outliers, claims from a single source held with high confidence, strong qualifiers in the language) and going further only if that did not suffice. It also noted that a contradiction between code and prose ("the README says X, the code does Y") is a good indicator of a bug or a stale document. Curate is also the system the code relies on to remove the duplicate entity and glossary pages and the orphaned index pages that ingestion now leaves (the decisions of 2026-05-17 and 2026-05-18). Proposed configuration: a `[curate]` block for thresholds of staleness and the ageing of orphans.

## How should proposals be shaped, tested and applied?

The design shared by every system that proposes (decided on 2026-05-05 and 2026-05-16, and built by none yet). A proposal lives in the lab notebook, at `EBONY_ENRICHING_DIR/proposals/<system>/<id>.md`, and is read and written with the notebook's tools (`ebony.write_proposal`, `list_proposals`, `read_proposal`, `update_proposal_status`, `supersede_proposal`). Its frontmatter:

```yaml
type: proposal
proposal_kind: schema_addition | schema_drift | schema_removal | wiki_edge | concept_merge
             | source_adoption | tool_adoption | tool_specification
             | toolkit_addition | toolkit_removal | novel_synthesis | ...
status: proposed | under_test | validated | rejected | applied | superseded
proposed_by: <system>
proposed_at: <ISO timestamp>
test_status: untested | passed | failed | untestable
test_cost: trivial | cheap | medium | expensive
related_pages: [<page ids>]
supersedes: <proposal id> | null
superseded_by: <proposal id> | null
```

Its body has five sections, in order: the observation (the evidence that triggered it, with pointers to pages, traces, counts or gaps, and no hypothesis yet), the hypothesis (the change in specific terms), the prediction (what should change measurably, numeric where possible, registered before the test), the test (its design and result, or why it is deferred or untestable), and the reasoning that connects the observation to the hypothesis.

The cost tier decides whether the system tests a proposal before a person reviews it:

| Tier | Examples | Tested automatically |
|---|---|---|
| trivial | a typo, a rename for clarity, alphabetising a list | no test; approved and applied |
| cheap | a schema dry-run against existing pages, a check of vocabulary at read time, a query benchmark before and after on a cached corpus | yes |
| medium | a policy replayed over past agent traces, the corpus re-linked with a proposed edge | when the compute budget allows; otherwise pending |
| expensive | an agent re-run on held-out inputs, a candidate MCP server in a sandbox, a full re-ingest with a new toolkit | only on request; otherwise the person is the test |

The lifecycle: a proposed change becomes validated when a cheap test passes and rejected when one fails; an untestable proposal goes to a person, who applies or rejects it; a person applies or rejects a validated proposal; any proposal can be superseded by a later one; and an applied proposal returns to proposed when later evidence contradicts it, which is how a schema field comes to be removed. A proposal whose prediction cannot be made measurable is marked untestable with a reason; untestable is not a free pass, since Curate audits its rate, and a person's own edits to `SCHEMA.md` and `POLICY.md` meet the same bar. Test runs leave records at `experiments/<proposal-id>/<run-timestamp>.md` (`ebony.write_experiment`). Applying is a sequence of calls that cobalt-grinding makes (write the Smalt page, mark the proposal applied, remove the gap it answered), since neither substrate has an apply tool. The schema and policy documents of both substrates are groomed through the same loop.

| System | Typical kinds | Typical test | Directory under `proposals/` |
|---|---|---|---|
| Cogitate (M7) | `schema_addition`, `wiki_edge`, `concept_merge`, `novel_concept` | schema dry-run; query benchmark; corpus re-link | `schema/` for schema kinds, else `cogitate/` |
| Curate (M8) | `schema_drift`, `schema_removal`, `orphan`, `duplicate`, `staleness` | drift counts, link reachability, age thresholds | `curate/` |
| Research (M6) | `source_adoption` | dry retrieval against the candidate's would-be summary; coverage of the gap | `research/` |
| Toolsmith (M9+) | `tool_adoption`, `tool_specification`, `toolkit_addition`, `toolkit_removal` | sandboxed replay of agent traces; rate of tool errors before and after | `toolsmith/` |
| Converse's novelty detector | `novel_synthesis` | citations re-checked on the proposed synthesis page | `converse/` |

## Should every applied proposal get a post-mortem?

Decided in outline on 2026-05-16. When a validated proposal is applied, cobalt-grinding would, after the substantive write and before the proposal's status is final: read the proposal's whole lifecycle (its frontmatter and body, every experiment record, the chain of proposals it superseded, and any notes from its review); read the related rejected proposals (siblings from the same observation, near-duplicates that took another angle), to see the whole space of decisions and not only the winning path; run a synthesis sub-agent, `post_mortem`, that produces a summary of one to three paragraphs and a structured block (the system, the kind, the pattern, the number of false starts, the time to validation); judge whether the lessons generalise (a novel pattern, a longer path than usual, success after notable false starts, a failure mode of the system illuminated, a class of proposals made cheaper); if they do, extend a methodology page (`pages/concepts/cogitate-methodology.md` and its like) or a pattern page with `smalt.add_claim`, or, for a broad lesson, propose a synthesis page through the ordinary loop; and in every case write the post-mortem as an experiment record (`input: {kind: post_mortem}`), so that Curate can later see whether anything is being learned. The cost follows the tier: trivial proposals skip it, cheap ones get a summary from a small model only, and medium and expensive ones get the whole pipeline; a `[post_mortem]` block would move the threshold or switch the step off for very small budgets. Once methodology pages exist, Curate could audit them as well, for lessons that contradict one another and pages that the owning system's prompts never read.

## How should the systems feed each other?

Two fan-ins of the same shape (search, judge, propose; a person approves; a system processes), on different substrates. Requests for research, about knowledge: Retrieve, when the top scores for a query are weak ("find sources about a topic the Smalt does not cover"); Converse, when it notices while answering that context is missing; Ingest, when a new source cites a work the Smalt lacks; Curate, when it flags stale sources, broken external links or orphans ("freshen, replace or supplement"); and Cogitate, when a concept area is thinly supported or a contradiction needs more evidence. All would add gaps to the notebook's queue with `ebony.add_gap`.

Tool gaps, about capabilities, for Phase 3: Ingest meets a file type that no tool handles; Retrieve wants a structural query that no backend answers; Converse needs a domain capability in the middle of an answer; Curate cannot perform a class of audit; Cogitate wants to test a hypothesis for which no tool gives the data; Research cannot search a surface (a paywalled database, a niche forum) for want of an adapter. These would go to the same queue at first, and to a separate `tool_gaps.md` if their volume warranted it.

Today nothing in the daemon reports a gap: `wiki.search` and `wiki.ask` flag one in their results, and only a client's call to `wiki.report_gap` records it (decided on 2026-05-18). Converse's novelty detector would flag an answer that is a new synthesis across sources and propose a synthesis page for Cogitate to consider, since otherwise the same answer is derived again for every question.

## How should Toolsmith (M9+) work?

Decided on 2026-05-04 as the seventh system, in Phase 3. It would read tool-gap entries from the notebook, search for existing MCP servers (registries, GitHub, npm, PyPI), judge their fit, maturity and licence, and write one of two kinds of proposal to `proposals/toolsmith/`. Adopt: use this existing server, with its tools, licence and maturity stated; on approval it joins `[mcp.clients]`, the daemon starts it, the tools index picks it up, and Cogitate proposes adding its tools to the relevant agents' toolkits. Specify: no server fits, and here are the requirements of one to be built, the deco-assaying pattern made routine; once that server ships, an adopt proposal follows. Toolsmith composes Research's search and judgement, Curate's audit of agents' toolkits for unused tools, and Cogitate's proposals of tools an agent was seen to need without declaring them. It proposes only. It needs Research, Curate and Cogitate as building blocks, and enough agents running for the signal about their use of tools to be real; until then people and Claude play its part, finding an existing server or starting a new project when a capability is missing, and the candidates weighed so far are in the next entry.

## Which MCP servers should CoGrind adopt or build?

The notebook [`mcp_servers_to_consider_ideas.md`](mcp_servers_to_consider_ideas.md) holds the candidates weighed so far and their open questions: an introspection server for the Anthropic API, planned in a brief and not built (the model's capabilities, its context budget, the state of the rate limits, token counts), with its dependencies chosen against a supply-chain threat; the llama.cpp side of the same knowledge; and the servers set aside, with their state as found on 2026-09-27.

## A system that judges the veracity and quality of sources?

A dedicated system (working names Vet, Appraise, Weigh) would rate each source for veracity and quality (the authority of its author or publisher, its recency, the strength of its evidence, whether it was peer-reviewed, the density of its citations, the prior reliability of its domain) and write the scores to the source page's frontmatter. Research would prefer better candidates, Cogitate would weigh conflicting claims by the quality of their sources, and Curate would flag pages whose claims rest on weak sources. It belongs with Toolsmith to Phase 3 (Toolsmith judges capabilities, this system knowledge), after enough corpus exists for the prior reliability of a domain to mean something. smalt-mcp 1.3.3's page schema already has `quality_score`, `veracity_score`, `evaluated_at` and `evaluation_notes`, so no migration of the schema is needed; the daemon does not write them.

## Should agents be defined as documents with declared toolkits?

The host chooses tools by retrieval against the latest message only. The seam designed on 2026-05-03: agents defined as Markdown documents (a role, a domain, the policy they work under, a prompt and a declared minimum toolkit), with the host merging the declared toolkit and the tools retrieval finds; the toolkits then groomed over time by Cogitate (adding tools an agent was seen to need), Toolsmith (new tools where nothing in the inventory fits) and Curate (declared tools never used), so that defining an agent is writing a document, not code. The smaller seams on the way: a `must_include` pin for a tool an agent needs whatever its rank; a router meta-tool that the model can call to find tools in the middle of a conversation, like Goose's `find_tools`, if ten tools prove too few; and a permission filter for each agent, a denylist applied to the index before retrieval. Meanwhile no tool list specific to an agent is to be hard-coded, to keep that path clear.

## Which subsystems should use the host's `run_agent`?

The host's `run_agent` exists and no subsystem uses it: ingest's extraction steps and question answering call the provider directly, and deco-assaying and flint-slating are called directly (the decisions of 2026-05-17 and 2026-05-18). The agentic versions deferred then: Converse driving `wiki.search`, `get_page` and `traverse` itself through tool use, and an agent that decides whether to parse a file at all. Before a subsystem relies on `run_agent`, its dispatch has to reach the default children (see [the agent runtime](architecture.md#the-agent-runtime)). Other work on the host that was proposed: the loop's progress events carried into the status of a task; an endpoint in the shape of `/v1/messages` for external agents; providers other than Anthropic, the provider interface being the seam; and the hosted embedding providers the configuration already accepts (Voyage, OpenAI).

## Which formats should ingest read next?

The daemon reads text, Markdown, RTF as text, JSON, JSON5, TOML, Python, C and C++, and PDFs through flint-slating. The plan proposed more, each to come as an MCP child rather than as code in the daemon: HTML (stripped, keeping the heading hierarchy); YAML and XML configuration (parsed, then its structure summarised, lockfiles and manifests especially); `.docx`, and RTF read properly, through pandoc; `.env` files, keys only, their values redacted before anything is written; OCR for scanned PDFs, flagged `confidence: low`; Docling's Markdown for PDFs, which keeps headings, tables and the reading order (flint-slating's `pdf_read_markdown`, with its asynchronous jobs for longer documents); and a GitHub repository's commits, pull requests and issues as further sources, with private repositories cloned through `gh`. Later: `.epub`, `.org`, `.rst`, `.ipynb`, CSV and TSV as tabular data, images and other multimodal sources (whiteboards, infographics, charts), archives and audio. An `[ingest]` block could hold the supported types and a cap on the files of one source.

## How should ingesting a source again update its pages?

Today ([Ingesting again](architecture.md#ingesting-again)) an unchanged file is skipped, a changed file gets a new source page beside the old one, every ingest of a directory writes a whole new set of pages, and entity and glossary pages are never merged. The plan's intent (2026-05-03): only new files for a directory; the source page regenerated in place; entity and concept pages updated additively, with new back-links and evidence, rather than duplicated; nothing changed when nothing changed; and, after Phase 1, detection of changed content file by file. Open: rewriting a source page in place (`update` with its existing identifier), skipping unchanged sections, and resolving entities and terms against existing pages (the next entry).

## How should ingest resolve entities, terms and domains against the Smalt?

The link resolver the plan designed: resolve each extracted entity or concept against existing pages (exactly, then fuzzily, then with a model as tie-breaker), propose merges, and append evidence to an existing glossary term instead of writing a new page. The assignment of domains decided on 2026-05-05: the ingestion agent sets `domains:` from the source's own domains, the surrounding text and the term itself, tags several domains freely when the context is mixed, marks an uncertain assignment `domain_confidence: low`, proposes a new domain page when a source clearly belongs to a domain not yet in the Smalt, and tells colliding slugs apart by meaning (`tree-data-structure`, `tree-plant`). Obsidian's `[[wikilinks]]` could be kept as edges beside CoGrind's own links.

## Should an ingest be all or nothing?

Transactional ingestion was a principle of the design conversation and a rule of the plan: accumulate the proposed edits of pages in memory, validate them against the schema, then write every touched page at once, so that a failure writes nothing. A source can touch ten to fifteen pages, and edits half applied across ten pages are much worse than a half-applied edit to one. Today each page is written as it is produced, and a failure leaves the pages already written ([Failure](architecture.md#failure)).

## Should claims carry a confidence and a provenance for each value?

Decided on 2026-05-02 and not built: a claim such as `revenue_q3_2024 = 4.2M` would carry its source pointer, units, date and confidence, not only the page it sits on, and each source would yield its hard data values with them. Two tiers of confidence (`source_type: chart_estimate` and `source_type: tabular`) would let the system reconcile the same value found in a chart and in a table. Pointers would take the form `source@version:path#fragment` (`smith2024.pdf#3.2.1`, `apps-repo@abc123:src/auth/session.ts:L42-78`). smalt-mcp offers claims (`add_claim`); the daemon writes none.

## Should ingest keep a source's structure?

The provenance decided on 2026-05-02: capture a document's table of contents and heading hierarchy, and a directory's tree, in the source page's frontmatter, or in a sidecar file (`structures/<source-id>.json`) when it is large; for a repository, its import graph as well as its directory tree, since neither subsumes the other. Today only a directory's list of files and its git or Obsidian metadata are kept.

## Should ingest be cheaper, faster, and whole on long sources?

The plan's measures: a small, fast model for the focused extraction steps (a `glossary_extractor_model` key), the larger model being kept for summaries; caching of the model's results keyed by content hash; processing the files of a directory in parallel, left on 2026-05-18 as a later optimisation with a gain of four to eight times expected; and chunking, since each extraction call now sees only a file's first 30,000 characters, deco-assaying's chunks being one way to do it for code (next entry). For `.h` files, the heuristic decided on 2026-05-03 (C++ when sibling files are C++ or a header uses C++ constructs, decided once for each directory) would replace the caller's `h_lang`.

## Should ingest use more of deco-assaying's analysis?

The daemon sends each Python, C or C++ file to deco-assaying's `analyze_file` without chunks and renders only the symbols' kinds, names and first lines, and the imports (the decision of 2026-05-18). The investigation of deco-assaying's output on 2026-05-04 found much more in each answer, and proposed using it:

- For a directory, one `index_repo` job (whose status is polled), then `get_file_analysis` for each file, which reuses the stored analysis rather than parsing again; the repository-wide results (a manifest with entry points and counts of test, configuration and generated files, the languages, a symbol index, the parse errors) would enrich the source-index page. A single file would keep `analyze_file`.
- Richer section pages: the module's docstring (`module_doc`) verbatim; each symbol's `signature` and `doc` rather than its name alone; the calls and inheritance in `references`, as a list or as links between pages, making the call graph part of the Smalt's graph; the per-file `metrics` in one line; and the `literals_of_interest` (URLs, paths, environment variables, SQL, routes) as signals of provenance.
- The syntax-aware `chunks`, each attributed to its enclosing symbol, given to the summariser in place of the first 30,000 characters (the investigation's own preference), whether or not their text is then kept, which the decision of 2026-05-04 permits.
- The `is_generated`, `is_test` and `is_config` flags recorded in a section page's frontmatter as signals, never used to leave a file out (decided on 2026-05-04).
- The parse status taken from the answer's `parse` object (`ok`, `error_nodes`, `missing_nodes`, and `reason` when no parser exists), which the renderer does not read now (see [Extraction](architecture.md#extraction)), so that a partially parsed file is marked as such.
- Languages beyond Python, C and C++: when investigated, deco-assaying analysed thirteen languages fully (Bash, C, C++, C#, Go, Java, JavaScript, PHP, Python, Ruby, Rust, TSX, TypeScript) and parsed thirty-two more into a generic fallback shape.

## Should ingest run as a background task?

`wiki.ingest` blocks until the ingest is done (decided on 2026-05-17, with background tasks left for when ingesting many files made the wait noticeable). The design it deferred: the call returns a task id at once; the scheduler runs the ingest on its pool of workers; progress reaches `wiki.task_status`; only the final commit of pages is serialised, through the corpus mutex; and a clone reports its progress through MCP's progress notifications. The scheduler and the mutex exist and are unused. Related, and left open by the plan: other ways of feeding sources, such as a watched inbox folder, a context menu of the operating system, or a browser extension.

## Should retrieval classify, re-rank and cache?

The plan's pipeline beyond what search does now: a classifier of queries (entity, concept, code, synthesis across sources); re-ranking, or ranking folded into the answering model; caching of `(query, candidate set) → ranked order`; a separate path for code, BM25 over identifiers with filters on the syntax tree; and a `[retrieve]` block (`gap_score_threshold`, `cache_size`, the weights of full-text and vector search in the fusion). An automatic gap signal on weak scores was set aside on 2026-05-18.

## Should Converse do more?

The plan's sub-agents beyond today's single call: a classifier of intent (a question, a task, an exploration); a citation checker that confirms that a cited page supports the claim, not only that the page was given; the novelty detector (in "How should the systems feed each other?"); and use of the expanded neighbours, which `wiki.ask` computes and does not read. Proposed configuration: `[converse] context_window` and `citation_strictness`.

## Which deferred directions come next?

Deferred by the plan and not scheduled: DuckDB as a read-only analytical layer over the Lance files (decided on 2026-05-02 for Phase 2; the files are now smalt-mcp's); concurrent writes by several users; a Claude Code skill or plugin aware of CoGrind, distinct from the MCP server; ingesting inline content that has no stable location; archiving URLs automatically (as archive.org snapshots); ingesting web pages beyond git repositories and PDFs, and crawling the web recursively rather than one URL at a time; pools of subprocesses for parsers prone to crashing; and scheduled runs of Cogitate and Curate.

## How should a seed corpus for tests be built?

Left open by the design conversation of 2026-05-02 and by the plan: a small, diverse seed corpus (10 to 20 sources covering each format), with a tiny seed Smalt and lab notebook, for development and for regression tests. The tests now replace the children with mocks and a stub MCP server.
