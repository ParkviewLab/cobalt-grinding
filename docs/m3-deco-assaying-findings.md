# deco-assaying — what its outputs actually look like

> Captured from a self-index of `https://github.com/ParkviewLab/deco-assaying@develop`
> via a locally-running deco-assaying MCP server. Indexing 66 files (55 Python, 11k LOC) took **1.67 seconds**
> end-to-end. Purpose: stress-test `docs/plan.md` M3's `parse_file`-shaped
> assumption against the real tool surface before writing M3 ingest code.

## 1. Tool surface as observed

### Job lifecycle
- `index_repo(source, git_ref?, ...)` → `{job_id}` immediately. Source can be a local path, a GitHub URL, or a GitLab URL. Optional `git_ref` for branch/tag/sha.
- `get_job_status(job_id)` → `{state ∈ {pending, running, done, failed, cancelled}, progress: {files_done, files_total}, errors_count, started_at, finished_at, error}`. Polled to completion.
- `get_log_events(job_id, from_offset?, limit?)` — append-only event log with byte-offset cursor.
- `cancel_job(job_id)` — cooperative.

### Repo-level rollups (cheap; small payloads)
- `get_manifest(job_id)` — counts, languages map, parse-error count, **entry_points**, **test/config/generated_file_count** buckets.
- `get_languages(job_id)` — `{lang: {file_count, bytes, loc}}`.
- `get_tree(job_id, analyzed_only?, path_prefix?)` — full path inventory; can scope to a subdirectory.
- `get_symbols(job_id, prefix?, kind?, file_prefix?)` — global symbol index, filterable. AND-combined filters.
- `get_errors(job_id)` — parse errors and skipped files.
- `list_job_files(job_id, glob?)` — discover per-file artifact paths.

### Per-file
- `get_file_analysis(job_id, path, sections?)` — full per-file analysis, with optional section subsetting (e.g. `["symbols","imports"]` to skip the `chunks` payload, which is the largest part).
- `analyze_file(content, filename?, language?, include_chunks?, chunk_max_tokens?)` — **inline content**, no disk access. Returns the identical envelope shape as a stored per-file artifact.

### Detection / capability
- `detect_language(path, first_line?)` — extension + shebang.
- `list_supported_languages()` — see §5.

## 2. Per-file shape (the actually-emitted JSON)

Top-level keys: **`file`, `module_doc`, `symbols`, `imports`, `exports`, `references`, `literals_of_interest`, `chunks`, `metrics`, `parse`**.

### `file`
```json
{
  "path": "src/...",
  "language": "python",
  "sha256": "caade03746...",
  "bytes": 6838,
  "loc": 199,         // total lines
  "sloc": 167,        // non-blank
  "blank": 32,
  "comment": 10,
  "is_generated": true,   // heuristic — fires on legit hand-written files; see §6
  "is_test": false,
  "is_config": false
}
```

### `module_doc`
String. Top-level module docstring extracted as its own field — *not* as a synthetic symbol.

### `symbols[]`
```json
{
  "kind": "function" | "method" | "class" | "constant" | "field" | ...,
  "name": "run_agent",
  "qualified_name": "Host.run_agent",
  "signature": "async def run_agent(\n    self,\n    *,\n    system: str,\n    ...\n) -> Message",
  "span": {
    "start_byte": 1259, "end_byte": 2042,
    "start_line": 48,   "end_line": 69
  },
  "doc": "Run one agent invocation end-to-end.",
  "modifiers": ["async"] | ["dataclass"] | [],
  "parent_qname": "Host"   // empty string if top-level
}
```

This is **substantially richer than `docs/plan.md` M3 expects**. Notable extras:

- **Full `signature` string** — Python type hints, decorators-on-class (e.g. `dataclass`), and the actual parameter list, preserved verbatim. M3's M3 spec only had `name + kind + span`.
- **`doc`** — docstring captured per-symbol (in addition to module-level `module_doc`). M3 had this in spec but parkview was speculative.
- **`modifiers`** — `["async"]`, `["dataclass"]`, `["static"]`, etc. Useful for "render `async def` correctly in section pages."
- **`parent_qname`** — explicit parent linkage. Combined with dotted `qualified_name`, both linkage paths work (e.g. `Host.run_agent` *and* `parent_qname=Host`). M3 spec had `parent` but unspecified shape.
- **Dataclass fields surface as symbols** with `kind: "field"` and the annotation as `signature` — not just methods. Constants too (`DEFAULT_TOP_K = 10` → `kind: "constant"`).

