# Schema reference (schema version 1)

Canonical column reference for `zaxbygraph sql`. Everything here was read from a
live database with `PRAGMA table_info`; if you change `src/zaxbygraph/schema.sql`,
re-dump rather than editing this from memory.

`zaxbygraph sql` is read-only. See [SQL access](#sql-access) at the end for what
the guard accepts and rejects.

## Conventions

- All timestamps are GitHub's ISO-8601 UTC strings (`2026-01-03T05:40:10Z`),
  stored verbatim as `TEXT`. They sort lexicographically, so `ORDER BY
  updated_at` and `WHERE updated_at > '2026-01-01'` both behave.
- Booleans are `INTEGER` `0`/`1` (SQLite has no boolean type).
- `repo` is always `OWNER/REPO`, lowercase as GitHub returns it. Every
  repo-scoped table carries it, so one database can hold several repos and
  **every query should filter on it** unless you deliberately want all repos.
- `number` is the GitHub issue/PR number. Issues and PRs share one number space
  per repo, so `number` alone identifies an item within a repo.
- `kind` is `'issue'` or `'pr'`.
- `raw_json` holds the original GitHub payload. Use it with SQLite's JSON
  functions (`json_extract(raw_json, '$.field')`) to reach a field this schema
  does not project into a column.

## items

One row per issue or pull request. The central table.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER | Primary key. GitHub's **issues-list** id. Never overwritten by the `/pulls/{n}` payload's id — see [identity](#identity-and-the-watermark). |
| `repo` | TEXT | `OWNER/REPO`. |
| `number` | INTEGER | Issue/PR number. `UNIQUE (repo, number)`. |
| `kind` | TEXT | `'issue'` or `'pr'`. |
| `node_id` | TEXT | GraphQL node id. |
| `title` | TEXT | |
| `body` | TEXT | May be `NULL` or empty. |
| `labels_text` | TEXT | Space-joined label names, denormalized for FTS. Use the `labels` table for structured access. |
| `state` | TEXT | `'open'` or `'closed'`. |
| `state_reason` | TEXT | GitHub's `state_reason` (e.g. `completed`, `not_planned`). |
| `author` | TEXT | Login. Joins `actors.login`. |
| `created_at` | TEXT | |
| `updated_at` | TEXT | Drives the incremental watermark. |
| `closed_at` | TEXT | `NULL` while open. |
| `merged_at` | TEXT | PRs only. `NULL` means not merged. |
| `merge_commit` | TEXT | PRs only. |
| `draft` | INTEGER | PRs only, `0`/`1`. |
| `locked` | INTEGER | `0`/`1`. |
| `base_ref` | TEXT | PRs only, e.g. `main`. |
| `head_ref` | TEXT | PRs only. |
| `additions` | INTEGER | PRs only. From `/pulls/{n}`. |
| `deletions` | INTEGER | PRs only. |
| `changed_files` | INTEGER | PRs only. `0` means the files GET was skipped. |
| `commits` | INTEGER | PRs only. |
| `html_url` | TEXT | Browser URL. |
| `api_url` | TEXT | REST URL. |
| `raw_json` | TEXT | Merged list + pull payload. |

A closed PR that was merged has both `state='closed'` and a non-null
`merged_at`; a closed PR that was rejected has `state='closed'` and
`merged_at IS NULL`. There is no separate `'merged'` state.

## comments

Issue comments and PR review comments.

| Column | Type | Notes |
| --- | --- | --- |
| `pk` | INTEGER | Synthetic primary key. |
| `github_id` | INTEGER | GitHub's comment id. `UNIQUE (repo, kind, github_id)`. |
| `repo` | TEXT | |
| `number` | INTEGER | The item this comment belongs to. |
| `kind` | TEXT | `'issue_comment'` or `'review_comment'` (CHECK-constrained). Issue and review comments have separate id spaces, so `github_id` alone is not unique. |
| `author` | TEXT | |
| `created_at` | TEXT | |
| `updated_at` | TEXT | |
| `body` | TEXT | |
| `html_url` | TEXT | |
| `in_reply_to` | INTEGER | Parent comment id for threaded review comments, else `NULL`. |
| `raw_json` | TEXT | |

Re-syncing an item replaces its comments. A duplicate row from paginated
fetching upserts on `(repo, kind, github_id)` rather than failing.

## reviews

PR reviews (the approve/request-changes records, not review comments).

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER | GitHub review id, primary key. |
| `repo` | TEXT | |
| `number` | INTEGER | |
| `author` | TEXT | |
| `state` | TEXT | `APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `DISMISSED`. |
| `submitted_at` | TEXT | |
| `body` | TEXT | Scanned for `closes`/`mentions` like any other body. |
| `html_url` | TEXT | |
| `raw_json` | TEXT | |

## pr_files

Files touched by a pull request. Source of `touches` edges and of `churn`.

| Column | Type | Notes |
| --- | --- | --- |
| `repo` | TEXT | Part of `PRIMARY KEY (repo, number, path)`. |
| `number` | INTEGER | |
| `path` | TEXT | Repo-relative path. The join key to a code graph. |
| `status` | TEXT | `added`, `modified`, `removed`, `renamed`. |
| `additions` | INTEGER | |
| `deletions` | INTEGER | |
| `changes` | INTEGER | `additions + deletions` as GitHub reports it. |
| `sha` | TEXT | Blob sha. |
| `patch` | TEXT | Unified diff — **only** when synced with `--include-patches`, else `NULL`. |

## labels

| Column | Type | Notes |
| --- | --- | --- |
| `repo` | TEXT | Part of `PRIMARY KEY (repo, number, name)`. |
| `number` | INTEGER | |
| `name` | TEXT | |
| `color` | TEXT | Hex, no leading `#`. |

Labels are replaced wholesale on each ingest of that item.

## edges

The graph itself. One row per relationship.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER | Primary key. |
| `repo` | TEXT | |
| `src_type` | TEXT | `actor`, `item`, `label`, or `file`. |
| `src_id` | TEXT | Login, item **number as text**, label name, or file path. |
| `rel` | TEXT | See the relationship table below. |
| `dst_type` | TEXT | Same domain as `src_type`. |
| `dst_id` | TEXT | |
| `confidence` | TEXT | Always `'EXTRACTED'`, enforced by a CHECK constraint. |
| `evidence` | TEXT | Provenance string, e.g. `pulls.files`, `body closing keyword`, `body #N`. Deliberately **not** part of the unique key. |

Unique key: `(repo, src_type, src_id, rel, dst_type, dst_id)`.

`src_id` and `dst_id` are `TEXT` even for item numbers, so compare with
`dst_id = '10'` or `CAST(dst_id AS INTEGER) = 10` — not `dst_id = 10`.

### Relationships

| `rel` | From → To | Meaning |
| --- | --- | --- |
| `authored` | actor → item | Opened the issue/PR. |
| `commented` | actor → item | Commented at least once. **Collapsed**: one edge per actor×item regardless of comment count. Counts live in `comments`. |
| `reviewed` | actor → item | Reviewed at least once. Also collapsed. |
| `has_label` | item → label | |
| `touches` | item → file | A PR changed this path. The join key to a code graph. |
| `closes` | item → item | A closing keyword plus a same-repo reference. Keyword-derived only — see the caveat below. |
| `mentions` | item → item | A same-repo reference **without** a closing keyword. |

`closes` and `mentions` are mutually exclusive for a given pair: a number that
is closed is not also emitted as a mention.

Node types are only ever `actor`, `item`, `label`, `file`. Never `issue`, `pr`,
`comment`, or `review` — an issue and a PR are both `item`, distinguished by
`items.kind`.

### What `closes` does and does not mean

`closes` is derived from **closing keywords in bodies and comments** —
`close`/`closes`/`closed`, `fix`/`fixes`/`fixed`, `resolve`/`resolves`/`resolved`
— followed by `#N`, `owner/repo#N`, or a same-repo GitHub URL.

It is **not** GitHub's connected-issue graph. Not present in v0.1:
auto-close from merge-commit messages, links made through the GitHub UI, and
anything that only appears in the timeline API. Cross-repo references are
ignored entirely rather than attached to a same-numbered local item. If a
conclusion depends on auto-close, say so explicitly rather than treating the
absence of a `closes` edge as evidence that no link exists.

## actors

| Column | Type | Notes |
| --- | --- | --- |
| `login` | TEXT | Primary key. |
| `html_url` | TEXT | |

## releases

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER | Primary key. |
| `repo` | TEXT | |
| `tag_name` | TEXT | `UNIQUE (repo, tag_name)`. |
| `name` | TEXT | |
| `body` | TEXT | Release notes. |
| `draft` | INTEGER | `0`/`1`. |
| `prerelease` | INTEGER | `0`/`1`. |
| `author` | TEXT | |
| `created_at` | TEXT | |
| `published_at` | TEXT | |
| `html_url` | TEXT | |
| `raw_json` | TEXT | |

Releases are not graph nodes in v0.1 — they are a table only.

## sync_state

One row per repo. What `zaxbygraph status` reads.

| Column | Type | Notes |
| --- | --- | --- |
| `repo` | TEXT | Primary key. |
| `issues_since` | TEXT | The watermark. Passed back to GitHub verbatim. |
| `last_full_sync_at` | TEXT | |
| `last_incr_sync_at` | TEXT | |
| `last_error` | TEXT | Last failure message, or `NULL`. Non-null means the sync stopped early and re-running will resume. |
| `item_count` | INTEGER | |
| `comment_count` | INTEGER | |
| `edge_count` | INTEGER | |
| `include_patches` | INTEGER | `1` if patches were stored, so a later run can tell a genuine no-patch state from a not-yet-backfilled one. |

### Identity and the watermark

Two rules that queries and any future ingest code must respect:

- `items.id` comes from the **issues-list** payload. Merging the `/pulls/{n}`
  payload must not overwrite `id` or `updated_at`, because the list row's
  `updated_at` is what the watermark is built from.
- `issues_since` is a list row's `updated_at` passed back to GitHub **verbatim
  and inclusive**. Nothing is subtracted from it. This re-fetches the boundary
  item on the next run, which is harmless because ingest is idempotent — and it
  is the reason no update can fall into a one-second gap.

Each item is ingested in a single `BEGIN IMMEDIATE` transaction that includes
its own watermark bump, so an interrupted sync leaves the watermark at the last
**fully committed** item and is resumable.

## fetch_log

Audit trail of HTTP fetches.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER | Primary key. |
| `repo` | TEXT | |
| `resource` | TEXT | e.g. `issues`, `pulls`, `comments`. |
| `resource_id` | TEXT | |
| `fetched_at` | TEXT | |
| `http_status` | INTEGER | |
| `note` | TEXT | |

## meta

| Column | Type | Notes |
| --- | --- | --- |
| `key` | TEXT | Primary key. Includes `schema_version`. |
| `value` | TEXT | |

## Full-text search

`items_fts` and `comments_fts` are FTS5 **external-content** tables
(`content='items'` / `content='comments'`), kept in sync by triggers. They store
no copy of the text; they index the base table in place.

Query them by joining back on the rowid:

```sql
SELECT i.number, i.title
FROM items_fts f
JOIN items i ON i.id = f.rowid
WHERE items_fts MATCH 'watermark'
ORDER BY rank;
```

`items_fts` indexes `title`, `body`, and `labels_text`, with
`content_rowid='id'` — so join it on `items.id`. `comments_fts` indexes comment
`body` with `content_rowid='pk'` — join that one on `comments.pk`, not
`comments.github_id`. FTS5 MATCH syntax applies: `"exact phrase"`, `a AND b`, `a OR b`,
`NOT`, and `pref*` prefix matching. A bare multi-word string is an implicit AND.

Note that `zaxbygraph search` already wraps both tables and returns highlighted
snippets, so prefer it over hand-written FTS SQL unless you need a shape it does
not produce.

## SQL access

`zaxbygraph sql` enforces read-only in two independent layers. They reject
different things, and knowing which layer answered explains the error you get.

**Layer 1 — statement guard** (`assert_read_sql`). The first keyword, after
comments and any UTF-8 BOM are skipped, must be `SELECT`, `WITH`, or `EXPLAIN`.
One statement only; a trailing `;` is fine but a second statement is not. The
scanner tracks string literals and quoted identifiers, so a `--` or `;` inside a
literal is data, not syntax. Violations exit **2**.

**Layer 2 — connection authorizer** (`db.connect_readonly_query`). The
connection is opened `mode=ro` with `PRAGMA query_only=ON` and a default-deny
`sqlite3` authorizer that allows only read actions (`SELECT`, `READ`,
`RECURSIVE`) plus the `data_version` pragma that FTS5 needs internally.
Denials surface at execution and exit **1**.

Verified behavior:

| Statement | Result |
| --- | --- |
| `SELECT 1` | accepted |
| `WITH x AS (SELECT 1) SELECT * FROM x` | accepted |
| `EXPLAIN QUERY PLAN SELECT 1` | accepted |
| `SELECT ';' AS x` | accepted — `;` inside a literal is data |
| `SELECT 1;` | accepted — trailing semicolon |
| `-- c⏎SELECT 1`, `/* c */ SELECT 1`, `﻿SELECT 1` | accepted — comments and BOM skipped |
| `DELETE FROM items`, `INSERT …` | rejected by layer 1 (exit 2) |
| `PRAGMA journal_mode=DELETE` | rejected by layer 1 (exit 2) |
| `ATTACH DATABASE 'x.db' AS y` | rejected by layer 1 (exit 2) |
| `SELECT 1; DELETE FROM items` | rejected by layer 1 (exit 2) |
| `SELECT '--'; DELETE FROM items` | rejected by layer 1 (exit 2) |
| `SELECT load_extension('x')` | **passes layer 1**, denied by layer 2 (exit 1) |

That last row is the reason both layers exist: `load_extension` is a function
call inside a legitimate-looking `SELECT`, so no keyword check can catch it.
Do not simplify this to "the guard blocks extension loading" — it does not.

Before relying on any of this, re-derive the current layers from
`src/zaxbygraph/query.py` and `src/zaxbygraph/db.py`. The invariant is that no
path through the `sql` subcommand can write, attach, set a pragma, or load an
extension; the specific mechanisms above are how that holds today.

Results are capped (default 200 rows) and rows are fetched incrementally, so a
query that *matches* millions of rows does not materialize them all. A
non-positive `--limit` is clamped rather than passed through, because a negative
`LIMIT` means *unlimited* in SQLite.
