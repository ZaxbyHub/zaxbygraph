# Changelog

## [0.5.2] - 2026-11-23

Bug fixes on schema v12 (CURRENT_USER_VERSION 12; no schema change, and the
20-tool MCP registry digest is unchanged from 0.5.1).

- **#90** Guard seams. The read-only `sql` authorizer's function deny-list
  named exactly one function, so `fts3_tokenizer` (whose two-argument form
  registers a tokenizer, i.e. code execution on a build compiled with
  `ENABLE_FTS3_TOKENIZER`) passed both guard layers; the deny is now a named
  set and is build-independent. `export-corpus --out` refuses DOS device
  names with the documented `out_is_database` refusal through a portable
  predicate instead of dying late at `os.replace`: the reserved names
  (`CON`, `PRN`, `AUX`, `NUL`, `CONIN$`, `CONOUT$`, `COM1-9`, `LPT1-9`),
  any case, extension forms such as `CON.txt`/`CON.tar.gz`,
  colon-suffixed device spellings (`CON:`, `NUL:`), the superscript COM/LPT
  spellings, and absolute paths. The refusal stays Windows-only because a
  POSIX file may legitimately be named `CON`. `release --tokenless` finds a
  tokenless claim even when an older tokened holder is active on the same
  issue. The nearest-flag hint no longer suggests the very flag argparse just
  rejected (the bad-value hint still names its own flag, and cross-command
  hints for a valid flag are unchanged). The MCP `bundle` tool enforces its
  published bounds (`count`, `max_files`, and `pool`/`exclude` entries, all
  `>= 1`, with the item minima now published in the inputSchema), and the CLI
  `bundle --max-files` override refuses a value below 1 the way the
  `[triage] max_files` config key already did.
- **#89** Refusals and classifications. The `gh` stderr 429 verdict needs a
  status context (`HTTP 429`, `status code 429`) or the `too many requests`
  reason phrase, so an exact issue number such as "issue 429 ... (HTTP 404)"
  no longer shadows the real 404; genuine rate-limit messages still classify
  429. `sync --numbers` (and `bundle --pool`/`--exclude`) accept canonical
  ASCII decimal issue numbers only; a sign, underscore, non-ASCII digit, a
  value below 1, a value past the signed 64-bit range, or an oversized digit
  run is a coded `bad_request` before any fetch (the MCP `sync` and `bundle`
  number sinks enforce the same range, so no accepted number can reach a SQL
  bind and fail as an uncoded runtime error). The
  no-corpus test names its repo explicitly, so it no longer depends on the
  working directory being a git checkout. The repo-resolution error names the
  `--repo OWNER/REPO` remedy exactly once. The MCP paging cursor enforces the
  producer's own ASCII-decimal shape (`1_0`, `+5`, ` 5`, and non-ASCII digits
  are `bad_request`; `"0"` and ordinary offsets still page).

## [0.5.1] - 2026-11-22

Bug fix on schema v12 (CURRENT_USER_VERSION 12; no schema change, and the
20-tool MCP registry digest is unchanged from 0.5.0).

- **#88** A listing row the sync cannot parse no longer vanishes behind the
  advancing watermark. The row is skipped AND recorded, pins the issue-#65
  recovery floor (at the row's own `updated_at`, else the run's starting
  floor — the watermark the run started from, or from the beginning on a
  full or forced run; an earlier floor is never replaced), and keeps the run
  answering incomplete until a skip-free run re-drains and clears it. The
  fix covers the real REST page (non-dict elements are preserved by
  `GhApiSource.list_issues` instead of filtered out), the bulk GraphQL chunk
  builder, the main per-item path, the post-torn-refresh replacement, the
  pending torn-retry path, and targeted `--numbers` exact-number responses.
  A malformed row with a parseable number remains delivery evidence for the
  deletion oracle, and a run that saw any malformed row derives no new
  deletion candidates (absence needs complete listing evidence). A targeted
  run can no longer launder an active floor or its `last_error`, and a
  pre-v12 store now aborts loudly after recording instead of finishing
  clean.
- **#91** `snapshot publish` and `snapshot pull` decide which repos a store
  holds from every PRESENT repo-bearing table (all 17 tables `schema.sql`
  declares `repo` on, including `issue_field_values`), compared canonically
  (`lower(repo)`), not from `sync_state` alone — a destination or source
  carrying a foreign corpus (even child-only rows, even without its
  `sync_state` row) is refused instead of silently replaced or transferred
  under a single-repo manifest. Canonical identity now also governs
  `_bind_destination`, the local-watermark `local_newer` guard (newest
  matching watermark), publish completeness (ANY matching state row
  incomplete) and watermark (earliest non-NULL), the corpus digest (every
  stored spelling of the canonical repo), and `--repo` binding. Ownership
  scans fail closed: a present table that faults or a NULL/non-string repo
  value refuses with a coded error instead of aborting the scan into an
  allowed replacement.

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
