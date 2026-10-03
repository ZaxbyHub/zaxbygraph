---
name: zaxbygraph
description: >
  Local incremental GitHub issue/PR knowledge graph. Sync once with `zaxbygraph sync`,
  then query SQLite instead of paging issue/PR threads into context. Use on any audit,
  history question, recurrence hunt, regression archaeology, or "what touched this file"
  task for a GitHub repo.
---

# ZaxbyGraph

Forge graph for GitHub issues and pull requests — the history side. Complementary
to Graphify, which is the code graph. They join on file path.

Everything in the database is mechanically extracted from GitHub. Nothing in it
was written by a model. So rows are usable as **evidence**, and any
interpretation is yours to make and to label as yours.

## First action

```bash
zaxbygraph status --repo OWNER/REPO
```

`status` answers whether a corpus already exists, and whether it is complete
and error-free (`complete: true`, `last_error: null`). Only when status
reports no corpus for the repo (exit 3) —
run the one-time build:

```bash
zaxbygraph sync --repo OWNER/REPO
```

(On a GitHub Enterprise checkout, run every command WITHOUT `--repo` from
the checkout itself so the origin host is captured; a bare
`--repo OWNER/REPO` always keys the store under `github.com` - mixing the
two forms splits one repo across two stores.)

If a corpus exists but `complete: false` or `last_error` is non-null, the
last sync stopped early — re-run the same `sync` (it resumes from the
watermark) and do not draw conclusions from the DB until it finishes clean.

Then query. After a successful sync, **never** page `gh api --paginate` of
issues, PRs, comments, or reviews into context — that spends exactly the context
the sync saved. Once the user-level store exists, every checkout of the same repo —
worktrees, clones, subagents — resolves the SAME graph. During migration
(a checkout still serving a legacy in-repo DB before `doctor
--consolidate` runs), `zaxbygraph where` tells you which file each
checkout is serving; `status` remains the cheap guard against drawing
conclusions from a corpus that was never built.

Check the result before trusting the DB:

- `ok: true` and an `ingested` count → good. `ingested: 0` on a re-run means
  nothing changed upstream, which is success, not failure.
- `ok: false` → read `error`. Sync reports failure as a **result object on
  stdout**, not on stderr, so don't infer success from a silent stderr.

## Which command answers which question

| You want to know | Use |
| --- | --- |
| Is there data, and is it stale, incomplete, or broken? | `status` — check `complete`, `last_error`, and `issues_since` |
| Anything about a keyword, error string, symptom | `search "QUERY"` |
| Everything about one issue/PR | `item N` |
| What connects to this issue/PR | `related N [--depth 1]` |
| Which files change most / are riskiest | `churn [--limit 30]` |
| What's currently open | `open` |
| Whether two items or files are connected at all | `path A B` |
| Anything the above don't shape | `sql "SELECT …"` |
| Feed a graph viewer or join to a code graph | `export-graph` |

Start with `status`. `complete: false` or a non-null `last_error` means the
last sync stopped early and the DB is incomplete — re-run `sync` (the
watermark resumes it) before drawing conclusions from counts.

## Output shapes

