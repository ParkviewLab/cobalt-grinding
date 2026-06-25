# Changelog

All notable changes to this project are recorded here. Each release entry
has two parts:

- **Highlights** — a 2-3 sentence "what's new" paragraph generated at
  release time by an Anthropic-API call (see
  `scripts/generate_changelog.py`).
- **Categorized changes** — a list of merged commits since the previous
  tag, grouped by [Conventional Commit](https://www.conventionalcommits.org/)
  prefix, produced by [git-cliff](https://git-cliff.org/) using
  `cliff.toml`.

The release workflow on every tag push regenerates both, commits the new
section here, and uses the same content as the GitHub Release body.

<!--
  Keep-a-Changelog ordering: [Unreleased] at the top, then newest
  released version, then older versions. generate_changelog.py inserts
  new "## [vX.Y.Z] - YYYY-MM-DD" sections directly below [Unreleased].
  Don't remove the marker.
-->

## [Unreleased]
