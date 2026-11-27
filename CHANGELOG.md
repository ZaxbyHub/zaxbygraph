# Changelog

## [0.6.0] - 2026-11-25

Output-contract honesty and the self-describing surface on schema v12
(CURRENT_USER_VERSION 12; no schema change, and the 20-tool MCP registry
digest is unchanged from 0.5.2).

- **#92** Exits, envelopes and freshness agree. An `ok:false` answer now
  always exits nonzero and an `ok:true` answer exits 0 (the documented
  claim outcomes `4`/`5`/`6` keep their own exits): a dead stdout or a
  dead stderr no longer turns a failing command's exit code into 0 (the
  render and echo boundaries absorb the dead pipe, so every
  failure-emitter class returns
  its OWN exit code — usage 2, sql 1/2, no-corpus 3, claim outcomes 4/5/6,
  snapshot failures their code, successes 0 — instead of a flattened
  quiet-0, the envelope still rendering on the live pipe), and the
  shutdown-flush death preserves the dispatched command's
  code — including main()'s early-return arms: a
  usage error or an invalid repo slug on a dead stdout no longer exits
  120 with 'Exception ignored' noise; the arms answer their own code 2,
  and the coded runtime catch-all keeps 1).
  An unexpected exception escaping a command is a coded
  `runtime` envelope (`{TypeName}: {message}`), nonzero exit and no
  traceback, reported through a per-invocation context so a fault after
  the store opened still names the store's db/repo identity. CLI failure
  envelopes emitted after the store opened carry that store's real
  freshness — including the arms that close the connection before
  emitting, and the read/sync/init guard refusals that fire right after
  the open — with a no-filter post-open failure falling back to the
  store's single stored repo (zero or several stored repos keep the null
  block). MCP failures get the same guarantee through per-call
  thread-local state (reset at the start of every `tools/call`, so no
  stale identity leaks between calls or concurrent sessions). The README
  now carries the one exit-contract table (branch on the envelope's `ok`),
  states the MCP bundle's defaults beside every payload-parity claim
  (a 4000-token answer budget and briefs always on, the budget overridable
  per call via `budget_tokens`, no briefs knob over MCP), and the MCP
  registration guidance names all three documented clients (Claude Code,
  ZCode, opencode, the latter per the current `mcp.servers.<name>` shape).
  `docs/schema.md` now lists the WITH-prefixed and EXPLAIN/EXPLAIN QUERY
  PLAN DML statement classes that pass the statement guard and are denied
  by the authorizer, beside the retained `load_extension` row.
- **#93** The CLI's answers describe themselves. Every envelope carries
  `version: "0.3"` naming the envelope contract (additive; existing keys
  and payload shapes preserved), the MCP tool outputSchemas declare the
  `version` key in their required lists, and `schema --output COMMAND`
  emits a full-envelope JSON Schema for every registry command — derived
  from the one shape registry, covering the CLI-specific `open` bare list
  and the `item` object|array|null union — that real success AND failure
  envelopes validate against (shape drift corrected in passing: `brief`
  declares its real always-present keys including author/created_at/
  html_url/labels/updated_at; `triage` declares `features_missing`;
  `pr_guard` no longer requires the conditional `collisions` key; SQL
  positional row cells and the new `hints` are typed; the `open` row
  schema types the genuinely nullable identity columns —
  author/html_url/updated_at — as string-or-null). A zero-row `sql`
  read whose filter used a closed-vocabulary column (`items.kind`,
  `items.state`, `comments.kind`) with an out-of-vocabulary literal
  answers an annotate-only `hints` block naming the stored values and the
  canonical valid set (`items.state`'s canonical set is the production
  order open, closed, deleted, transferred — the gone-marker states sync
  writes; the empty literal hints too); the hint pass shares the
  statement's deadline, converts a deadline interrupt to the coded
  timeout exactly as the warnings pass does, and never fabricates an
  empty `stored_values` for a probe it could not run; in-vocabulary
  answers are byte-identical and
  filtered reads refuse invalid literals up front.

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
