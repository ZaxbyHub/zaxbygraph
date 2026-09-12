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
zaxbygraph sync --repo OWNER/REPO
```

Then query. After a successful sync, **never** page `gh api --paginate` of
issues, PRs, comments, or reviews into context — that spends exactly the context
the sync saved.

Check the result before trusting the DB:

- `ok: true` and an `ingested` count → good. `ingested: 0` on a re-run means
  nothing changed upstream, which is success, not failure.
- `ok: false` → read `error`. Sync reports failure as a **result object on
  stdout**, not on stderr, so don't infer success from a silent stderr.

## Which command answers which question

| You want to know | Use |
| --- | --- |
| Is there data, and is it stale or broken? | `status` — check `last_error` and `issues_since` |
| Anything about a keyword, error string, symptom | `search "QUERY"` |
| Everything about one issue/PR | `item N` |
| What connects to this issue/PR | `related N [--depth 1]` |
| Which files change most / are riskiest | `churn [--limit 30]` |
| What's currently open | `open` |
| Whether two items or files are connected at all | `path A B` |
| Anything the above don't shape | `sql "SELECT …"` |
| Feed a graph viewer or join to a code graph | `export-graph` |

Start with `status`. A non-null `last_error` means the last sync stopped early
and the DB is incomplete — re-run `sync` (the watermark resumes it) before
drawing conclusions from counts.

## Output shapes

Pass `--format json` when parsing; the default depends on whether stdout is a
TTY, and `text` mode leaves nested structures as JSON anyway.

**Objects:** `status` → `{repos[], counts[]}` · `search` →
`{items[], comments[]}` · `item` → all item columns plus
`labels[] comments[] reviews[] files[] edges[]` · `related` →
`{number, repo, nodes[], edges[]}` · `path` → `{a, b, repo, path}` ·
`export-graph` → `{nodes[], edges[]}`

**Bare arrays:** `churn` and `open` return top-level JSON arrays, *not* objects.
Indexing `["items"]` into them fails.

Common fields: items carry `number kind title state author updated_at html_url`;
edges carry `src_type src_id rel dst_type dst_id confidence evidence`. `search`
marks hits in `snippet` with `«` `»`. `path` is `null` when no route exists —
with exit code 0, because "not connected" is an answer.

## Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| `0` | Success | — |
| `2` | Bad request: bad flags, non-read SQL, multiple statements | **Fix the request.** Retrying unchanged always fails again. |
| `1` | Runtime: item not found, sync failure, authorizer denial | Situational — may be a real absence, or worth one retry |

Errors print `error: MESSAGE` to stderr (except `sync`, above).

## `sql` — the escape hatch

Read-only. `SELECT` / `WITH` / `EXPLAIN`, one statement per call. `PRAGMA`,
`ATTACH`, and every write are rejected; `load_extension` is denied at execution.

**Read [`../../docs/schema.md`](../../docs/schema.md) before writing queries.**
Column names are not guessable, and three things trip up most first attempts:

- `edges.src_id` / `dst_id` are **TEXT** even for item numbers — use
  `dst_id = '10'`, not `dst_id = 10`.
- Always filter on `repo` unless you mean every repo in the DB.
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

`.swarm/zaxbygraph/history.db` if `.swarm/` exists or is gitignored, else
`.zaxbygraph/history.db`, resolved from the git root. Override with `--db`.
Never commit it — it is a rebuildable cache.

## Frontier-audit Phase 1

Replace "page every issue into context" with: `zaxbygraph sync`, then collectors
run `search` / `sql` / `related` and return truncated rows. The lead model alone
generates candidates. Full program:
[`../frontier-audit-enhance/SKILL.md`](../frontier-audit-enhance/SKILL.md).
Contract: [`../../docs/frontier-audit-hook.md`](../../docs/frontier-audit-hook.md).
