# Changelog

All notable changes to this project are recorded here. Each release entry has two parts: a **Highlights** paragraph, generated at release time by an Anthropic-API call, and the **categorized changes**, the pull requests merged since the previous tag grouped by their [Conventional Commit](https://www.conventionalcommits.org/) type, with any commit that reached the release without a pull request listed under Direct commits; a bookkeeping commit (a version bump, a change to `CHANGELOG.md` alone, a trivial merge) is left out of both. Both are written by dev-tools' `generate-changelog`, which the release workflow runs at a pinned release.

The v0.1.0 entry below was written by an earlier version of the generator and carries its Highlights paragraph alone; it stays as published.

The release workflow on every tag push regenerates both, commits the new
section here, and uses the same content as the GitHub Release body.

<!--
  Keep-a-Changelog ordering: [Unreleased] at the top, then newest
  released version, then older versions. generate-changelog inserts
  new "## [vX.Y.Z] - YYYY-MM-DD" sections directly below [Unreleased].
  Don't remove the marker.
-->

## [Unreleased]

## [v0.1.1] - 2026-09-27

### Highlights

`--version` and the status tool now report the version recorded in the installed package's metadata instead of a hard-coded "0.0.1", and the locked dependencies move past open security advisories in anyio, cryptography and starlette. The documentation has been reorganised around what the code does: `docs/plan.md` and `docs/ideation.md` give way to `docs/architecture.md`, a dated `docs/decisions.md`, `docs/in-flight_ideas.md` and `docs/architecture-why.md`, with two further ideas notebooks and README links, and the README and architecture notes now state that the bundled MCP servers do not run in the published image and that PDF ingestion has no reader. The remaining changes are to the release and changelog workflows, which are now assembled from the shared handbook parts and generate the changelog with the shared dev-tools script.

### Bug fixes

- Anyio, cryptography and starlette past their security advisories (#4)
- Report the installed version, and bring the documents current with the code (#5)

### Maintenance

- Drop the shallow re-fetch from the version guard (#1)
- Assemble the release workflows from the handbook's parts (#2)
- Generate the changelog with dev-tools' shared script (#3)

## [v0.1.0] - 2026-07-01

### Highlights

This initial public release introduces cobalt-grinding, an agentic LLM-Wiki for ingesting sources, retrieving content, and conversing over them, distributed under AGPL-3.0-or-later.

