# Changelog

## [0.5.0] - 2026-11-16

Ships schema v12 (CURRENT_USER_VERSION 12) with the 20-tool MCP registry
whose sha256 digest over
`','.join(sorted(tool names))` is
`0eb4be5f40db5a88b8ffe9aaff1591a9063197a3361ded7c9ab44af544b8a9fc`
(recorded in `tests/test_release.py`'s RELEASE_PINS beside this version; a
tool-list change must ship with a version bump, a CHANGELOG entry, and a new
recorded pin).

- **#75** A PR's filtered timeline page that arrived whole is stored as
  complete: `filteredCount` is selected by both timeline query builders with
  a host-degrade retry, false `timeline truncated` notes and their re-spent
  timeline requests stop, and reopen events reach `item_events`.
- **#70** One bounded call answers "what should we work on next":
  `bundle --with-briefs --budget-tokens B` (and the MCP `bundle` tool with
  no arguments) returns decision-complete members under a token budget;
  triage/bundle default rows are slim with `--explain` opt-in; `brief`
  computes one row carrying labels/author/date and unresolved paths as
  strings; derived commands gain a deterministic operation budget.
- **#73** One version everywhere and a one-command first use: pyproject and
  `__version__` move to 0.5.0 (this file pins version + schema + toolset
  together); `server/discover` and `status` (which now also reports the
  opened store's `schema_version` beside `tool_version`) name the version;
  `zaxbygraph init --repo O/R` preflights gh presence/auth with coded
  errors and hints, builds the store through the sync path, and prints the
  MCP registration line; packaging carries the SPDX `Apache-2.0` license
  expression, Python 3.11-3.14 classifiers, and a README pipx/uvx
  one-liner; the agent skill ships inside the package with
  `zaxbygraph skill [--install DIR | --check [--dir DIR]]`, and `doctor`
  reports stale installed skill copies over the four documented default
  skill homes.
