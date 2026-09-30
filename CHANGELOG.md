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

## [v0.1.3] - 2026-09-30

### Highlights

This release caps the `mcp[cli]` dependency at `>=1.27,<2`, fixing a `ModuleNotFoundError` on startup for fresh `pip install cobalt-grinding` installs, which had been resolving to mcp 2.2.0 after that version removed `mcp.server.fastmcp`; installs from the image or from source were already unaffected because they use the lockfile. The remaining changes are documentation and internal alignment: the README, contributing guide, and architecture notes now state the current status, CI's test command, and the new dependency ceiling, three factual errors in the northstar were corrected, and the agent pointer files and CI workflow were brought into line with handbook v2.1.0.

### Bug fixes

- Keep mcp below 2, and bring the documents current (#9)

### Docs

- Three corrections of fact in the northstar (#10)

### Maintenance

- Align with handbook v2.1.0 (#8)

## [v0.1.2] - 2026-09-27

### Highlights

This release is internal maintenance: the project switches from squash merges and a direct back-merge to merge commits and a back-merge pull request, with the version-guard and release workflows re-assembled from the handbook templates and their dev-tools pins moved to v1.5.1. The only change visible to contributors is the contributing guide, which now documents merge commits and the back-merge pull request that closes a release.

### Maintenance

- Merge commits and the checked back-merge pull request (#6)

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

