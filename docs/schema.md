# Schema reference (schema version 8, `PRAGMA user_version` = 10)

Canonical column reference for `zaxbygraph sql`. Everything here was read from a
live database with `PRAGMA table_info`; if you change `src/zaxbygraph/schema.sql`,
re-dump rather than editing this from memory.

`zaxbygraph sql` is read-only. See [SQL access](#sql-access) at the end for what
the guard accepts and rejects.

Tip: `zaxbygraph schema [TABLE]` prints this same shape (DDL plus per-column
notes) from the live database you are querying, so an installed copy of the
skill never needs a file path to this document. Every command's JSON output is
the self-describing envelope documented in the README ("Output and error
contract"): `{ok, db, repo, freshness, data, truncated}`, plus
`error: {code, message, hint?}` on failures.

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
| `state` | TEXT | `'open'` or `'closed'`. A full sync whose listing drained may also mark a stored item `'deleted'` or `'transferred'` when the source reports it gone (the shipped GraphQL source reports only `'deleted'`; `'transferred'` requires a source that can distinguish the two). Marked rows are kept (edges pointing at them survive) and the marking leaves a `fetch_log` trail. |
| `state_reason` | TEXT | GitHub's `state_reason` (e.g. `completed`, `not_planned`). Overwritten with `marked <verdict>: <note>` when a deletion verdict marks the row. |
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
| `reactions_total` | INTEGER | v7, listing-owned (`reactions.total_count`). A re-ingest from a source that does not carry the key preserves the stored value; a key present with JSON null clears it. `NULL` on rows whose last payload predated v7. |
| `milestone` | TEXT | v7, listing-owned (`milestone.title`); same ABSENT/present-null rule. |
| `item_type` | TEXT | v7, listing-owned (`type.name`); same rule. |
| `sub_issues_total` | INTEGER | v7, listing-owned (`sub_issues_summary.total`). PR rows never carry the summaries in the listing payload, so this stays `NULL` for them. |
| `sub_issues_completed` | INTEGER | v7, listing-owned (`sub_issues_summary.completed`). |
| `dependencies_blocked_by` | INTEGER | v7, listing-owned (`issue_dependencies_summary.blocked_by`) — the **open** count. The link relations themselves are issue #27's scope. |
| `dependencies_blocking` | INTEGER | v7, listing-owned (`issue_dependencies_summary.blocking`) — the **open** count. |
| `dependencies_total_blocked_by` | INTEGER | v7, listing-owned (`issue_dependencies_summary.total_blocked_by`) — kept distinct from the open count. |
| `dependencies_total_blocking` | INTEGER | v7, listing-owned (`issue_dependencies_summary.total_blocking`). |
| `author_association` | TEXT | v7, listing-owned (the item row's own actor association). |
| `merged_by` | TEXT | v7, **pull-owned** (`/pulls/{n}` `merged_by` login). A listing-only re-ingest preserves it — the AC3 rule reproduced on `merge_commit` before v7. |
| `comment_count` | INTEGER | v7, **derived**: the stored-row count of `comments` for the item, recomputed on every ingest and by the migration — not a payload field. |
| `paths_text` | TEXT | v7, **derived**: space-joined `pr_files` paths (`ORDER BY path`), recomputed on every ingest. Indexed in `items_fts` as the search surface for touched paths, weighted below title/body. An emptied files section empties it too. |
| `raw_json` | TEXT | Merged list + pull payload. |

A closed PR that was merged has both `state='closed'` and a non-null
`merged_at`; a closed PR that was rejected has `state='closed'` and
`merged_at IS NULL`. There is no separate `'merged'` state.

Items indexes: items carries six non-unique indexes. `idx_items_repo_number
(repo, number)` and `idx_items_repo_merge (repo, merge_commit)` (merge-commit
lookups during ingest) are joined by `idx_items_repo_title (repo, title)` for
the revert-title lookups; `idx_items_kind_state`, `idx_items_updated` and
`idx_items_author` serve their name-sake filters. The title index is added by
a plain `CREATE INDEX IF NOT EXISTS` in the schema script, which runs on
every write-open — any store that opens for a write gains it, including a
`sync`, an MCP-triggered sync, or `doctor --consolidate` — with no
`user_version` bump, and read-only opens never create it (so can a
hand-built store that never opens for writing).
`idx_items_repo_merge` keeps merge-commit lookups on a seek; dropping it
would be a performance change, not a correctness one. The finish-time
`PRAGMA optimize` (below, [sync_state](#sync_state)) can also create a
`sqlite_stat1` table inside the store; it is planner bookkeeping, hidden
from `zaxbygraph schema`, and safe to delete.

## assignees

v7. Listing-payload assignees, one row per entry.

| Column | Type | Notes |
| --- | --- | --- |
| `repo` | TEXT | |
| `number` | INTEGER | The item this assignee belongs to. |
| `login` | TEXT | `PRIMARY KEY (repo, number, login)` — duplicate payload entries upsert idempotently. |

Item-scoped: replaced with the other children on every re-ingest.

## issue_field_values

v7. Listing-payload custom issue field values, one row per entry.

| Column | Type | Notes |
| --- | --- | --- |
| `repo` | TEXT | |
| `number` | INTEGER | The item this value belongs to. |
| `field_name` | TEXT | The payload entry's `name`. `PRIMARY KEY (repo, number, field_name)`. |
| `value` | TEXT | Stringified payload value; `NULL` when the entry carried an explicit null. |

Item-scoped: replaced with the other children on every re-ingest.

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
| `author_association` | TEXT | v7. REST supplies it natively; GraphQL maps `authorAssociation` when the capability probe reports the field. A probe-degraded re-ingest preserves the stored value via the pre-replacement snapshot. |
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
| `src_type` | TEXT | `actor`, `item`, `label`, `file`, or `commit`. |
| `src_id` | TEXT | Login, item **number as text**, label name, or file path. A foreign cross-referenced source is a repo-qualified id (`owner/repo#N`), never a bare number. |
| `rel` | TEXT | See the relationship table below. |
| `dst_type` | TEXT | Same domain as `src_type`. |
| `dst_id` | TEXT | A commit endpoint carries the sha, so the graph joins to `git log`. |
| `confidence` | TEXT | Always `'EXTRACTED'`, enforced by a CHECK constraint. |
| `evidence` | TEXT | Provenance payload, never part of an edge's identity — e.g. `pulls.files`, `body closing keyword`, `body #N`. Deliberately **not** part of the unique key; `source` **is** — with the `closes` exception below. |
| `source` | TEXT | Provenance stream: `'keyword'` (text patterns), `'timeline'` (closed/cross-referenced events), `'closing_ref'` (PR closingIssuesReferences), `'link'` (v10 GitHub-reported blockedBy/parent/duplicateOf/open-linked-PR connections), `'payload'` (structured fields). Part of the unique key: the same pair reported by two streams is two facts — with the `closes` exception below. `link` edges are exempt from the delete-on-re-ingest rule like `timeline`/`closing_ref`, and are replaced per connection only when that connection was fetched complete. |

Unique key: `(repo, src_type, src_id, rel, dst_type, dst_id, source)`.

**`closes` holds at most one row per `(repo, src_type, src_id, dst_type,
dst_id)` pair** (schema v6, issue #19): the `closing_ref` and `timeline`
streams corroborate ONE GitHub-reported closure, so ingest merges them
instead of storing both — the `closing_ref` source survives when present,
and `evidence` carries both provenance strings (`'; '`-joined, capped at
512 chars: over the cap the arriving stream's marker and the
closing-ref string are protected while older details trim first, and a
lone over-cap part is truncated, never dropped — merged evidence never
becomes NULL). Ingest-time merge lives in `store.insert_edge`;
the physical backstop is the partial unique index `ux_edges_closes_pair`
`ON edges(repo, src_type, src_id, rel, dst_type, dst_id) WHERE rel='closes'`,
created by migration v6 — deliberately **not** in `schema.sql`, because
`init_schema` runs the script before the migration chain and a UNIQUE index
there would fail against a v5 store's not-yet-collapsed duplicates (fresh
databases get the index when the forward chain runs). Every other relation
keeps two-rows-are-two-facts.

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
| `closes_keyword` | item → item | A closing keyword plus a same-repo reference in a title, body, comment, review body, or PR commit message (`commit <sha>` evidence). Keyword-derived only — see the caveat below. |
| `mentions` | item → item | A same-repo reference **without** a closing keyword, in a title, body, comment, or review body. |
| `closes` | item → item | GitHub-reported closing link: a `closed` timeline event (closer is a PR or a known merge commit) or a PR's `closingIssuesReferences`. Never written by keyword regexes. |
| `cross_referenced` | item → item | Stored as carried on each item's timeline: source → owner. GitHub reports every reference on **both** items' timelines, so a linkage can appear in either direction (`related`/`path` are direction-agnostic). Foreign sources are repo-qualified (`owner/repo#N`). |
| `closed_by_commit` | item → commit | The item's `closed` timeline event carries this commit sha. |
| `merged_by` | item → actor | Who merged the PR. |
| `merged_commit` | item → commit | The PR's merge commit sha — joins the graph to `git log`. |
| `reverts` | item → commit, or item → item | A `This reverts commit <sha>` body line targets the commit; a title-only revert (`Revert "…"`) targets the item whose exact quoted title matches, when exactly one does. |
| `branch_ref` | item → item | v9: the PR's `head_ref` carries exactly one issue number that names one stored issue. Multi-token branches (`fix/12-34`) and ghosts name nothing. Keyword-derived (rebuilt on re-ingest). |
| `blocked_by` | item → item | v10: GitHub-reported `blockedBy` connection. `source='link'`. |
| `child_of` | item → item | v10: GitHub-reported sub-issue `parent`. `source='link'`. |
| `duplicate_of` | item → item | v10: GitHub-reported `duplicateOf`. `source='link'`. Text saying "duplicate of #N" produces `duplicate_keyword` instead — never this relation. |
| `linked_pr` | item → item | v10: the issue's open linked PRs from GitHub's `closedByPullRequestsReferences` (`includeClosedPrs: false`). `source='link'`. |
| `duplicate_keyword` | item → item | v10: "duplicate of #N" text in a title, body, comment, or review body. Keyword-derived only; foreign references dropped. |

`closes_keyword` and `mentions` are mutually exclusive within a single text
payload — a title, body, one comment, or one review body: a number that is closed by
keyword in a payload is not also emitted as a mention from that payload.
Across payloads one pair can carry both.

Node types are only ever `actor`, `item`, `label`, `file`, `commit`. Never
`issue`, `pr`, `comment`, or `review` — an issue and a PR are both `item`,
distinguished by `items.kind`. A commit endpoint's id is the sha.

### What `closes_keyword` and `closes` do and do not mean

`closes_keyword` (source='keyword') is derived from **closing keywords in
bodies, comments, and review bodies** — `close`/`closes`/`closed`,
`fix`/`fixes`/`fixed`, `resolve`/`resolves`/`resolved` — followed by `#N`, `owner/repo#N`, or a
same-repo GitHub URL. It means *a keyword said so*; nothing more is claimed.

`closes` (source='timeline' or 'closing_ref') is what **GitHub reported**:
`closed` timeline events — including auto-close from merge-commit messages,
which lands as a closed event with a commit closer — and a PR's
`closingIssuesReferences`. Keyword regexes never write it. A commit closer
always yields a `closed_by_commit` edge; it yields `closes` only when the
closer itself is a PR, or the sha resolves to a stored PR's merge commit — a
direct-push closer that matches no stored PR leaves `closed_by_commit` alone.
Edges mirror the timeline events as reported, so after reopen/re-close cycles
**multiple closers coexist**; evidence timestamps order them, and
`items.state` is authoritative for current open/closed. `cross_referenced` is
likewise stored as carried: GitHub reports every reference on both items'
timelines, so the same linkage can appear in both directions.

What remains absent is anything beyond GitHub's own reports — no edge claims
a link GitHub did not report, with one deliberate exception: a title-only
revert (`Revert "…"`) links `item → item` by exact quoted-title match
against stored items, `source='keyword'` — a locally-derived lookup, not a
GitHub report. Cross-repo keyword references are dropped entirely rather
than attached to a same-numbered local item; foreign
`closingIssuesReferences` entries are discarded outright — never stored,
never repo-qualified — unlike timeline cross-references from other repos,
which are kept as repo-qualified ids (`owner/repo#N`) that never attach to a
same-numbered local item.

Two boundaries:

- **REST syncs see no timeline.** `--source rest` captures no timeline events
  and no closing references — both ride the GraphQL page query only — so a
  REST-only sync writes no `closes`/`cross_referenced` edges (keyword edges
  still derive from bodies). Conversely, a GraphQL-degraded re-ingest can
  never retract timeline- or closing-ref-backed edges once written: the
  delete-owned pass exempts `source NOT IN ('timeline','closing_ref')` on
  every re-ingest, `--force` included — and a `--force` run with a GraphQL
  source re-fetches these edges and refreshes them via upsert. Recovery for a
  genuinely stale row is manual SQL against the database file (e.g. the
  `sqlite3` CLI); the `sql` **subcommand** is read-only by design and is
  never a write path.
- **Timeline retention is bounded.** The page query carries
  `timelineItems(first: 50, CLOSED_EVENT, CROSS_REFERENCED_EVENT, REOPENED_EVENT)` (the itemTypes list is capability-degraded per host) and a
  continuation drains up to 500 events per item; beyond 500 the retained set
  is the contiguous **newest** 500 and sync logs a `fetch_log` note
  (`timeline truncated: retained newest 500 events`) — `fetch_log` is the
  durable, queryable truncation record. `closingIssuesReferences(first:
  100)` notes truncation the same way.

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

## Migrations 9 and 10

v9 (issue #26) adds `item_path_refs` and `item_signatures` and backfills both
from the stored `raw_json` text of items, comments and reviews. v10 (issue
#27) adds `item_events` and `pr_commits`; no backfill is possible (reopen
events and commit messages were never fetched before v10), so migrated stores
start with both empty and fill as items refresh.

## revisions

v8. Evidence history: the pre-image of every displaced text version — item
title, item body, comment body, review body — written inside the same
transaction that replaces it (issue #25).

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER | Primary key; the sequence order for the item's history. Assigned at insert, never updated. |
| `repo` | TEXT | Owning item's repo. |
| `number` | INTEGER | Owning item's number. |
| `entity` | TEXT | `item_title`, `item_body`, `comment`, or `review`. |
| `entity_id` | INTEGER | GitHub id for `comment`/`review` entities; `NULL` for item entities. |
| `kind` | TEXT | `issue_comment` / `review_comment` for comments; `NULL` otherwise. |
| `origin` | TEXT | `edit` (displaced by a newer payload) or `deleted` (vanished from a fetch whose section's `children_complete` flag was exactly `True`). |
| `content` | TEXT | Exactly the displaced stored value — a pre-image, so a first ingest writes nothing and an unchanged re-ingest writes nothing (growth tracks real edit volume). `NULL` is legal: the text was absent before this version began. |
| `valid_from` | TEXT | GitHub time this version became current: `updated_at` else `created_at` (items, comments); `submitted_at` else the owning item's `created_at` when `NULL` (reviews). Never the sync wall clock. |
| `valid_to` | TEXT | GitHub time it was displaced: the incoming item's `updated_at`, except comment edits prefer the comment's own incoming `updated_at`. |

Write rules:

- **Append-only at ingest.** Nothing in the sync path updates or deletes a
  revision — not child replacement, not the deletion oracle, not migrations.
  `doctor --consolidate` is the one sanctioned rewrite: it replaces the whole
  corpus snapshot and re-assigns fresh ids, ordered so each item's sequence
  survives.
- **No content key.** A row exists only when the stored value and the
  incoming value differ (exact, NULL-aware). An edit reverted A→B→A therefore
  yields two rows and can never wedge a sync. A displacement whose
  GitHub-time window cannot be established (both bounding stamps NULL on
  the payload and stored row) is not recorded rather than written with an
  unusable window.
- **Tombstones are complete-fetch facts.** An `origin='deleted'` row means
  the entity was present in the stored children and absent from a fetch the
  source flagged complete for that section. A degraded fetch (flag missing,
  `False`, or otherwise not exactly `True`) writes no `deleted` rows —
  `edit` pre-images are still written whenever content changed. Boundary:
  a degraded fetch still replaces children, so an absence first seen
  degraded destroys the stored row and a later complete fetch has nothing
  to tombstone — that evidence gap is a deliberate property of the shipped
  replace-on-ingest contract, not a bug.
- **Backfill: none.** Text displaced before v8 was never stored, so a
  migrated database starts with an empty `revisions` table. `status`
  coverage reports `revision_rows`/`revision_bytes` as `NULL` on pre-v8
  stores and honest zeros on an empty v8 store; `item --history` /
  `--as-of` degrade with an explicit note rather than fabricating history.


## item_path_refs

v9 (issue #26). Raw path strings mentioned in item text — Python frames
(`File "<path>", line N`), `path:L:C` tokens, same-repo `blob/<ref>/<path>`
URLs, and backticked `seg/seg.ext` segments. Item-scoped children: replaced
on every re-ingest.

| Column | Type | Notes |
| --- | --- | --- |
| `origin` | TEXT | `'body'`, `'issue_comment:<id>'`, `'review_comment:<id>'`, or `'review:<id>'`. |
| `path` | TEXT | The raw string, deliberately UNRESOLVED — resolution against the stored file vocabulary happens at read time (issue #29), never at ingest, so rows stay independent of sync order. |
| `backticked` | INTEGER | 1 when the path appeared inside backticks. |

## item_signatures

v9 (issue #26). Error signatures from fenced blocks: the exception name, the
exception line with run-varying constants masked (`0x…` → `<hex>`, digit runs
→ `<n>`), and the nearest frame function when the block carries Python
frames. Item-scoped, replaced on re-ingest.

## item_events

v10 (issue #27). GitHub-reported timeline events stored as rows (currently
`reopened`); events never become edges. Replaced only when the timeline
section was fetched **complete** — a degraded re-ingest (REST source, 404,
capability-dropped field, truncation cap) leaves stored rows untouched, and a
plain-REST sync answers no timeline at all (presence `None`), which touches
nothing.

## pr_commits

v10 (issue #27). A PR's commit messages (`sha`, headline+body composed).
Replaced only when the commits section was fetched complete. The derived
`closes_keyword` edges (a commit message saying `fixes #N`, evidence
`commit <sha> closing keyword`) re-derive from these RETAINED rows on every
ingest, so a REST or overflowed re-ingest never retracts them.

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
| `full_sync_pending` | INTEGER | `1` while a full sync has been started but not completed by any clean run; also set by a targeted `sync --numbers` run against a store that has never completed a full sync, so a numbers-only corpus reads incomplete. `status` derives `complete` = `full_sync_pending = 0 AND last_error IS NULL` per repo. |
| `rate_limit_remaining` | INTEGER | `0` when the last clean run observed its budget at the floor (it slept a window through, or proceeded with the floor already reached); `NULL` when the run saw no rate limit. The reset instant is in `rate_limit_reset_at`. |
| `rate_limit_reset_at` | TEXT | When that window resets (verbatim from the source); `NULL` when the run saw no rate limit. |

The three counts are recomputed when a sync run finishes (and by
`doctor --consolidate` and the schema migrations), not per ingested item:
values are exact after every completed run, and may lag inside a run or
after an interrupted one until the next such refresh. Two visible
consequences: the stderr identity line's `items=N` reads the stored
`item_count` while `where`'s `items` payload is a live `COUNT(*)`, so the
two can disagree after an interrupted run; and `status`'s `repos[]` counts
can disagree with its live `counts[]` for the same reason.

### Schema versions and migrations

`PRAGMA user_version` is the authoritative schema state (currently `10`). The
`meta.schema_version` row is informational only. `init_schema` (which runs
the migration chain) executes only on a WRITABLE open: a `sync`, an
MCP-triggered sync, or `doctor --consolidate` (adoption migrates its temp
copy first). Read commands open the database read-only and never migrate,
so healing of an old cache happens on the next writable open, not on
reads. `init_schema` then:

- refuses loudly when `user_version` is newer than the build (a database from
  a future zaxbygraph),
- creates missing objects at the current shape (idempotent `IF NOT EXISTS`),
- applies each pending migration in its own transaction that ends by stamping
  `user_version` — an interrupted migration rolls back whole and re-runs.

Migration 3→4 adds `sync_state.rate_limit_remaining` / `rate_limit_reset_at`
(issue #5: the sync layer sleeps out an exhausted core rate limit until the
reported reset and reports the window instead of aborting).

Migration 4→5 rebuilds `edges` for timeline provenance (issue #6): a
`source TEXT NOT NULL` column after `evidence`, the unique key widened to
`(repo, src_type, src_id, rel, dst_type, dst_id, source)` — the same pair
reported by two provenance streams is two facts (amended by v6 for
`rel='closes'`, below) — keyword `closes` rows
renamed to `closes_keyword`, and a mechanism-honest backfill (legacy `closes`
→ `closes_keyword` + 'keyword', legacy `mentions` → 'keyword', everything
else → 'payload'). Ids and row counts are preserved and the migration is
idempotent; older builds refuse a v5 database.

Migration 5→6 collapses duplicate `closes` pairs and installs
`ux_edges_closes_pair` (issue #19): since #6 shipped, a PR that closed an
issue was stored once per provenance stream — `closing_ref` from the PR's
`closingIssuesReferences` and `timeline` from the closed event — so joins
and `what-closed` double-counted the pair. The migration keeps the
`closing_ref` row (the structured declaration) with both evidence strings
merged and deletes the other; existing caches heal on the next writable
open (sync / `doctor --consolidate` / MCP sync — reads never migrate). The
index then makes the invariant physical; see the `edges` section above.

Migration 1→2 adds `sync_state.full_sync_pending` and folds repo slugs to
lowercase across every repo-keyed table, merging case-split duplicates
(freshest row per folded key; `sync_state` timestamps take MAX; distinct-id
`(repo, number)` collisions keep the freshest item and delete the losers'
number-scoped rows under their own casing). For a legacy row whose
`last_full_sync_at` is NULL the migration sets `full_sync_pending = 1`: a
complete-but-unprovable corpus reports `complete: false` until the next clean
run drains the listing and stamps the timestamp — truthful because each item
commits with its own watermark bump, so a clean run from the watermark has
covered everything at or below it.

`complete` for a migrated legacy row means "no unresolved full sync since the
last recorded one": v1 recorded nothing when a run crashed outside its caught
exception types, so a database whose last full sync completed and whose later
`--force` refresh crashed pre-upgrade migrates to `complete: true` even though
that refresh never finished. The state is self-healing — the next clean sync
drains the listing from the watermark and everything at or below it is
covered — and `sync --force` re-establishes ground truth. v1 data cannot
distinguish this shape from ordinary incrementals, so the migration keeps the
recorded timestamp rather than forcing a full resync of healthy databases.

Two operational notes: `sync` is the one command that can open a legacy
database writable and perform the one-time migration write in place. Read
commands open the file `mode=ro` and never migrate, and plain `doctor`
never writes a legacy file either — it reads legacy databases through
migrated temp copies, and `doctor --consolidate` migrates the destination
store it adopts into (creating it if needed, whether that store is new or a
pre-existing v2 store), never the source. A v2 database therefore keeps the
old FTS tokenizer until its next in-place `sync` (`search` reports
`index_stale: true` meanwhile). The forward-only version check runs in
`init_schema`: older builds refuse a v3 database on those write paths, while
their read commands (which never call `init_schema`) still open a v3 file —
the FTS queries simply run against porter stemming they may not expect.

Migration 2→3 rebuilds both FTS tables in place with the porter tokenizer:
the six sync triggers and both virtual tables are dropped and recreated with
`tokenize = 'porter unicode61'`, then `INSERT INTO <fts>(<fts>)
VALUES('rebuild')` repopulates each index from its content table — no resync
of `items`/`comments` happens. The migration runs per-statement (never
`executescript`, which would commit the migration transaction) inside its
own `BEGIN IMMEDIATE`, so an interrupted rebuild rolls back whole and
re-runs. Stemming is why this matters: `reconnect` matches
`reconnection`/`reconnecting` only after the rebuild.

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
| `resource` | TEXT | `item` (one row per ingested item, and per deletion verdict), `pr_files`, or `graphql_probe` (capability-probe omissions and failures). |
| `resource_id` | TEXT | |
| `fetched_at` | TEXT | |
| `http_status` | INTEGER | |
| `note` | TEXT | |

## meta

| Column | Type | Notes |
| --- | --- | --- |
| `key` | TEXT | Primary key. Includes `schema_version` and the capability-cache namespace `capability_probe:<host>:<gh-version>` (value: JSON `{"Issue": [...field names], "PullRequest": [...]}`, rewritten by every successful probe). |
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

`items_fts` indexes `title`, `body`, `labels_text`, and (v7)
`paths_text`, with `content_rowid='id'` — so join it on `items.id`.
`comments_fts` indexes comment `body` with `content_rowid='pk'` — join that
one on `comments.pk`, not `comments.github_id`. `reviews_fts` (v7) indexes
review `body` with `content_rowid='id'` — join it on `reviews.id`. All three
tokenize with `porter unicode61` (schema v3; the v2→v3 migration rebuilds
older databases in place, and the v6→v7 migration rebuilds `items_fts` and
`reviews_fts` in place — creating an external-content FTS never indexes rows
the store already holds). For hand-written SQL, FTS5 MATCH syntax applies:
`"exact phrase"`, `a AND b`, `a OR b`, `NOT`, and `pref*` prefix matching — a
bare multi-word string is an implicit AND, and a porter-stemmed term matches
its morphological variants.

Note that `zaxbygraph search` already wraps all three tables with stemmed,
bm25-ranked, OR-fallback matching and merged comment/review hits (path text
weighted below title and body), so prefer it over hand-written FTS SQL unless
you need a shape it does not produce. On a pre-v7 store (reads never
migrate), `search` degrades to the legacy items+comments arms and reports
`index_stale: true`; `status`'s `coverage.null_v7_rows` is `null` there for
the same reason.

## status coverage

`status`'s `coverage` block (v7) distinguishes "absent upstream" from "not
fetched": `truncation_notes` maps `fetch_log.resource` to the count of that
resource's notes containing `truncated`; `dropped_fields` counts
capability-probe `omitted <field>` notes; `null_v7_rows` counts items rows
where `reactions_total` or `author_association` is `NULL` — the two listing
keys GitHub always carries with a non-null value, so their `NULL`
unambiguously marks a payload that predated v7. Columns whose `NULL` is
legitimate current state are deliberately excluded (unmilestoned/untyped
issues' `milestone`/`item_type`, PR rows' missing dependency summaries,
unmerged PRs' pull trio) so ordinary rows are not flagged. `null_v7_rows` is
`null` on pre-v7 stores. `store_bytes` is the database size in bytes
(`PRAGMA page_size * page_count`, database-global). The three derived counts
follow `status`'s repo scoping.

v8 adds two revision-coverage counts, repo-scoped the same way:
`revision_rows` counts stored pre-images and `revision_bytes` sums their
`content` sizes in UTF-8 bytes (`LENGTH(CAST(content AS BLOB))` — a plain
text `LENGTH` counts characters, not bytes). Both are `NULL` on pre-v8
stores and honest `0` on a v8 store with no edits yet, so "no history
recorded" is never confused with "history recorded, nothing displaced".
Feature gates pin the schema slot that introduced each capability (v7
coverage on `>= 7`, revision coverage on `>= 8`), never the build's current
version — a read-only store several slots behind keeps every feature its own
schema supports.

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

## Derived reads (issues #29, #30)

Seven commands are **derived reads**: `similar`, `explain`, `coupling`,
and `eval` (whose `localize` mode ranks files) from issue #29, joined by
the work-selection layer of issue #30 — `triage` (two-stage ranking of
open issues), `bundle` (greedy work bundles over the triage pool), and
`brief` (one issue's work packet). All seven are computed at query time
from the tables above and never stored (one exception reads past the
store: `brief --live` fetches linked-PR CI state/reviews through `gh` at
query time and stores nothing — the store-only claim holds for every
command run without `--live`), under the same never-written /
never-stored invariants as their #29 siblings. Every result carries
`derived: true`. The invariants:

- The derived tier opens the store through the same read-only connection
  reads use (`db.open_existing`, `mode=ro`); per-query scratch (the path
  vocabulary, suffix index, adjacency, coupling index) lives in in-memory
  Python structures, and a store file hash before/after any derived command
  is byte-identical. The `sql` subcommand keeps its own guard layers and
  never shares a connection factory with the tier.
- No new relations: derived pairs never enter `edges` (whose rows remain
  EXTRACTED, GitHub-reported facts only), and the feature needs no schema
  migration. None of the seven commands is on the MCP surface yet — that
  surfacing of all seven is issue #32's lane.
- Raw path strings in `item_path_refs` are resolved at read time against a
  vocabulary built from merged-PR `pr_files` paths (minus `removed`), by
  exact or unique-suffix match in either direction; unresolved mentions
  never anchor a derived pair.
- `eval` consumes this store read-only and reports `corpus_sha256` over the
  same content-line stream `export-corpus` hashes (via the exported
  `iter_corpus_content_lines` / `corpus_sha256_from_lines` helpers), inside
  one read snapshot with the metrics. Stores below the export floor or ahead
  of this build report `corpus_sha256: null` with a `store_too_old` /
  `store_too_new` label instead.
