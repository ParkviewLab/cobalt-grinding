<!--
SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# cobalt-grinding: architecture

This document describes how cobalt-grinding is put together and what its code does. Why the project exists is stated in [`northstar.md`](northstar.md). The dated record of the decisions behind the design is [`decisions.md`](decisions.md), and what has been proposed but not built is in [`in-flight_ideas.md`](in-flight_ideas.md). [`architecture.dot`](architecture.dot) draws the pieces, the repositories they come from and the state each one owns.

## What it is

cobalt-grinding is one program, the daemon `cobalt-grinding` (entry point `cobalt_grinding.daemon.main:run`). It is an MCP server in two directions. Towards its clients it serves twelve `wiki.*` tools, over streamable HTTP by default or over stdio. Towards its own work it is an MCP host: it starts four other MCP servers as child processes and calls their tools. It keeps no corpus of its own. The Smalt, the corpus of Markdown pages with its search index, belongs to the `smalt-mcp` child; the lab notebook of gaps, proposals and experiments belongs to the `ebony-enriching` child. What cobalt-grinding holds is the orchestration: it reads sources, asks Anthropic's API for summaries, entities, glossary terms and answers, and writes the results through `smalt-mcp`.

A person reaches it through an MCP client: the command-line client in the sibling repository [`cogrind-workshop`](https://github.com/ParkviewLab/cogrind-workshop), Claude Desktop, Claude Code or any other. No client is privileged; each calls the same tools. This repository ships no client.

It is published as a Python package on PyPI, which carries the daemon alone, and as a container image on GHCR, which carries the daemon and the four children (see [Packaging](#packaging)).

## Start-up and shutdown

`cobalt-grinding` runs these steps in order (`daemon/main.py`):

1. It loads the configuration ([below](#configuration)) and sets up logging.
2. It resolves the transport (`--transport`, else `[mcp] transport`, where `http` is read as `streamable-http`) and the bind address (`--host` and `--port`, else `[mcp] host` and `port`).
3. It sets `SMALT_DIR` in the `smalt-mcp` child's environment to `smalt_dir`, and `EBONY_ENRICHING_DIR` in the `ebony-enriching` child's to `ebony_dir`, unless that child's `env` table already sets the variable, and it creates `cobalt_grinding_dir`.
4. It builds the MCP server with its tools, the task scheduler and the corpus mutex (`daemon/server.py`).
5. Inside the transport's lifespan, before it accepts a connection, it starts its dependencies. First it starts a supervisor for each child whose `autostart` is true; the supervisors handshake in the background, so this step does not wait for any child. Then, if the environment holds the API key named by `[llm] api_key_env`, it builds the agent runtime: the Anthropic client, the fastembed embedder (which loads its model, downloading it on first use), the tools index and the host. Without the key it logs a warning and continues: `wiki.ask` then returns an error, and `wiki.ingest` writes pages without the model's notes. Last, it bootstraps the two substrates: for `smalt-mcp` and then `ebony-enriching` it waits up to 30 seconds for the child's handshake and calls the child's `bootstrap` tool, which creates the substrate's layout where it is missing and changes nothing where it exists. A failure at any of these steps is logged and the daemon carries on, so with neither substrate reachable it begins accepting connections about a minute after it starts.
6. It serves until the transport stops: uvicorn's own handling of SIGINT and SIGTERM for HTTP, the end of input or an interrupt for stdio. It then stops the children (it signals every supervisor, waits up to 5 seconds, and cancels those still running) and shuts the scheduler down.

FastMCP's HTTP application carries a lifespan of its own and does not run the one passed to FastMCP, so `_run_streamable_http` in `daemon/main.py` nests the two: cobalt-grinding's start-up runs before FastMCP's session manager starts, and its teardown after the session manager stops.

## Serving

Over streamable HTTP, uvicorn serves FastMCP's application, and the MCP endpoint is `/mcp` (`http://127.0.0.1:7474/mcp` by default). There is no authentication. FastMCP's host-header check is on, with `127.0.0.1`, `localhost` and `[::1]` allowed on any port: the server answers only requests addressed to one of those names and answers any other Host header with HTTP 421, whatever address uvicorn binds. Over stdio (`--transport stdio`) the daemon runs as the child of another MCP host, Claude Desktop for instance.

## Configuration

The configuration is TOML, loaded in five layers, each overriding the ones before it (`config.py`):

1. Built-in defaults.
2. The user-global file: the file given with `--config`, else `config.toml` in the user configuration directory that platformdirs gives for `cobalt_grinding`, which is `~/Library/Application Support/cobalt_grinding/config.toml` on macOS and `$XDG_CONFIG_HOME/cobalt_grinding/config.toml` on Linux (`~/.config/cobalt_grinding/config.toml` when the variable is unset). A missing file is skipped; a file named with `--config` must exist.
3. The per-Smalt file, `config.toml` in the Smalt directory that the first two layers name. `--smalt` and `COBALT_GRINDING_SMALT_DIR` change the Smalt directory but not which per-Smalt file is read.
4. Environment variables: `COBALT_GRINDING_SMALT_DIR`, `COBALT_GRINDING_EBONY_DIR` and `COBALT_GRINDING_COBALT_GRINDING_DIR` for the three directories, and `COBALT_GRINDING_<SECTION>_<KEY>` for a key of the sections `embedding`, `llm`, `mcp`, `logging`, `daemon` and `host` (`COBALT_GRINDING_MCP_PORT=7600`, for instance). Any other variable with the prefix is ignored with a warning. The children's tables cannot be set this way.
5. `--smalt <dir>`.

`--transport`, `--host`, `--port` and `--log-level` then override the loaded values for the run. There is no flag for the model. `cobalt-grinding --help` lists the flags, and `--version` prints the version from the installed package's metadata. The file holds the name of the variable that carries the API key, never the key.

| Key | Default | Use |
|---|---|---|
| `smalt_dir` | `~/Documents/Smalt` | given to `smalt-mcp` as `SMALT_DIR` |
| `ebony_dir` | `~/Documents/EbonyEnriching` | given to `ebony-enriching` as `EBONY_ENRICHING_DIR` |
| `cobalt_grinding_dir` | `~/.local/state/cobalt_grinding`, on every platform | the daemon's own state: the tools index |
| `[llm] model` | `claude-opus-4-7` | the model of every call the daemon makes |
| `[llm] api_key_env` | `ANTHROPIC_API_KEY` | the variable that holds the API key |
| `[llm] provider` | `anthropic` | not read; the provider is always Anthropic's |
| `[host] default_model` | unset | replaces `[llm] model` when set |
| `[host] tools_top_k` | 10 | tools the agent runtime offers per call |
| `[host] max_iters` | 20 | round trips the agent runtime allows per call |
| `[embedding] provider`, `model`, `dim` | `fastembed`, `BAAI/bge-small-en-v1.5`, 384 | the tools index's embedder; `voyage` and `openai` pass validation and are refused when the runtime starts |
| `[embedding] api_key_env` | unset | not read |
| `[mcp] transport`, `host`, `port` | `streamable-http`, `127.0.0.1`, 7474 | the server's transport and bind address |
| `[mcp.clients.<name>]` | the four children | the supervisor ([below](#the-children)) |
| `[daemon] max_workers` | 8, from 1 to 64 | the scheduler's thread pool |
| `[logging] level` | `info` | the daemon's log level |

`~` is expanded in the three directories. Files merge into the defaults key by key, so a file can add a child or change one of the four but cannot remove one; `autostart = false` leaves a child unstarted.

## The children

| Section | Command | Tool prefix | Role | Tools cobalt-grinding calls |
|---|---|---|---|---|
| `smalt-mcp` | `smalt-mcp` | `smalt` | the Smalt: pages, links, index, search | `bootstrap`, `status`, `reindex_all`, `task_status`, `search`, `read_page`, `traverse`, `find_by_alias`, `write_page`, `add_links` |
| `ebony-enriching` | `ebony-enriching` | `ebony` | the lab notebook | `bootstrap`, `list_gaps`, `add_gap` |
| `deco-assaying` | `deco-assaying` | `deco` | analysis of source code with tree-sitter | `analyze_file` |
| `flint-slating` | `flint-slating` | `flint` | reading PDFs | `pdf_info`, `pdf_read_text` |

These are the defaults of `[mcp.clients]`. A section may set `command`, `args` (none by default), `env`, `cwd`, `tool_prefix` (the section's name by default), `autostart` (true), `restart` (`on-failure`, `always` or `never`; `on-failure` by default), `call_timeout` (30 seconds) and `startup_timeout` (10 seconds). The children are separate programs found on `PATH`, not Python dependencies of this package. A section added under another name is started and supervised like these four, but no part of the daemon calls it; its tools could reach a model only through the agent runtime, which nothing calls ([below](#the-agent-runtime)). The daemon calls the four by their section names and raw tool names (`smalt-mcp` and `write_page`, for instance); the prefix is used only in the tools index.

The supervisor (`daemon/mcp_clients.py`) starts each child as `command` with `args` and speaks MCP over the child's standard input and output, through the MCP SDK's stdio client. The child's environment is the daemon's `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER`, together with the section's `env` table and the directory injected at start-up; nothing else of the daemon's environment reaches it. Its working directory is `cwd`, or the daemon's own when `cwd` is unset, and its standard error goes to the daemon's. The handshake (`initialize`, then `tools/list`) must finish within `startup_timeout`, after which the child is running and its tools are recorded as `<prefix>.<tool>`. The supervisor then pings the child every 2 seconds, allowing 5 seconds for each reply. A failed handshake, a failed ping or the child's exit ends the attempt: the child's tools are dropped, and the supervisor starts it again after 1, 2, 4 and so on seconds, at most 60, unless `restart` is `never`, or is `on-failure` and the child exited cleanly. A call to a child that is not running fails at once; a call that outlasts `call_timeout` fails with a timeout and leaves the child running. Every failure comes back as a result carrying an error text, never as an exception.

## The tools

| Tool | Arguments (defaults) | What it does |
|---|---|---|
| `wiki.status` | none | `smalt.status`, overlaid with the daemon's own fields: `version`, `cobalt_grinding_dir`, a fixed `milestone` of `"M2.7"`, the start time and uptime, the host and platform, the corpus mutex, and task counts; adds `wiki_exists`, `pages_indexed` and `embedding`, the fields cogrind-workshop's status display reads. Without `smalt-mcp` it returns the daemon's fields with `smalt_status_error` |
| `wiki.index` | `full` (false) | `smalt.reindex_all`, returning `smalt-mcp`'s task id; `full` is echoed and has no effect |
| `wiki.ingest` | `path`, `h_lang` (`"c"`) | ingests a file, a directory, a git URL or a PDF URL and returns the result when the ingest has finished ([Ingest](#ingest)) |
| `wiki.search` | `query`, `top_k` (10), `expand_hops` (0), `expand_label`, and the filters `glossary`, `is_domain`, `domain`, `has_aliases_containing`, `fetched_at_before`, `fetched_at_after` | hybrid search with optional graph expansion ([Search and gaps](#search-and-gaps)) |
| `wiki.get_page` | `page_id`, `fuzzy` (true) | `smalt.read_page`: the page by id, exact alias or fuzzy alias |
| `wiki.traverse` | `from_id`, `hops` (1), `label` | `smalt.traverse`: outgoing links from a page |
| `wiki.find_gaps` | none | `ebony.list_gaps`: the open knowledge gaps |
| `wiki.report_gap` | `query`, `why`, `source` | `ebony.add_gap`: records a knowledge gap in the lab notebook |
| `wiki.ask` | `question`, `top_k` (8), `expand_hops` (1), `max_tokens` (2048), `prior_messages` | a cited answer from the Smalt ([Answering questions](#answering-questions)) |
| `wiki.task_status` | `task_id` | the daemon's own task with that id, else `smalt.task_status` |
| `wiki.task_list` | `kind`, `status` | the daemon's own tasks |
| `wiki.task_cancel` | `task_id` | requests cancellation of one of the daemon's own tasks |

A tool reports the failures it anticipates as a payload with an `error` key (`ingest_error`, `invalid_argument`, `retrieve_error`, `converse_error`, `smalt_unreachable` or `not_found`) and a message, not as an MCP error.

The scheduler (`daemon/scheduler.py`) runs submitted work on a thread pool of `max_workers` threads, tracks each task's state (queued, running, succeeded, failed or cancelled) and supports cooperative cancellation. No tool submits work to it, so `wiki.task_list` returns an empty list, `wiki.task_cancel` finds no task, and `wiki.task_status` answers every id from `smalt-mcp`. The corpus mutex (`daemon/mutex.py`) is likewise created and never taken: `wiki.status` reports it, and it is always free. Writes to the Smalt are serialised inside `smalt-mcp`, which takes a lock of its own around each write tool.

## Ingest

`wiki.ingest` awaits the whole pipeline (`ingest/orchestrator.py`) within the tool call.

### What it accepts

The `path` argument is routed in this order:

1. An `http` or `https` URL whose path ends in `.pdf`, ignoring any query or fragment, is read by `flint-slating`: `pdf_info` gives the PDF's SHA-256, page count and metadata, and `pdf_read_text` its text.
2. Any other URL (`http://`, `https://`, `ssh://`, `git://` or `git@host:path`) is cloned with `git clone --depth 1` into a temporary directory, with a limit of five minutes, and the clone goes through the directory pipeline. The temporary directory is removed afterwards. `git` must be on `PATH`.
3. An existing file goes through the single-file pipeline.
4. An existing directory goes through the directory pipeline.

Anything else is an `ingest_error`.

A file's kind comes from its extension alone (`ingest/format_classifier.py`):

| Kind | Extensions | Read by |
|---|---|---|
| text | `.md`, `.rtf`, `.txt` | the daemon; RTF is read as text, markup included |
| code | `.py` (Python); `.c` (C); `.cpp`, `.cc`, `.cxx`, `.hpp`, `.hh` (C++); `.h` (the language given by `h_lang`) | the daemon, with a symbol outline from `deco-assaying` |
| config | `.json`, `.json5`, `.toml` | the daemon |
| pdf | `.pdf` | `flint-slating`'s `pdf_read_text`, given the file's path |

Every other extension is unsupported: a single unsupported file is an `ingest_error`, and in a directory such files are listed by name as ignored. No heuristic decides the language of a `.h` file: the caller's `h_lang` (`"c"` or `"cpp"`) applies to every `.h` file of the ingest.

The daemon reads text, code and configuration files up to 1 MiB, decodes them as UTF-8 with replacement of malformed bytes, and hashes the whole file with SHA-256. A local PDF's text comes page by page, joined with `--- page N ---` markers and cut to 1,048,576 characters; if `flint-slating` cannot be reached or reports an error, the PDF's page is written without its text.

### Extraction

For each file, three model calls run one after another through the provider (`ingest/agents.py`): a summary of two or three paragraphs; the named entities, each with aliases, a snippet and a kind, which the prompt confines to `person`, `org`, `product`, `repo`, `package`, `place` and `concept` and which defaults to `concept`, code identifiers being excluded by the prompt; and glossary terms, each with a definition of one to three sentences and a snippet. Each call is sent the first 30,000 characters of the file and may return 4,096 tokens; it uses the configured model and offers no tools. A call that fails yields an empty result for that step, and output that is not valid JSON is parsed as far as possible. Without an API key the three steps are skipped, and the pages are written without a summary, entities or glossary terms.

For a code file in Python, C or C++, `deco-assaying`'s `analyze_file` receives the file's text, its name and its language, with `include_chunks` false. Its answer is rendered as a Markdown outline of the symbols (kind, name and first line) and the imports. The renderer also shows a `parse_status` field that is not `ok`, but `deco-assaying` 0.3.5 reports its parse under `parse`, which the renderer does not read, so no parse status appears. When `deco-assaying` cannot be reached, the page is written without the outline.

The daemon calls `deco-assaying` and `flint-slating` directly, through the supervisor, rather than through the agent runtime, and it calls the provider directly for the three extraction steps, so that no extraction step is offered tools.

### What it writes

Pages are written with `smalt.write_page` in `create` mode (the final rewrite of a directory's index page uses `update`), and links with `smalt.add_links`. `smalt-mcp` 1.3.3 gives a page created this way a new identifier, its slug followed by a random suffix, unless the slug is a section identifier (`<source-id>::<path>`), which it writes in place.

- A single file becomes a source page with the slug `file-<stem>-<extension>`. Its frontmatter holds the alias and `location_uri` `file:<absolute path>`, `location_kind` `file`, `source_content_hash`, `fetched_at` and an empty `domains` list. Its body holds the summary, the symbol outline for code, and a "Source content" block with the path, the hash, whether the text was truncated, and the text itself in a fenced block.
- A PDF read from a URL becomes a source page in the same shape, with `location_uri` `url:<URL>`, the PDF's metadata as `pdf_*` fields and its page count, and the whole extracted text in the body.
- A directory becomes a source-index page and one section page per supported file. The index page's `location_uri` is `git:<origin URL>` for a git working tree with an `origin` remote, `obsidian:<path>` for an Obsidian vault (a directory holding `.obsidian/`) and `dir:<path>` otherwise; git takes precedence when both are present. Its frontmatter carries the ignored files and, where they could be read, the git remotes, branch, head commit (SHA, author, e-mail, date and subject) and whether the working tree is dirty, and the vault's name, plugin lists and three of its settings. The index page is written first with a placeholder body; each file then becomes a section page with the identifier `<index page id>::<relative path>`, its `parent_source` set to the index page, and a body built as for a single file; last, the index page is rewritten with an overview synthesised by one more model call from the section summaries, the list of sections, the ignored files and the captured metadata, and it is linked to each section with the label `contains`. The walk descends into subdirectories, skips symbolic links, hidden directories and a fixed list of build and environment directories (`node_modules`, `venv`, `dist`, `build`, `__pycache__` and the like), does not read `.gitignore`, and stops adding files after 500 supported files, marking the result as truncated. Links in the vault's notes are not read.
- Every extracted entity becomes an entity page (`entity_kind`, aliases, a link `mentioned_in` to the page it came from, and the snippet as evidence), and every glossary term a concept page with `glossary: true` (the definition, the snippet as evidence, and a link `defined_in`). The page the terms came from links to them with `mentions` and `defines`. `smalt-mcp`'s indexer lists the glossary pages in its generated glossary index page.

The pages therefore hold each source's text, up to 1 MiB of it for a local file, beside the notes made about it.

### Ingesting again

Before a single file or a PDF URL is processed, the daemon looks up an existing source page by its `location_uri` alias. When one exists and its `source_content_hash` equals the new hash, the ingest is skipped and reported with `skipped: true` and the reason `content_hash_matches_existing`. Otherwise a new source page is written, with new entity and glossary pages; the earlier pages remain. The same lookup is made for a directory, but its result is not used: every directory ingest writes a new index page and so new section pages, with new entity and glossary pages, beside the earlier ones. Entity and glossary pages are never merged with existing ones, within one ingest or across ingests.

### Failure

The ingest fails with an `ingest_error` for a missing path, an unsupported single file, a failed clone, a failed `flint-slating` call for a PDF URL, or a failed write of a source page or of a directory's first index page. A failed write of an entity page, a glossary page or a link is logged and skipped, as are a section whose page cannot be written and a failed final rewrite of the index page, which leaves the placeholder body. The result lists the pages written, the ignored files, the number of links added, whether the walk was truncated, and the start and finish times.

## Search and gaps

`wiki.search` (`retrieve/orchestrator.py`) sends the query, `top_k` and any filters to `smalt.search`, whose index fuses full-text search, vector similarity and alias matching. The daemon neither re-ranks nor caches. With `expand_hops` above zero it calls `smalt.traverse` from each of the first three hits, with that many hops and `expand_label` if given, and returns the edges found and the pages reached that were not already hits. `gap_detected` is true when there are no hits. Nothing is recorded in the lab notebook unless a client calls `wiki.report_gap`; no part of the daemon reports a gap by itself.

## Answering questions

`wiki.ask` (`converse/orchestrator.py`) needs the API key and returns a `converse_error` without it. It runs the search above with the question, `top_k` and `expand_hops`, then reads each of the first `top_k` direct hits with `smalt.read_page` and cuts each body to 4,000 characters. The pages reached by the expansion are not read or given to the model. It makes one model call: a system prompt that confines the answer to the excerpts and requires a `[page:<id>]` citation after each claim, then any `prior_messages` (entries whose role is `user` or `assistant` and whose content is a non-empty string, others being dropped), then the question with the excerpts. The citations in the answer are checked against the identifiers of the pages given: those that match are valid, and the rest are listed in `invalid_citations`. `gap_detected` is true when the search found nothing, or when the answer cites and every citation is invalid. The result carries the answer, the citations, the hits used, `gap_detected` and whether any excerpt was cut.

## The agent runtime

The agent runtime (`host/`) is built at start-up when the API key is present. It consists of:

- the provider (`host/provider.py`), a thin wrapper over the Anthropic SDK's asynchronous `messages.create`, holding the configured model and a default of 4,096 output tokens. Ingest and question answering call it directly;
- the tools index (`host/tools_index.py`), a LanceDB table `tools_index` under `<cobalt_grinding_dir>/tools_index`, holding each running child's tools with their descriptions embedded by fastembed. A search ranks the tools by full-text match on the description and by vector similarity, and fuses the two rankings by reciprocal rank (k = 60);
- the host (`host/api.py`), whose coroutine `run_agent(system, messages)` rebuilds the index whenever the set of running children's tools has changed since the last call, offers the model the `tools_top_k` tools that best match the latest user message, and runs the tool-use loop (`host/loop.py`). The loop calls the provider with its defaults, dispatches every `tool_use` block, feeds the results back, and stops at `end_turn`, `max_tokens`, `stop_sequence`, `refusal` or an unknown stop reason, or fails after `max_iters` round trips. A failed tool call is returned to the model as a `tool_result` with `is_error` set. `run_agent` accepts `model` and `max_tokens` and does not pass them on.

No part of the daemon calls `run_agent`. The index advertises each tool under its prefix (`smalt.search`), and the dispatcher (`host/dispatch.py`) looks the part before the first dot up as a child's section name, so a tool of a child whose prefix differs from its section name, as each of the four defaults' does, fails with `unknown client`.

## State

On disk, cobalt-grinding keeps only `cobalt_grinding_dir`, where the tools index is created when the agent runtime starts; its table is written only by `run_agent`, so it stays empty. fastembed keeps its model in its own cache. The Smalt and the lab notebook are written only by their children. `flint-slating` 0.1.6 keeps the PDFs it fetches and its outputs in `cache/` and `output/` under its working directory, which is the daemon's unless the section sets `cwd`, or `CACHE_ROOT` and `OUTPUT_ROOT` in `env`.

In memory, the daemon keeps the scheduler's task table, the start time and the set of tools last indexed. None of it survives a restart.

## Packaging

The package (`pyproject.toml`, built with hatchling) installs the daemon alone. The image (`Dockerfile`, based on `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`) installs the daemon from the lockfile into `/app/.venv` and the four children into the system Python with `uv pip install --system`, and runs `uv run cobalt-grinding`. It sets `COBALT_GRINDING_SMALT_DIR=/data/smalt`, `COBALT_GRINDING_EBONY_DIR=/data/ebony`, `COBALT_GRINDING_COBALT_GRINDING_DIR=/data/cobalt_grinding`, `COBALT_GRINDING_MCP_HOST=0.0.0.0` and `COBALT_GRINDING_MCP_PORT=7474`, declares `/data` a volume and exposes port 7474. The API key is passed at run time.

## Dependencies

The daemon's direct dependencies are `anthropic` (at least 0.97 and below 1.0) for the model calls, `mcp[cli]` (1.27 or later) for the server and for the stdio client of the children, `click` and `platformdirs` for the command line and the configuration, `pydantic` for the configuration's schema, and `lancedb`, `fastembed`, `pyarrow` and `numpy` for the tools index. `tomlkit` is declared and not imported. The Claude Agent SDK is not a dependency: the tool-use loop is the daemon's own.

## Source layout

| Module | Holds |
|---|---|
| `config.py` | the configuration's schema, defaults and layered loader |
| `app.py` | `App`, which holds the configuration, the supervisor and the agent runtime, and starts and stops them |
| `daemon/main.py` | the command line, start-up, bootstrap wiring and the HTTP lifespan |
| `daemon/server.py` | builds the FastMCP server, the scheduler and the mutex |
| `daemon/tools.py` | the twelve `wiki.*` tools |
| `daemon/bootstrap.py` | the substrates' bootstrap |
| `daemon/mcp_clients.py` | the supervisor of the children |
| `daemon/scheduler.py`, `daemon/mutex.py` | the task scheduler and the corpus mutex |
| `host/` | the agent runtime: provider, tools index and its LanceDB connection, embedder, loop, dispatch |
| `ingest/` | the ingest pipeline: routing and cloning (`orchestrator.py`, `url_fetcher.py`), classification and reading (`format_classifier.py`, `handlers.py`), directory walking and metadata (`structure_extractor.py`, `source_fetcher.py`), the extraction prompts (`agents.py`), and the clients of `smalt-mcp`, `deco-assaying` and `flint-slating` |
| `retrieve/orchestrator.py` | search, page reads, traversal and the gap tools |
| `converse/orchestrator.py` | question answering |

The tests are in `tests/`. Those marked `integration` spawn real subprocesses or load real models; the continuous integration runs the rest (`pytest -m "not integration"`). `tests/fixtures/stub_mcp_server.py` is a small MCP server that stands in for a child.

## Milestone labels in the code

Comments in the code, and the `milestone` field of `wiki.status`, name the development milestones in which the parts were built: M2 the daemon, its scheduler and its server; M2.5 the agent runtime and the supervisor; M2.7 the move of the Smalt's storage into `smalt-mcp` and of the command-line client into `cogrind-workshop`; M3 ingest, built in chunks; M4 search and gaps; M5 question answering. The decisions made along the way are in [`decisions.md`](decisions.md), and the milestones not built (M6 onward) are in [`in-flight_ideas.md`](in-flight_ideas.md).