### `imports[]`
```json
{
  "module": "deco_assaying.analyzers.get_analyzer",  // dotted, names the imported attr
  "alias": null,
  "kind": "import" | "from",
  "span": {...}
}
```

`from x.y import Z` becomes `module: "x.y.Z", kind: "from"`. Fully-qualified, alias preserved separately.

### `exports[]`
Empty for Python (no `export` keyword). Populated for JS/TS/etc.

### `references[]`
**This is new vs M3 spec — and very useful.** Per call-site / inherit-site:
```json
{
  "name": "search",
  "qualifier": "self.tools_index.search",  // full dotted form as written
  "kind": "call" | "inherit",
  "span": {...},
  "in_symbol": "Host.run_agent"            // which enclosing symbol made the reference
}
```

Edge data, basically: `symbol_X --calls--> symbol_or_name_Y`. Pre-built call-graph edges per file. This is way more than `parse_file` was going to give us.

### `literals_of_interest[]`
String literals scanned for URLs, paths, env vars, SQL, route patterns. Empty in `analyze.py`; would populate for `routes.py` style files. Useful provenance signal — e.g. "this source mentions URL X" — but optional from M3's POV.

### `chunks[]`
```json
{
  "qualified_name": "Host.run_agent" | "<module>" | "",
  "kind": "function" | "module" | ...,
  "span": {...},
  "text": "<raw source slice>",
  "token_estimate": 758
}
```

AST-aware: chunks split on syntactic boundaries (function / class / module), tagged with the deepest enclosing symbol's qualified name. `text` is the **raw source bytes for the chunk**. `token_estimate` is approximate.

For cobalt-grinding's purposes: chunks are likely *too big* to embed wholesale (default `chunk_max_tokens=800`), but they're an excellent input to a per-file summarizer agent — the agent gets symbol-attributed source slices instead of arbitrary fixed-window splits. **cobalt-grinding doesn't store source, so we should not persist `text` to the wiki.** What we'd want is `(qualified_name, span, summary_of_chunk)`.

### `metrics`
```json
{
  "n_functions": 7, "n_classes": 0,
  "max_nest_depth": 1,
  "has_main_guard": false,
  "async_count": 0, "generator_count": 0,
  "test_count": 0
}
```

Aggregate per-file. Cheap signal for section-page frontmatter.

### `parse`
```json
{ "ok": true, "error_nodes": 0, "missing_nodes": 0 }
```

`ok = (error_nodes == 0 && missing_nodes == 0)`. A no-parser fallback adds `"reason": "no_parser"`. Distinguishes clean vs. partial trees cleanly — matches M3's `parse_status: partial` need exactly.

## 3. Repo-level shape

### `manifest`
```
file_count, total_bytes, tree_total, skipped_count, skipped_by_reason,
languages: {lang: count},
parse_errors_count,
entry_points: ["src/deco_assaying/__main__.py"],
test_file_count, config_file_count, generated_file_count
```

`entry_points` detection is interesting — picks `__main__.py` automatically. Useful for source-page bodies ("entry point: …") if we want it.

### `tree`
```json
{ "entries": [{"path": "...", "size": int, "analyzed": bool}], "total_in_repo": 66, "total_returned": 66 }
```

Includes both analyzed and skipped files (binary / oversize / gitignored when `analyzed_only=false`).

### `symbols` (global)
```json
{
  "qualified_name": "_mock_response._Ctx",  // dotted across enclosing scope
  "kind": "class",
  "name": "_Ctx",
  "language": "python",
  "file": "tests/test_github.py",
  "span": {"start_byte": ..., "end_byte": ..., "start_line": ..., "end_line": ...}
}
```

Global symbol index has **less detail than per-file symbols** — no `signature`, no `doc`, no `parent_qname`, no `modifiers`. It's purely a "where is symbol X defined" lookup. Total in deco-assaying repo: **663 symbols**.

### `languages`, `errors`
As shown in §1. Small.

## 4. `analyze_file` vs persisted per-file artifact

**Confirmed: same envelope shape.** Calling `analyze_file` on `cobalt-grinding/host/api.py` returned the same top-level keys (`file`, `module_doc`, `symbols`, `imports`, `exports`, `references`, `literals_of_interest`, `chunks`, `metrics`, `parse`) with the same per-symbol fields as `get_file_analysis` does on a stored artifact.