Every command's JSON output is one self-describing **envelope** (v0.2):
`{ok, db, repo, freshness:{synced_at, age_s, complete}, data, truncated}` —
plus `error:{code, message, hint?}` on failures. The per-command payload lives
under `data`; `ok` is at the root. Errors answer on **stdout** in JSON mode
(exit codes unchanged), and stderr keeps one `error: MESSAGE` echo; a
successful read that resolves a repo also writes exactly one identity line
(`sync`/`doctor` never do; slug-less reads print none; `synced=0` means "no
recorded sync age"):
`# db=<path> repo=<slug> items=<n> synced=<age> complete=<yes|no>`.

**Formats:** `--format json` (pretty, default when piped) and `compact` (one
line) always carry the envelope · `jsonl` (one object per line for list
payloads — rows only, identity on the stderr line; any `head -n` prefix
parses) · `text` (default on a TTY, renders the payload only).
`--fields repo,number` projects rows to those keys that exist (unknown keys
are omitted) — applies to list payloads and `sql` rows in both row modes, not
to nested arrays inside dict payloads. `sql` rows are objects keyed by column
by default — duplicate column names are suffixed `name_2`, `name_3`, ... so
no value is lost (`--rows array` keeps positional lists with exact
duplicates; `--limit N` for the cap). `item N --max-body-chars C` truncates
bodies and marks them `truncated: true`.

**Payloads (`data`):** `status` → `{repos[], counts[]}` · `search` →
`{items[], matched_mode, total_matches, corpus_items}` (comment hits merge
into their item: `matching_comments` + `comment_snippet`) · `item` → all item
columns plus
`labels[] comments[] reviews[] files[] edges[]` · `related` →
`{number, repo, nodes[], edges[]}` · `path` → `{a, b, repo, path}` ·
`export-graph` → `{nodes[], edges[]}` · `churn`/`open` → arrays of row objects.

Common fields: items carry `number kind title state author updated_at html_url`;
edges carry `src_type src_id rel dst_type dst_id confidence evidence`. `search`
marks hits in `snippet` with `«` `»` (stemmed, bm25-ranked; a zero-hit result
carries `total_matches: 0` and `corpus_items: <n>`, so "no prior art" is
distinguishable from an empty corpus). `path` is `null` when no route exists —
with exit code 0, because "not connected" is an answer.

**Schema discovery:** run `zaxbygraph schema [TABLE]` for live DDL plus
per-column notes (TEXT-typed `edges.src_id`/`dst_id`; `pr_files.patch` is NULL
unless the sync used `--include-patches`). Do not follow a file path to
`docs/schema.md` from an installed copy of this skill — the command always
answers from the database you are querying.

## Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| `0` | Success | — |
| `2` | Bad request: bad flags, non-read SQL, multiple statements, environment guards (SQLite too old, database newer than this build), the DB holds other repos but not the resolved one (the error lists them) | **Fix the request or the environment.** Retrying unchanged always fails again. |
| `1` | Runtime: item not found, sync failure, authorizer denial | Situational — may be a real absence, or worth one retry |
| `3` | No corpus for the resolved repo: the resolved DB is missing or empty | Run the printed `zaxbygraph sync --repo <slug>` — a retry without syncing cannot succeed |

Errors print `error: MESSAGE` to stderr and answer with the structured
`{ok: false, error: {code, message, hint?}}` envelope on stdout (exit codes
unchanged).

## `sql` — the escape hatch

Read-only. `SELECT` / `WITH` / `EXPLAIN`, one statement per call. `PRAGMA`,
`ATTACH`, and every write are rejected; `load_extension` is denied at execution.

**Run `zaxbygraph schema [TABLE]` before writing queries** — it prints the
live DDL and per-column notes from the database you are querying (the full
column reference, `docs/schema.md`, lives in the repo). Column names are not
guessable, and three things trip up most first attempts:

- `edges.src_id` / `dst_id` are **TEXT** even for item numbers — use
  `dst_id = '10'`, not `dst_id = 10`.
- Always filter on `repo` unless you mean every repo in the DB. Reads
  resolve the repo by default (issue #2); `sql` result rows that carry a
  `repo` column are filtered to it, but a projection without a `repo`
  column is only store-scoped by construction — filter explicitly when
  aiming `--db` at a multi-repo file.
- `pr_files.patch` is `NULL` unless the sync used `--include-patches`.

Results are capped (default 200 rows) and fetched incrementally, so a broad
query is safe to run.

## Interpreting `closes` correctly

`closes` edges come from **closing keywords in bodies and comments** — `close`,
`closes`, `closed`, `fix`, `fixes`, `fixed`, `resolve`, `resolves`, `resolved` —
followed by `#N` or a same-repo URL.

This is **not** GitHub's connected-issue graph. Merge-commit auto-close, links
made in the GitHub UI, and timeline-only links are absent in v0.1. So:

> A missing `closes` edge means *no keyword said so*. It does not mean the two
> items are unrelated.

If a conclusion depends on auto-close or UI-linked issues, say that the data
cannot confirm it rather than treating absence as evidence. Cross-repo
references are dropped entirely, never attached to a same-numbered local item.

## Collectors

May run `status|search|item|related|churn|open|path|sql` and paste **truncated**
JSON. Must not cluster, rank, assign severity, name root causes, or propose
enhancements — gathering and judging stay in separate contexts.

## Forbidden after a successful sync

- `gh api --paginate` of issues, pulls, comments, or reviews into the lead
- Re-fetching the same corpus "to be sure"

Use `--force` only when you suspect updates that never bumped `updated_at`
(review-only changes), or to backfill patches after a no-patch sync.

## Default database

The graph is keyed by repo, not by checkout (issue #2). Resolution order:
`--db` → `ZAXBYGRAPH_DB` → the user-level store for the slug
(`<root>/<host>/<owner>/<repo>/history.db`, root = `%LOCALAPPDATA%\zaxbygraph`
on Windows, `${XDG_DATA_HOME:-~/.local/share}/zaxbygraph` elsewhere,
`ZAXBYGRAPH_HOME` overrides) → a legacy in-repo DB under the MAIN worktree or the current checkout
(migration continuity). Every checkout of the same repo resolves the same
file. Only `sync` (and `doctor --consolidate`) creates a database: reads
exit `3` and print the resolved path plus the exact sync command when no
corpus exists for the resolved repo.

- `zaxbygraph where` prints the whole resolution chain (cwd → git common dir
  → slug → DB path → exists / items / watermark / complete, legacy DBs found,
  serving DB, sync-lock holder).
- `zaxbygraph doctor [--consolidate] [--scan DIR]` reports scattered legacy
  DBs (per-DB item and garbled-row counts); `--consolidate` copies the
  freshest complete corpus into the store — originals are never modified.
- A second `sync` while one is running joins it: `{ok: true, ..., data: {joined: true}}`
  with zero GitHub calls; `--wait` blocks for the lock instead.

Never commit `history.db` — it is a rebuildable cache.

## Frontier-audit Phase 1

Replace "page every issue into context" with: `zaxbygraph sync`, then collectors
run `search` / `sql` / `related` and return truncated rows. The lead model alone
generates candidates. Full program:
[`../frontier-audit-enhance/SKILL.md`](../frontier-audit-enhance/SKILL.md).
Contract: [`../../docs/frontier-audit-hook.md`](../../docs/frontier-audit-hook.md).
