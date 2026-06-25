# CoGrind — North Star

> **What this document is.** A living statement of what CoGrind *is* and what it's for. Read this before designing or implementing anything; come back to it when a decision feels off. If a proposed change doesn't fit the north star, that's a signal to stop and reconsider — either the change is wrong or the north star needs to evolve. This document is **deliberately not implementation detail** — those live in `plan.md`. This is the orienting principle.

---

## What CoGrind is

**CoGrind is an MCP server wrapped around an AI brain.**

The brain has two parts:

- **Memory** — split across **two storage substrates**, both served by separate MCP child servers `cobalt-grinding` supervises:
  - **The Smalt** (canonical knowledge), via [`smalt-mcp`](https://github.com/ParkviewLab/smalt-mcp): a markdown-canonical wiki of interlinked notes maintained by an LLM, indexed for fast retrieval (FTS + vector + alias), traceable back to the sources it was built from. CoGrind grinds cobalt to make Smalt; the Smalt is what it *knows*.
  - **The lab notebook** (research-in-flight), via [`ebony-enriching`](https://github.com/ParkviewLab/ebony-enriching): proposals, experiments, gaps — the scientific-method substrate where cognitive systems record hypotheses, predictions, test outcomes, and lifecycle transitions. The notebook is what CoGrind is *thinking about*.
- **Cognitive skills** — agentic systems that *do things* with that memory: ingest new sources into the Smalt, find what's relevant, answer questions about it, propose new structure (writing to the lab notebook), surface inconsistencies (writing to the lab notebook), hunt for what's missing (reading gaps from the lab notebook).

Externally, CoGrind looks like a single MCP server. The cognitive skills are the tools it exposes: `wiki.ingest`, `wiki.search`, `wiki.ask`, `wiki.cogitate`, `wiki.curate`, `wiki.research`. Anyone with an MCP client — Claude Desktop, Claude Code, the `cogrind-workshop` CLI, a future web UI — gets the brain through the same surface. The two underlying substrate MCP servers stay invisible to external clients — they're cobalt-grinding's internal-state plumbing.

Internally, CoGrind is also an MCP host: its cognitive skills run as LLM-driven agents, and those agents reach for *external* tools (storage substrates, parsers, extractors, web search, archive lookups) through child MCP servers CoGrind spawns and supervises. Both storage substrates have **zero outbound dependencies** — neither knows the other exists; cobalt-grinding's cognitive systems orchestrate any cross-substrate write (e.g., apply-a-validated-proposal = `smalt.write_page` + `ebony.update_proposal_status(applied)` in sequence). So CoGrind is **a learning, thinking MCP server that uses other MCP servers — for both its memory and its capabilities — to get smarter.**

That's the whole concept. Everything else is a consequence of it.

---

## What "memory" means here

The memory is **not** an embedding store. Embeddings are a derived index. The memory is **markdown** — human-readable, human-editable files with YAML frontmatter, organized into entity pages, concept pages, source pages, and synthesis pages (in the Smalt); and proposal pages + experiment records + gap entries (in the lab notebook). Both substrates are markdown-canonical: the Smalt is the asset, the LanceDB index is rebuildable from it; the lab notebook is the asset, its filesystem layout is queryable as-is.

Why markdown is the substrate, not a database:

- A human can read and fix what the LLM got wrong without any special UI.
- LLMs natively produce and modify markdown.
- It's git-diffable, syncable, portable.
- Editors that already exist (VS Code, Obsidian, plain `vim`) are the browse UI.

Memory is **earned, not stored**. CoGrind never copies the sources it ingests — it captures their location, content hash at fetch time, structure, and the notes the system made. To re-verify a claim, CoGrind re-fetches the source.

---

## The Smalt documents itself

CoGrind's Smalt holds more than the user's knowledge. It also holds the Smalt's own scaffolding — and that scaffolding lives by the same markdown-canonical, propose-don't-act discipline as everything else:

- **Schema** — page types, frontmatter shape, link-edge vocabulary (`SCHEMA.md` + `frontmatter_schema.py`).
- **Policy** — agent behavior rules (`POLICY.md`).
- **Agent definitions** — the SME agents that run the cognitive skills (role, domain, prompt, declared toolkit), as markdown.
- **Tool inventory** — which MCP servers CoGrind uses (the `existing_MCP_servers_to_consider/` directory and its successors).
- **Vocabulary** — the glossary and the domains it's organized by.

Every layer is groomed by the same loop: **propose, don't act; humans approve; the Smalt applies.** Day 0 — humans + Claude seed every layer ("birthing the 0-day CoGrind"). Day 1+ — the system proposes; humans approve; the Smalt grows up. Where the layer's grooming lives: schema → M7 Cogitate proposes additions, M8 Curate flags drift; agents and toolkits → M2.5 future seam, with Cogitate / Curate / Toolsmith jointly grooming over time; tool inventory → Toolsmith (Phase 3); vocabulary → Ingest writes, Curate prunes.

The Smalt is **recursively self-documenting**: it documents what it knows, *and* it documents how it knows, *and* it documents how it grows.

---

## How the Smalt evolves: hypothesis, test, truth

A self-modifying system needs a discipline for what counts as a good change. CoGrind's discipline is the scientific method, applied recursively to its own structure.

Every self-modification proposal — a new schema field, a new agent toolkit entry, a new edge in the graph, a new source to ingest, a new MCP server to adopt — is written as a **hypothesis with a falsifiable prediction**. Where the system can run a cheap test (a schema dry-run, an agent replay against a fixed trace, a query benchmark before/after), it tests the prediction *before* asking the user. The user reviews **hypothesis + evidence together**, not bare opinion.

The lifecycle is **Observe → Hypothesize → Predict → Test → Validate → Apply**. Where testing is expensive or impossible, the proposal is marked `untestable` (with a reason); the user becomes the test. Falsifiability is required — proposals whose predictions can't be reformulated into something measurable get reframed or rejected. The user's own direct edits to SCHEMA.md / POLICY.md are held to the same bar: keeps both sides honest.

**Truth is provisional.** Accepted findings can be re-tested when new evidence accumulates. SCHEMA.md is not monotonically growing — schema *removal* becomes possible (Curate flags fields whose tests fail under accumulated evidence). The Smalt carries the **experimental record** so re-evaluation is always grounded in what was actually observed. This makes science's discipline native to the system, not bolted on.

**Every successful apply is also a learning opportunity.** When CoGrind's orchestration finishes applying a validated proposal, it runs a **post-mortem** on the proposal's full journey from initiation to validation — what worked, what didn't, what false starts there were, where the system's prior failure modes were illuminated. Where the lessons generalize, they're added to the Smalt as knowledge: extending a methodology ConceptPage for the proposing system (e.g., `pages/concepts/cogitate-methodology.md`) via `smalt.add_claim`, or — for sufficiently broad lessons — proposing a new SynthesisPage via the standard proposal loop (the system doesn't bypass proposal discipline to write Smalt syntheses, even about itself). The post-mortem itself is logged as an experiment in the lab notebook so the system's record of its own learning is queryable later — even when the post-mortem decides nothing was worth keeping. Without this step, the proposal track record would be inert data; with it, each apply updates the system's judgment about how to propose better, and the methodology pages become context for the next run. **This is the "CoGrind learns how to learn better" mechanism — made concrete.**

This shape connects directly to *Things to Remember* item 1 below: the LLM hypothesizes; **code** runs the test. Hypothesis-generation needs judgment; test-execution needs determinism. Don't make an LLM do what code can do — and equally, don't make code do the part that needs judgment.

The implementation seam — proposal document shape, lifecycle states, cost tiers, test mechanics by layer, and the apply-time post-mortem — lives in `plan.md` under *Proposal document shape and lifecycle* and *Apply-time post-mortem: closing the learning loop*.

---

## What "cognitive skills" mean here

Seven agentic systems, named as verbs:

| Skill | What it does |
|---|---|
| **Ingest** | Read a source. Extract summary, entities, concepts, claims, glossary terms. Link them. Write them. Index them. |
| **Retrieve** | Given a query, find the relevant pages — hybrid lexical + semantic, with graph expansion and gap detection. |
| **Converse** | Talk to the human. Bounded by what's been ingested. Cite sources. Refuse to hallucinate. |
| **Cogitate** | Look at the whole graph. Propose new connections, emergent concepts, taxonomies. Surface contradictions. *Phase 2.* |
| **Curate** | Look at the whole graph. Find rot — orphans, duplicates, staleness, drift, extraordinary unsupported claims. *Phase 2. Proposes; never deletes.* |
| **Research** | Given a knowledge-gap signal, find sources to fill it. *Phase 2. Proposes; never auto-ingests.* |
| **Toolsmith** | Given a tool-gap signal, find existing MCP servers that fit, or write a requirements doc for one to be built. *Phase 3. Proposes; never auto-installs. Same engine as Research, applied to capabilities instead of knowledge.* |

Each skill is an LLM-driven agent (or an orchestrator of sub-agents). Each one, when it needs to reach for an external capability — parse a file, extract from a PDF, search the web — does so through CoGrind's MCP host machinery. The agent itself never touches MCP plumbing; it asks the host to run a tool-using turn for it, and the host handles the rest.

Skills compound. Each one writes back into the memory (or proposes back, in the audit-and-grow systems). The Smalt gets denser, more interlinked, more accurate over time. The roster of agents and the toolkits they declare evolves too — Cogitate adds existing-but-undeclared tools to an agent's toolkit; Toolsmith proposes adopting or commissioning new tools when nothing in the inventory fits; Curate flags declared-but-unused tools. **CoGrind learns. CoGrind also learns how to learn better.**

---

## What CoGrind is *not*

Worth being explicit about — these are the drift directions to push back on.

- **Not a chatbot.** Conversing with the Smalt is one of the cognitive skills, not the product.
- **Not a notes app.** It's not a UI for taking notes — humans don't write *into* the memory directly (though they can fix what they find wrong); the agentic systems write, and humans read.
- **Not a search engine.** Search is the substrate Retrieve uses; it's not the user-facing offer.
- **Not a RAG implementation.** The classic RAG pattern — "retrieve raw chunks every query and stuff them into a prompt" — is what CoGrind explicitly isn't. CoGrind builds *interlinked structured notes* the LLM produces and maintains; retrieval works against those notes, not against raw source chunks.
- **Not a pile of plugins.** The MCP children are *capabilities* — parsers, extractors, fetchers — not products. The product is the brain; the capabilities serve it.
- **Not a tool a user installs and forgets.** The Smalt grows because the user feeds it sources and converses with it. The flywheel needs human attention to turn.

---

## The shape that follows from this

One binary in this repo, one CLI in a sibling, one protocol between them:

- **`cobalt-grinding`** — the daemon binary (shipped by this repo). Runs the brain. Hosts the MCP server (out to clients) and the MCP host (in to its own agents over child MCP servers). Bootstraps its substrate children on first run. The only program that touches the Smalt or lab notebook on disk is the corresponding child MCP server it supervises.
- **`cogrind-workshop`** — the human-facing CLI (sibling repo, [ParkviewLab/cogrind-workshop](https://github.com/ParkviewLab/cogrind-workshop)). Pure MCP client. A polished interface for talking to a running cobalt-grinding daemon over the daemon's MCP server. Will grow into a REPL. Shares no business logic with the daemon — they're separate programs that meet at the protocol.
- **MCP** — the only protocol. The CLI uses it. Claude Desktop / Claude Code use it. Any future web UI uses it. CoGrind's own agents reach external capabilities through it.

`cogrind-workshop` is one of many possible MCP clients (Claude Desktop, Claude Code, future web UIs — same surface). Everything flows through MCP.

---

## Names

The vocabulary, kept tight on purpose so the metaphor and the system stay traceable:

| Name | What it is |
|---|---|
| **Cobalt-Grinding** | The project. The act. The metaphor's source — historically, grinding fired cobalt-blue glass produces a fine pigment. |
| **CoGrind** | The styled project name in prose. PascalCase preserves the seam: **Co** is the periodic-table symbol for cobalt, **Grind** is the action. |
| **`cobalt-grinding`** | The daemon binary, shipped by this repo. (Earlier drafts used a separate `cogrindd`/`cogrind` daemon+CLI split; M2.7 collapsed that — the daemon is now `cobalt-grinding` and the CLI moved to a sibling repo.) |
| **`cogrind-workshop`** | The user-facing CLI; sibling repo [ParkviewLab/cogrind-workshop](https://github.com/ParkviewLab/cogrind-workshop). Pure MCP client; one of many that can drive a running `cobalt-grinding`. |
| **The Smalt** | The canonical substrate of frontmattered markdown files (entity pages, concept pages, source pages, synthesis pages, index pages) plus their on-disk organization. The canonical-knowledge half of CoGrind's memory. *Smalt* is the historical name for the pigment that comes out of cobalt-grinding. |
| **The lab notebook** | The research-in-flight substrate — proposals, experiments, gaps. The scientific-method record. Lives in `EBONY_ENRICHING_DIR`, served by `ebony-enriching`. Conceptually the lab notebook a scientist carries; the Smalt is the library. |
| **`smalt-mcp`** | The MCP server dedicated to reading and writing the Smalt — `smalt.read_page`, `smalt.write_page`, `smalt.add_link`, `smalt.add_claim`, `smalt.search`, etc. A separate ParkviewLab repo; runs as an MCP child of `cobalt-grinding`. |
| **`ebony-enriching`** | The MCP server dedicated to reading and writing the lab notebook — `ebony.write_proposal`, `ebony.list_proposals`, `ebony.update_proposal_status`, `ebony.write_experiment`, `ebony.add_gap`, etc. A separate ParkviewLab repo; runs as an MCP child of `cobalt-grinding`. |
| **`wiki.*`** | The user-facing high-level MCP tool namespace — `wiki.ingest`, `wiki.search`, `wiki.ask`, `wiki.cogitate`, `wiki.curate`, `wiki.research`. Served by `cobalt-grinding` itself, not by any storage substrate. The cognitive skills, exposed. |

The naming chain reads end-to-end: *Cobalt-Grinding (the act) is performed by CoGrind (the system); CoGrind ships as the `cobalt-grinding` daemon (this repo) driven by the `cogrind-workshop` CLI (sibling repo); they together produce and groom **the Smalt** (the substrate); the Smalt is read and written through `smalt-mcp` (low-level) and queried via `wiki.*` (high-level).*

---

## Agent or code? — the question to ask before building anything

For everything CoGrind does, ask first: **is this a job for a specialized agent, or for code?**

The two kinds of answer have different costs, different shapes, and very different consequences for the system.

- **Code (an MCP server we spec out and build, in-tree or external).** Choose this when the work is deterministic, mechanical, and benefits from speed, parallelism, or precise structured output. Parsing source code with tree-sitter is a code job. Extracting text from a PDF is a code job. Hashing a file is a code job. The output is a contract — a typed JSON shape another agent or piece of code can consume without judgment. **`deco-assaying` is the canonical example**: CoGrind needs structural information about code — symbols, imports, references, spans — and the right way to get it is to build (or adopt) an MCP server that uses tree-sitter and emits structured JSON. We do *not* build an "agent that reads code files and decides what's in them" because the work has a correct answer and a tree-sitter grammar already encodes it.
- **A specialized agent (a markdown definition of an SME).** Choose this when the work requires judgment, synthesis, naming, or weighing tradeoffs — the kind of thing a human subject-matter expert would do. Deciding whether two entity pages are duplicates is an agent job. Writing a one-paragraph summary of a source is an agent job. Choosing a glossary term's short definition from a snippet is an agent job. Defining a new agent is **not writing code** — it's writing a markdown document that says: this agent's role is X, its domain expertise is Y, the policies and prompts it operates under are Z, and the MCP tools it draws on are these.

The two compose. A specialized agent often *uses* code (via MCP children) to do its work — the code-handler agent in M3 doesn't parse code itself; it reasons about what tree-sitter (via deco-assaying) returned. The agent provides judgment; the code provides structure. Mixing them up — building an agent to do mechanical work, or hard-coding a heuristic for something that needs judgment — is the most common architectural mistake in this space, and the cost shows up later as either bad answers or unmaintainable code.

**Agent definitions are themselves wiki-like artifacts the system grooms over time.** A specialized agent's markdown definition includes its role, its domain, the policies it operates under, its prompt, and the **minimum toolkit** of MCP tools it's expected to use to do its job. That toolkit is initially human-authored; over time it's groomed by three of the seven agentic systems working in concert:

- **Cogitate** proposes adding existing tools the agent has been *observed* to need but didn't declare.
- **Toolsmith** (Phase 3, the 7th system) proposes adding *new* tools — finding existing MCP servers or writing a requirements doc for one to be built — when the agent needs a capability nothing in the inventory provides.
- **Curate** flags declared-but-never-used tools for removal.

So the same self-evolution discipline CoGrind applies to the Smalt — propose, don't act; humans stay in the approval loop until trust is earned — applies to CoGrind's own roster of agents *and* to the capability surface those agents reach for. **CoGrind grooms its own agents, and grooms its own toolset.** Until Toolsmith exists (Phase 3), humans + Claude play its role: when an agentic system hits a capability gap, we either find an existing MCP server or spawn a new project (deco-assaying was the first). The implementation seam for this lives in `plan.md` under M2.5's "Future seams" → *Agent-declared toolkits, system-curated over time*; Toolsmith itself is sketched in M9+.

When you find yourself wanting a new capability, ask:

- *Does this have a correct answer the system can compute?* → code (probably an MCP server).
- *Does this require choosing the best of several reasonable answers?* → agent.
- *Does it need both?* → an agent that calls code via MCP. (This is the common case in CoGrind.)

When the answer is "code, and we don't have it yet," the deliverable is a **requirements doc for a new MCP server** — what tools it exposes, what shape they return, what languages or formats it covers — not a new package inside CoGrind. The capability-vs-infrastructure line in `plan.md` is what *enforces* that boundary; this section is what *guides the choice* in the first place.

---

## Things to Remember

1. Do not make an LLM do something that plain old code can do.
2. Agents need to know all about the model they are using (see docs/what_an_agent_needs_to_know.md) and use that knowledge to adapt their methods to their environment (model)
3. Prefer "divide and conquer" over "stop-compress-continue". Meaning, agents should strive to keep their tasks within the limits of the available context window. Example: need a summary of something big? have another agent do it for you so you don't waste context window on a non-context dependent side task like summarizing.

---

## How to use this document

- **When designing a new system or sub-agent**, ask: does this fit the memory + cognitive skills frame? If it doesn't, either the design is wrong or the north star needs to evolve. Pick deliberately.
- **When deciding what to build vs. defer**, ask: does this make the brain learn or think better, or does it just make CoGrind a different kind of thing? Prefer the former.
- **When the implementation feels heavy**, ask: am I building infrastructure for the brain, or am I building CoGrind into something it isn't? The capability-vs-infrastructure line in `plan.md` is downstream of this question.
- **When a tradeoff feels close**, default to the choice that keeps the Smalt readable, traceable, and human-fixable. That's the deepest property of the brain.

This document evolves. When a fundamental decision changes the shape of CoGrind, update the north star *before* updating `plan.md` — the north star is the why; `plan.md` is the how.
