# Cobalt Grinding (`cobalt-grinding`)

An agentic LLM-Wiki served as an MCP server. Point it at a directory, file, or URL; it ingests the contents into a markdown-canonical corpus of interlinked notes with structured metadata; serves the result through hybrid retrieval and a conversational interface; and runs the scientific method on itself to grow + audit + propose.

CoGrind is **an MCP server wrapped around an AI brain** — see [`docs/northstar.md`](docs/northstar.md). The implementation arc is in [`docs/plan.md`](docs/plan.md); the design conversation that produced it is in [`docs/ideation.md`](docs/ideation.md).

## Status

**Phase 1 complete.** The daemon binary (`cobalt-grinding`) is wired as an MCP host that supervises three sibling MCP children:

- [`smalt-mcp`](https://github.com/ParkviewLab/smalt-mcp) — the Smalt (canonical knowledge): markdown wiki + LanceDB hybrid retrieval.
- [`ebony-enriching`](https://github.com/ParkviewLab/ebony-enriching) — the lab notebook: proposals, experiments, gaps.
- [`deco-assaying`](https://github.com/ParkviewLab/deco-assaying) — code parsing (tree-sitter).

The three Phase 1 cognitive skills (Ingest / Retrieve / Converse) are wired through the `wiki.*` MCP surface:

- `wiki.ingest` — file or directory → SourcePages + EntityPages + glossary ConceptPages + cross-page links + code symbol outlines (via deco-assaying).
- `wiki.search` / `wiki.get_page` / `wiki.traverse` — hybrid retrieval + 1-hop graph expansion.
- `wiki.ask` — cited natural-language answer with hallucination flagging.
- `wiki.find_gaps` / `wiki.report_gap` — knowledge-gap queue (Research / M6 will read these in Phase 2).

Phase 2 (Research / Cogitate / Curate) is next.

## Running

`cobalt-grinding` is the daemon binary; the user-facing CLI lives in the sibling [`cogrind-workshop`](https://github.com/ParkviewLab/cogrind-workshop) repo, and Claude Desktop / Claude Code can connect over MCP directly.

### From source

```sh
uv sync
# Install the children you want supervised (each is a standalone MCP server):
uv pip install smalt-mcp ebony-enriching deco-assaying
# Run the daemon — streamable-HTTP transport on 127.0.0.1:7474 by default:
uv run cobalt-grinding
```

### From the published Docker image

The image bundles cobalt-grinding + the three sibling MCP servers, so the only thing you need to provide is your Anthropic API key and a host directory to mount as `/data`:

```sh
docker run \
  -p 7474:7474 \
  -v ./data:/data \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  ghcr.io/parkviewlab/cobalt-grinding:latest
```

Or pull a specific version: `ghcr.io/parkviewlab/cobalt-grinding:v0.1.0`.

### Configuration

Bootstrap runs automatically on first startup: the daemon waits for each child to handshake, then calls its `bootstrap` tool. Defaults: `SMALT_DIR=~/Documents/Smalt`, `EBONY_ENRICHING_DIR=~/Documents/EbonyEnriching`. Overrides via `--smalt`, env vars (`COBALT_GRINDING_SMALT_DIR`, `COBALT_GRINDING_EBONY_DIR`), or a `config.toml`. See `uv run cobalt-grinding --help`.

To use as a stdio-transport MCP server (e.g. from Claude Desktop): `cobalt-grinding --transport stdio`.

## Releasing

Tag-driven via [`.github/workflows/release.yml`](.github/workflows/release.yml). A push of a `v*` tag fires four jobs: a **gate** (tag matches the `pyproject.toml` version, which carries no dev marker; tag reachable from `origin/main`; version strictly greater than the previous release tag) that gates the two **publish** jobs (PyPI + GHCR Docker), and a **changelog** job that runs after the publishes — it generates the new `CHANGELOG.md` section (LLM-written "Highlights" header + `git-cliff` categorized list), commits it back to `main`, and creates the GitHub Release with the same content as its body. Use the [`ParkviewLab/dev-tools`](https://github.com/ParkviewLab/dev-tools) helpers:

```sh
git bump minor              # 0.0.1 → 0.1.0, committed
git release                 # annotated tag v0.1.0 from pyproject.toml
git push --follow-tags      # CI fires
```

Don't have the helpers? Install once: `git clone https://github.com/ParkviewLab/dev-tools.git ~/dev-tools && cd ~/dev-tools && ./install.sh`.

### Commit message convention

The changelog job categorizes commits using [Conventional Commits](https://www.conventionalcommits.org/) prefixes (the full list is in the ParkviewLab handbook's `commits-and-changelogs.md`):

| Title | Group in the notes | Notes |
|---|---|---|
| any type with `!` after it (`feat!:`), or a breaking-change footer | Breaking changes | listed there once, whatever its type |
| `feat:` | Features | user-visible |
| `fix:` | Bug fixes | user-visible |
| `perf:` | Performance | user-visible |
| `refactor:` | Refactor | |
| `docs:` | Docs | |
| `test:` | Tests | |
| `revert:` | Reverts | GitHub's Revert button titles a PR `Revert "…"`, which has no type |
| `build:` / `chore:` / `ci:` / `style:` | Maintenance | |
| any other title | Other changes | the whole title |
| a commit with no pull request | Direct commits | its subject and short hash |

A title without a recognised type is not dropped: it is listed whole under Other changes. So prefix your PR titles, and correct a title before the merge, since retitling afterwards does not change the commit. The groups appear in the order above, and an empty group is left out.

## License

Cobalt Grinding is **dual-licensed**. By default it is free software under the **GNU Affero General Public License v3.0 or later** (`AGPL-3.0-or-later`); see [`LICENSE`](LICENSE) for the full text. A separate **commercial license** is available from the copyright holder for use that cannot comply with the AGPL (e.g. embedding in a closed-source product). See [`LICENSING.md`](LICENSING.md) for details.

Copyright © 2026 Gary Frattarola.