Source-of-truth confirms it: `src/deco_assaying/analyze.py:23` exposes a single `analyze_inline(content, filename, language, include_chunks, chunk_max_tokens)` function that returns the per-file dict; both the MCP `analyze_file` tool and the `index_repo` worker call it. No drift possible by design.

`include_chunks=False` strips chunks (still returns `chunks: []`).

## 5. Supported languages

**Full support (analyzer module exists):** Bash, C, C++, C#, Go, Java, JavaScript, PHP, Python, Ruby, Rust, TSX, TypeScript.

**Grammar-only (parses, fallback shape):** Clojure, CMake, CSS, Dart, Dockerfile, Elixir, Erlang, GraphQL, Groovy, Haskell, HCL, HTML, JSON, Julia, Kotlin, Lua, Make, Markdown, Nim, OCaml, Perl, Proto, R, Scala, SCSS, SQL, Svelte, Swift, TOML, Vue, YAML, Zig.

**For M3's stated scope** (`.py`, `.c`, `.cpp`, `.h`): all four are full-support.

## 6. Gaps vs. `docs/plan.md` M3's `parse_file` spec

M3 says (paraphrased):

> `codeparse.parse_file(path, language)` returns `{symbols: [{name, kind, span, docstring, parent}], imports: [{module, alias, span}], errors: [...]}`. Section pages render `Symbols` and `Imports` lists; partial-tree files get `parse_status: partial`.

Real surface:

| M3 expectation | Reality | Verdict |
|---|---|---|
| `parse_file(path, language)` (path-based) | `analyze_file(content, ...)` (inline) **or** `index_repo(source)` whole-repo job | **Mismatch.** M3 needs to choose: per-file inline, or whole-source job. |
| Symbol `{name, kind, span, docstring, parent}` | Has all of those, **plus** `qualified_name`, `signature`, `modifiers`, `parent_qname` | Strict superset — keep using ours, gain extras. |
| Import `{module, alias, span}` | Same shape, with `kind ∈ {import, from}` added | Strict superset. |
| `errors: [...]` | `parse: {ok, error_nodes, missing_nodes, reason?}` per file + repo-level `errors` rollup | Different shape; same information available. |
| `parse_status: partial` flag | `parse.ok == false && parse.reason != "no_parser"` ≈ partial | Easy to map. |
| (not specified) | `references[]` (call-graph edges per file) | **Bonus.** |
| (not specified) | `literals_of_interest[]` (URLs, paths, SQL) | **Bonus.** |
| (not specified) | `metrics{}` per file + global `manifest` rollup (entry_points, test/config/generated buckets) | **Bonus.** |
| (not specified) | AST-aware `chunks[]` with symbol-attributed text | **Bonus** — and freely usable; see §6a on why "no source copies" doesn't apply here. |

**Heuristic correction:** I initially flagged `is_generated: true` on `src/deco_assaying/analyze.py` as a false positive. It isn't — every Python file in the deco-assaying repo was generated by Opus (with the likely exception of `humans_notes.md`, if that's checked in). So the heuristic actually called this one correctly. Worth keeping as a soft signal regardless: in cobalt-grinding we should still treat `is_generated` / `is_test` / `is_config` as informative-but-not-authoritative — useful for source-page frontmatter, but not for excluding a file from ingest.

## 6a. The "no source copies" rule does NOT apply to deco-assaying output

I initially recommended that we *not* persist `chunks[].text` to the wiki on the strength of cobalt-grinding's "no source copies" rule. That was wrong. Clarification (per Gary):

> Deco-assaying is a tool that analyzes and summarizes for our agentic system. Its output is not the source — the source is the repo it analyzed. So copying deco-assaying output into our markdown is fine, on a case-by-case basis.

So the rule is:

- **The "no source copies" constraint applies to the original ingested artifact** (the `.py` / `.c` / `.pdf` / etc. file at its location). We don't republish those.
- **It does not apply to derived analysis** produced by a tool we asked to look at the source. That's our notes, regardless of whether a tool wrote them or an LLM did.

What this changes for M3:

