# Changelog

All notable changes to this project are recorded here. Each release entry has two parts: a **Highlights** paragraph, generated at release time by an Anthropic-API call, and the **categorized changes**, the pull requests merged since the previous tag grouped by their [Conventional Commit](https://www.conventionalcommits.org/) type, with any commit that reached the release without a pull request listed under Direct commits. Both are written by dev-tools' `generate-changelog`, which the release workflow runs at a pinned release.

The release workflow on every tag push regenerates both, commits the new
section here, and uses the same content as the GitHub Release body.

<!--
  Keep-a-Changelog ordering: [Unreleased] at the top, then newest
  released version, then older versions. generate-changelog inserts
  new "## [vX.Y.Z] - YYYY-MM-DD" sections directly below [Unreleased].
  Don't remove the marker.
-->

## [Unreleased]

## [v0.1.0] - 2026-07-01

### Highlights

This initial public release introduces cobalt-grinding, an agentic LLM-Wiki for ingesting sources, retrieving content, and conversing over them, distributed under AGPL-3.0-or-later.