- We *can* persist `chunks[].text` to section pages if we decide that's useful. (We probably won't store *all* of it — that's a section-page-bloat issue, not a copyright/policy issue. The deciding question becomes "is this useful to the agentic system later?" not "are we allowed to keep it?")
- We *can* paste a symbol's `signature` plus `doc` plus a deco-assaying-derived synopsis directly into a section-page body without summarizing first, if the deterministic shape is more useful than an LLM paraphrase.
- We *can* (and probably will) lift the per-symbol `references[]` edges into the wiki's `links` LanceDB table as `from_id → to_id` rows, treating the call-graph as wiki edges.
- The **summarizer-eats-chunks** option in §7 is still the most likely play for full chunk text, but for a different reason: section-page brevity and signal-to-noise, not source-copy rules. We still might keep some chunks verbatim.

Same logic generalizes: any future capability MCP child (PDF text extractor, OCR, archive walker) produces analysis-of-source, not source. Their output can go into the wiki at our discretion. **The "no source copies" rule is about not republishing the *thing the tool looked at*, not about not persisting the tool's output.**

## 7. Open questions for M3 redesign

These are not decisions — they are the questions the next "revise M3" task needs to answer:

1. **Per-file (`analyze_file`) vs. whole-source (`index_repo`) for cobalt-grinding ingest?**
   - `index_repo` matches M3's "directory = one source" model exactly: one job, one `job_id` we can cache, one shot at the rollups (manifest, tree, symbols, languages). Concurrency happens server-side.
   - `analyze_file` matches M3's pipelined per-file sub-agent model (each file goes through `summarizer → entity_extractor → glossary_extractor → ...`).
   - Hybrid is plausible: `index_repo` once for the directory, then `get_file_analysis(path)` per file in the per-file pipeline (no re-parse — the analysis is already on disk in the deco-assaying server).
   - **Recommendation lean**: hybrid. Use `index_repo` for directory ingests so we get the repo-level rollups (`manifest.entry_points`, language stats, global symbol index) for the **source page**, and use `get_file_analysis` per file for **section pages** without re-parsing.
   - Single-file ingests use `analyze_file` directly.

2. **What does the section-page body actually render?** M3 specified `## Summary / ## Symbols / ## Imports`. With references + metrics + module_doc available, candidates to add:
   - `module_doc` verbatim (cheap; very informative)
   - `## Calls` (or fold into Symbols as a sub-list per symbol from `references[]`)
   - Per-symbol `signature` instead of just `name`
   - `## Metrics` (one line)
   
   Open: do exports / references / literals warrant their own H2s, or fold into the LLM summary as input-only?

3. **Where does `chunks[]` go?** cobalt-grinding doesn't persist source. Two reasonable answers:
   - Drop it (no LanceDB row stores the chunk text). Section pages are summary + structure only.
   - Use chunks as input to the `summarizer` sub-agent (per-symbol-or-better summarization), then drop them. Best of both — summary quality goes up; nothing copyrighted is persisted.
   - **Recommendation lean**: option 2. Pass chunks to the summarizer; never write `text` to disk.

4. **Global symbol index — entity-page candidate?** The repo-level `symbols` rollup is exactly the kind of thing the M3 entity scope (people / orgs / products / repos / packages) **doesn't** want to flood with per-function rows. So **no auto-promotion** of code symbols to entity pages — they stay in section-page bodies. M3's existing rule is right; the rich global symbol index doesn't change that.

5. **`is_generated` / `is_test` / `is_config` heuristics — trust them?** False positive seen in the wild (`analyze.py` flagged generated). Use as soft signal in section-page frontmatter; do not use to *exclude* a file from ingest.

6. **Config naming.** `[mcp.clients.codeparse]` in the current plan should become `[mcp.clients.deco-assaying]` — and the autostart/install hint in the M2.5 default config needs updating.

7. **Rename `parkview-codeparse-server` → `deco-assaying`** throughout `docs/plan.md`, `docs/northstar.md`, and any other prose. The server URL and tool prefix change too (`codeparse.parse_file` → `deco-assaying.analyze_file` / `deco-assaying.index_repo` etc.).

## Verification

- ✅ `get_job_status(b19c9486767a4c49)` returned `state == "done"` (66/66 files, 4 no-parser errors only).
- ✅ Repo-level rollups captured (manifest, languages, errors, tree, symbols subset).
- ✅ Full per-file analysis captured for `src/deco_assaying/analyze.py` (all sections, including chunks).
- ✅ `analyze_file` inline call against `cobalt-grinding/host/api.py` confirms shape parity with stored artifact.
- ✅ Gap list against `docs/plan.md` M3's `parse_file` spec is concrete and section-by-section.
