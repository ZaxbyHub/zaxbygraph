# ZaxbyGraph: state-of-the-art gap review and the bundling plan (rev 3)

**Status.** Proposal only. Nothing in this document is implemented.

**Anchor.** Every code claim was checked at
`b47913eeeb90095b8a15998eae34413523853fa5` (current `main`, schema v6, 310
tests green). Before filing, re-verify each claim at that commit and expand
each file:line reference to a full-SHA permalink, as CONTRIBUTING:92
requires.

**Revision history.**

- **Rev 1** was written from the docs and a partial read of the code.
- **Rev 2** followed a full read of the code and added measurements.
- **Rev 3** applies an independent critic pass. The critic re-checked every
  claim against the code and introspected the live REST API and GitHub's
  official GraphQL schema. Its blocking findings were then reproduced in
  this session before being accepted.

Corrections are kept inline as **CORRECTED (rev N)**. Labels:

- **VERIFIED**: re-checked or measured, with the method stated.
- **INFERENCE**: unproven, with the check that would settle it.

CONTRIBUTING:118 forbids filing an INFERENCE. Every INFERENCE below must be
settled before its PR's issue is filed.

---

## 1. Verdict

The ingest core is strong, and none of these need redesign:

- per-stream edge provenance;
- idempotent per-item transactions;
- the inclusive, verbatim watermark;
- the append-only exemption for GitHub-reported link streams;
- the two-layer read-only `sql` guard;
- one envelope on every surface.

**CORRECTED (rev 3).** Rev 2 credited the core with a "None vs [] section-presence
contract". That contract does not exist for the timeline.
`sync._resolve_children` sets `timeline: list[dict] = []` unconditionally
(`sync.py:206`) and always returns a list. Timeline edges survive a degraded
re-ingest only because `delete_owned_edges` exempts `source IN
('timeline','closing_ref')` (`store.py:84-88`). Children sections signal
presence through key presence in the bulk payload (`sync.py:167-182`,
`graphql.py:555-557`), not through None.

Four gaps keep the tool from being first-class:

1. **Ingest scales quadratically.** Three separate superlinear terms grow
   per-item cost with corpus size (§2.1).
2. **Signals for choosing work are missing or not modeled.** Issues link to
   no files; demand, ownership, blocking, and duplicate signals are absent.
3. **Nothing answers relatedness, and nothing measures it.** No primitive
   finds related or bundleable items, and no harness evaluates one.
4. **The agent surface lags.** The tool does not use MCP features its own
   advertised protocol version already offers, and it is not ready for
   2026-07-28.

---

## 2. Verified findings

### 2.1 Cold sync is quadratic: three terms

All three terms below were measured. The critic ran a separate experiment: 4,000 items,
about 24 edges per item, timed per 1,000-item batch.

| Configuration | Seconds per batch | Ratio, last to first batch |
|---|---|---|
| base | 4.98, 10.91, 15.64, 21.82 | 4.38 |
| + `INDEXED BY idx_edges_dst` | 3.01, 5.30, 7.46, 10.36 | 3.44 |
| + no per-item `recount` | 2.04, 2.76, 4.51, 5.03 | 2.47 |
| + `items(repo, title)` index | 2.23, 2.08, 2.27, 2.36 | **1.06** |

Only the fourth configuration, with all three fixes, keeps per-batch time flat.
This session's own run is consistent: batches took 12.3 s, 32.0 s, 51.4 s, and
69.1 s per 2,000 items at 2k through 8k. Profiled at 4k items,
`delete_owned_edges` took 11.7 ms per call and `recount` 3.6 ms.

**Term T1: the actor-edge DELETE.** It is at `store.py:89-92`:

```python
"DELETE FROM edges WHERE repo = ? AND dst_type = 'item' AND dst_id = ? "
"AND rel IN ('authored', 'commented', 'reviewed')",
```

Its plan is `SEARCH edges USING INDEX sqlite_autoindex_edges_1 (repo=?)`, a
scan of every edge in the repo. The same predicate as a SELECT, or as a DELETE
without the `rel` term, plans onto `idx_edges_dst`. Running `ANALYZE` also
fixes the plan.

- **The fix:** `INDEXED BY idx_edges_dst`. VERIFIED: its plan is
  `SEARCH edges USING INDEX idx_edges_dst (repo=? AND dst_type=? AND dst_id=?)`.
- **CORRECTED (rev 3).** Rev 2 offered "split into equality deletes" as an
  alternative. That is disproven: `... AND rel = 'authored'` still plans
  `sqlite_autoindex_edges_1 (repo=?)`. VERIFIED in this session.

**Term T2: per-item `recount`.** `recount` is called inside every item's
transaction (`store.py:632`). It runs three COUNTs, and each is O(N) or O(E)
per item.

**Term T3: title lookups.** Every ingest runs them at `store.py:363-368`
(`title_lookup`, for revert PRs) and twice at `store.py:403-407` (the reverse
revert pass). Each plans as `SEARCH items USING INDEX idx_items_repo_merge
(repo=?)`, which is non-covering. Every lookup therefore reads every item row
of the repo, `raw_json` included. Real `raw_json` sizes make this worse than
the synthetic run.

- **CORRECTED (rev 3).** Rev 2 measured T3 and then left it out of the fix.
  With `CREATE INDEX ... ON items(repo, title)` the plan becomes
  `SEARCH items USING INDEX ... (repo=? AND title=?)`. VERIFIED.

**The fix (A0):**

1. Add `INDEXED BY` on T1.
2. Add an `items(repo, title)` index. It needs migration v7, so A0 is a schema
   change.
3. Stop recounting inside each item's transaction. `sync_state` counts come
   from covering-index COUNTs at `status` read time, or from per-item deltas.
   This replaces rev 2's per-page recount, which goes stale after a
   mid-page crash.

**Regression tests.** Both checks are deterministic. No wall-clock ratio test
is used, because one would be flaky.

1. Assert on `EXPLAIN QUERY PLAN` text for T1, T3, and the counts.
2. Count VDBE steps with `conn.set_progress_handler` while ingesting item
   2,000 and item 4,000. Assert that per-item steps stay within a constant
   factor.

### 2.2 Read paths that are O(E) per call

The derived tier must not build on any of these as they stand.

- **`path_between`** loads every structural edge of the repo per call
  (`query.py:503-506`). VERIFIED.
- **The `item`/`related` edge query** is
  `repo=? AND ((src_type='item' AND src_id=?) OR (dst_type='item' AND dst_id=?))`
  (`query.py:331-336`, `:366-370`). Its plan is `SEARCH edges USING INDEX
  idx_edges_dst (repo=?)`, a full repo-edge scan, and it runs once per BFS
  frontier node in `related`. VERIFIED in this session.
- **`what_closed` PR rows** scan every `closes` row through
  `ux_edges_closes_pair (repo=? AND src_type=?)`. Critic-measured.

**The fix:** rewrite each as a `UNION ALL` of two fully indexed lookups. Pin
the plans in tests.

### 2.3 Issues have no file linkage

`touches` comes only from PR files (`extract.edges_from_files`,
`extract.py:173-179`). VERIFIED.

### 2.4 Demand, ownership, type and dependency data is stored but unmodeled

No column, child table, or edge exists for this data (`store.py:148-211`,
`schema.sql:15-44`). VERIFIED. `raw_json` is the merged listing+pull payload
(`store.py:587-610`).

**Live REST listing.** The critic called the live REST API
(`gh api repos/ZaxbyHub/zaxbygraph/issues`). VERIFIED. **Issue rows** carry
these keys:

- `assignees`, `author_association`, `milestone`, `reactions`, `type`
- `sub_issues_summary`
- `issue_dependencies_summary`, with keys `{blocked_by, blocking,
  total_blocked_by, total_blocking}`
- `issue_field_values`, `closed_by`, `pinned_comment`

**PR rows lack** `sub_issues_summary` and `issue_dependencies_summary`.

**Consequence.** Every A1 field is already on disk for corpora synced
recently, and GraphQL is not needed to get them.

**Two cases are not confirmed.**

- INFERENCE: whether **older stored rows** carry every key. Check with the
  `json_type(raw_json, ...)` query below on the user's corpus.
- INFERENCE: GHES versions may lack these keys.

```bash
zaxbygraph sql "SELECT number, json_type(raw_json,'$.reactions'), json_type(raw_json,'$.assignees'), json_type(raw_json,'$.type'), json_type(raw_json,'$.issue_dependencies_summary'), json_type(raw_json,'$.issue_field_values') FROM items ORDER BY updated_at DESC LIMIT 5"
```

### 2.5 GraphQL coverage, and how fragile adding to it is

**Current coverage.** VERIFIED.

- The timeline captures only `CLOSED_EVENT` and `CROSS_REFERENCED_EVENT`
  (`graphql.py:141`, `:280`).
- Any other typename becomes a bare `{"type": ...}` (`graphql.py:272`).
- Issue nodes select only comments and the timeline (`graphql.py:170-174`).

**Fragility (new in rev 3).** `_graphql` raises on any error that is not
NOT_FOUND (`graphql.py:357-366`). VERIFIED. One unsupported or removed field
therefore aborts every page query, and with it every sync.

**Fields that exist.** The critic checked GitHub's official schema
(`github/docs` `schema.docs.graphql`) for these. Verified there (official schema
file, not live introspection):

- `Issue.blockedBy`, `blocking`, `parent`, `issueType`, `duplicateOf`
- `subIssuesSummary`, `issueDependenciesSummary`, `issueFieldValues`
- `closedByPullRequestsReferences`
- `PullRequest.statusCheckRollup`
- `MarkedAsDuplicateEvent { canonical duplicate }`
- timeline enums `REOPENED_EVENT`, `MARKED_AS_DUPLICATE_EVENT`, and
  `UNMARKED_AS_DUPLICATE_EVENT`

`PullRequest.closingIssuesReferences` takes `excludeUserLinked: Boolean =
false`. User-linked (Development-sidebar) issues are therefore included by
default, which settles rev 2's `CONNECTED_EVENT` inference.

**`stateReason(enableDuplicate:)`** is marked for removal, so it must never be
requested.

**Do not validate against the octokit schema mirror.** It lacks
`blockedBy` and `issueType`.

### 2.6 Read connections can write temp objects

**CORRECTED (rev 3).** Rev 2 said a `mode=ro` read connection "can never create
a table". That is false.

- On an `open_existing` connection, `CREATE VIRTUAL TABLE temp.v USING
  fts5vocab(main, items_fts, row)` succeeds.
- `CREATE TEMP TABLE` and inserts into it also succeed.
- Only writes to `main` fail ("attempt to write a readonly database").

VERIFIED in this session. The correct guarantee is narrower: **the read tier
cannot persist**. It is not "cannot write". This also makes rev 2's
`items_vocab` migration unnecessary, because a per-call temp `fts5vocab`
works.

### 2.7 The MCP surface

- `_PROTOCOL_VERSIONS` tops out at `2025-06-18` (`mcp_server.py:65`). VERIFIED.
- Results are text-only (`:1054`, `:1078`, `:1137`). VERIFIED.
- No tool has `outputSchema` or `annotations`. VERIFIED.
- There is no `churn` or `export-graph` tool. VERIFIED.

**CORRECTED (rev 3).** `outputSchema`, `structuredContent`, and annotations are
already part of **2025-06-18**, which the server already advertises, so no
version bump is needed to use them.

**The 2026-07-28 revision.** The critic read the changelog from the
modelcontextprotocol GitHub repo. VERIFIED. It:

- removes initialize and sessions, and adds `server/discover`;
- **deprecates Roots**;
- replaces server-initiated requests with `InputRequiredResult`, which
  invalidates `_roots_slugs`'s server-initiated `roots/list` (`mcp_server.py:258+`);
- requires `resultType` on every result;
- removes `ping` and `notifications/roots/list_changed`.

### 2.8 Smaller gaps

| Gap | Where |
|---|---|
| `churn` counts unmerged PRs | `query.py:413-426` |
| `open` mixes issues and PRs and returns no labels, assignees, or linkage | `query.py:429-450` |
| GraphQL comment nodes select no `authorAssociation`, and `comments` has no such column | `graphql.py:57-69` |
| The query travels in argv; `gh api graphql --input -` and `-F query=@-` both read stdin (VERIFIED: `gh api --help`, gh 2.89.0) | `graphql.py:304` |

**Review-only changes need `--force`.** INFERENCE: the README asserts it, and
the code only shows that the listing filters on issue `updated_at`
(`github.py:166`).

---

## 3. Owner decision (unchanged)

Bundling ranks issues, and `AGENTS.md` forbids ranking in the sync path and by
collectors. The plan adds a **derived read tier** that works under these rules:

- **Computed at query time, never persisted.** Temp objects on the `mode=ro`
  connection are allowed, but nothing is written to `main` (§2.6).
- **Deterministic.** Given the same DB, config, and vocabulary source, it
  returns byte-identical output. The vocabulary source and any git HEAD sha
  are echoed in `data`.
- **Explainable.** It returns per-signal components, the raw evidence behind
  them, and the weights used.
- **Labeled.** Output carries `data.derived = true`, and is described as a
  heuristic rank or affinity, never as severity or priority.

**Amendments needed.** `AGENTS.md` needs one sentence: *"The derived read tier
may score and group at query time; it may never persist, and its output is a
heuristic, not evidence."* The collector rule is amended to match.

**If rejected.** Workstream B reduces to raw feature exports.

---

## 4. Workstream A: ingest (EXTRACTED only)

### A0: linear ingest plus migration v7, part 1

This covers §2.1. Migration v7 creates the `items(repo, title)` index **inside
the migration**, not in `schema.sql`.

- **Why the migration and not `schema.sql`.** `init_schema` runs
  `executescript(schema.sql)` before the migration chain (`db.py:614`). It is
  the same trap that the v6 partial index documents (`schema.sql:113-118`,
  `db.py:546-550`).
- **Read paths.** A0 also rewrites the O(E) read paths from §2.2.

### A0b: GraphQL over stdin

This is an independent, low-risk change. The body becomes
`{"query": ...}` and is sent via `--input -`. It removes the argv ceiling
before any fields are added. `_MAX_QUERY_CHARS` stays as a size guard.

### A1: payload columns from REST data already stored (migration v7, part 2)

**Columns on `items`.** All are nullable and filled by `upsert_item_row` from
the merged payload:

| Column(s) | Source field |
|---|---|
| `reactions_total`, `reactions_plus1` | `reactions` |
| `comments_count` | `comments` |
| `author_association` | — |
| `milestone_title`, `milestone_due_on` | `milestone` |
| `issue_type` | `type.name` |
| `sub_issues_total`, `sub_issues_completed` | `sub_issues_summary` |
| `blocked_by_open`, `blocking_open`, `blocked_by_total`, `blocking_total` | `issue_dependencies_summary` |

The four dependency fields map one-to-one, so open and total are never
conflated.

**`issue_field_values`** gets a child table:
`issue_field_values(repo, number, field, value)`.

**Assignees** get a child table: `assignees(repo, number, login)`.

Both child tables are replaced in `replace_item_children`
(`store.py:63-67`).

**Backfill.**

- The migration parses `raw_json` in Python. JSON1 is built in only from
  SQLite 3.38, and the floor is 3.35 (`db.py:21`).
- The backfill UPDATE fires `items_au` for every row, which rebuilds the FTS
  index for every item. Either narrow the trigger to `AFTER UPDATE OF title,
  body, labels_text`, or accept a one-time FTS rebuild and document it.
- Fresh-database and migrated column order must match, for the parity tests.

**CORRECTED (rev 3).** Rev 2's `fields_partial` flag is dropped. PR rows lack
the issue-only keys legitimately, so the flag would be set on almost every
corpus. NULL plus `raw_json` is enough.

**Comment author association.** Add the `author_association` column on
`comments`. On REST it comes from the comment payload; on GraphQL, add
`authorAssociation` to `_ISSUE_COMMENT_NODES` and to the review-comment
selection. These are additive fields, and they must go through the A2
capability gate.

**Source-specific columns.** `upsert_item_row` replaces the whole row on
conflict (`store.py:161-180`). A column that only one source provides must
`COALESCE(excluded.col, items.col)`, so a REST re-ingest does not erase
GraphQL-only data.

**Gate before A2 lands.** A1 makes `blocked_by_open` available to triage's
"blocked" gate.

### A2: GraphQL link fields, behind a capability gate

**1. Capability probe (new in rev 3; it blocks everything else in A2).**
One `__type(name: "Issue") { fields { name args { name } } }` query, plus the
same for `PullRequest`, cached in `meta` together with the gh version and
host. The selection is built only from the fields that are present. A
field-level GraphQL error then drops that section, marks it absent, and logs
to `fetch_log`. It never fails the run. This protects GHES and
restricted-permission repos, and it survives upstream removals.

**2. Fields.** All are owner-side and direct, and none of them come from the
timeline:

| Field | Becomes |
|---|---|
| `Issue.blockedBy(first: 50) { totalCount pageInfo{hasNextPage} nodes{ number repository{nameWithOwner} } }` | `blocked_by` |
| `Issue.parent` | `child_of` |
| `Issue.duplicateOf` | `duplicate_of` |
| `Issue.issueType` | — |
| `Issue.closedByPullRequestsReferences(first: 20, includeClosedPrs: false)` | open linked PRs |
| `PullRequest.statusCheckRollup { state }`, `reviewDecision` | read-time data, below |

INFERENCE: the argument names on `closedByPullRequestsReferences` must be
checked against the official schema file before use.

**CORRECTED (rev 3).** Rev 2's duplicate design is withdrawn: the
`MARKED_AS_DUPLICATE_EVENT` / `unmarked_duplicate_of` timeline scheme. It made
evidence timestamps carry meaning, which breaks the AGENTS rule that evidence
is a payload. It could also fall outside the newest-500 timeline window, and
its orientation on the canonical item's timeline was never verified.
`Issue.duplicateOf` replaces it. The only timeline addition kept is
`REOPENED_EVENT`.

**3. Presence-gated `source='link'` stream. This is new plumbing; it does not
reuse an existing contract.**

- `_resolve_children` gains a separate `links: dict | None` return value.
  It is `None` whenever the section was not fetched: a REST source, an
  unresolved node, the 404 path, or a capability-dropped field.
- The jsonl writer handles `None` explicitly. It must not iterate the value
  the way it iterates `timeline` (`sync.py:550`).
- `delete_owned_edges` exempts `link`.
- `rebuild_edges` deletes and reinserts this item's outbound `link` rows
  **only when `links` is not None AND the connection is complete**.
- An overflowed connection (more than 50 blockers) is treated as
  **union-only**, never delete, and is logged to `fetch_log`. Rev 2's
  delete-then-insert would drop blockers beyond 50 on every re-ingest.

**4. Relations.** Each uses `src = the ingesting item`:

| Relation | Source |
|---|---|
| `blocked_by` | `link` |
| `child_of` | `link` |
| `duplicate_of` | `link` |

Every relation is registered in `extract.RELATIONS` (`extract.py:253`) and in
`query.STRUCTURAL` (`query.py:458`), and has a row in the Relationships table
in `docs/schema.md`. `tests/test_relation_registry.py` enforces all three.
Cross-repo endpoints stay repo-qualified (`owner/repo#N`).

**5. `item_events(repo, number, type, created_at, actor)`** holds `reopened`
events. It is presence-gated the same way: replaced only when the timeline
section was fetched, so it is never zeroed by a degraded re-ingest.

**6. Volatile PR state is not stored (CORRECTED, rev 3).** Rev 2 stored
`mergeable`, `checks_state`, `review_decision`, and `reviewRequests`. These
change without bumping issue `updated_at` (INFERENCE: confirm with one live
reaction toggle and one CI run), so incremental sync would leave them stale.
`mergeable` is also often `UNKNOWN`. Instead, `brief` and `bundle` report them
as **read-time optional live data**: one bounded GraphQL call when `--live` is
passed, labeled as live, never persisted. Reactions stay stored, because the
listing carries them, and their staleness is documented.

### A3: raw path and signature extraction (child tables, no resolution at ingest)

**`item_path_refs(repo, number, raw_path, line, origin, evidence)`** is
replaced on re-ingest. `origin` is one of `body`, `issue_comment:<id>`,
`review_comment:<id>`, or `review:<id>`.

**CORRECTED (rev 3).** Rev 2's `comment:<id>` was ambiguous, because the two
comment kinds use separate id spaces (`schema.sql` `UNIQUE(repo, kind,
github_id)`).

These patterns are extracted:

- slash-path tokens;
- Python `File "...", line N`;
- `at fn (path:L:C)` and `path:L[:C]`;
- same-repo `blob/<ref>/<path>` URLs. Foreign URLs are dropped.

**`item_signatures(repo, number, kind, value)`** covers exception class
names, masked first error lines, and frame function names.

All of these writes stay inside `ingest_item`'s transaction.

**Additional cheap linkage (new in rev 3; all EXTRACTED; no new fetches).** A
PR's `head_ref` matching `(^|/)(issue-?|gh-?|fix/|feat/)?(\d+)\b` produces a
`branch_ref` relation, item → item, `source='keyword'`. The pattern set is
configurable, and an ambiguous match produces nothing. `head_ref` is already
stored. Commit-message references are deferred, because they need the PR's
commit list.

---

## 5. Workstream B: derived read tier (`src/zaxbygraph/derive.py`)

Every function in this tier:

- runs on the `mode=ro` connection;
- checks `PRAGMA user_version`;
- degrades with `index_stale` when an expected table is missing;
- echoes `weights`, `config_source`, and `vocab_source`.

All output carries `derived: true` inside `data`.

**Untrusted content (new in rev 3).** Every field that quotes issue or comment
text is wrapped as `{"untrusted_text": ...}`. The payload carries
`data.content_trust = "untrusted: GitHub user content"`, so agent hosts can
delimit it against prompt injection.

### 5.1 Path vocabulary and resolution

**Vocabulary.** The base vocabulary is the union of `pr_files.path` from
**merged** PRs, minus paths whose latest merged-PR status is `removed`.
`git ls-files` is an **opt-in** addition (`--vocab git`). It runs only when the
cwd is a checkout of the same slug, and its HEAD sha is echoed back, which
keeps output deterministic.

**Resolution.** A path resolves by longest-suffix match. A bare basename
resolves only when it is unique. Anything ambiguous is dropped and counted in
`unresolved_paths`.

INFERENCE: renames are partially invisible. GraphQL file nodes carry no
`previous_filename`, so a renamed file keeps both names in the vocabulary.

### 5.2 `similar N`

**Candidate generators.** All are bounded:

1. A temp `fts5vocab` more-like-this query: the top 12 terms of the item, top
   50 hits.
2. A signature join.
3. A resolved-path overlap, capped at 50 per path.
4. A graph neighbourhood up to 2 hops, using the indexed lookups from A0.

**Signals.**

| Signal | Computation |
|---|---|
| `lexical` | bm25 normalized by the self-match score |
| `title` | bigram Jaccard |
| `signature` | count of shared signatures |
| `files_direct` | IDF-weighted Jaccard over direct and linked files |
| `files_analog` | the same over analog files, reported separately |
| `cochange` | max lift; merged PRs touching 2–50 files, support ≥ 2 |
| `structural` | 1/hops over structural relations |
| `context` | shared parent, milestone, and IDF-weighted labels |

**Payload.** Each candidate returns its components, its evidence (shared
paths, terms, and signatures, plus the route), and `score`.

### 5.3 `coupling PATH`

Co-change support, confidence, and lift over merged PRs.

**CORRECTED (rev 3).** Rev 2's `--git` direct-push counting is cut. It added
cost and gave no measured benefit.

### 5.4 `triage`

Triage scores open **issues** (PRs excluded) in two stages, which bounds the
cost.

**Stage 1 (every open issue, cheap features):**

| Group | Features |
|---|---|
| Demand | `reactions_plus1`; distinct participants; `comments_count`; inbound `duplicate_of`; inbound distinct-item `mentions` |
| Leverage | `blocking_open`; sub-issue gap; milestone due date |
| Risk | reopen count; `reverts` on linked files |
| Priority | `issue_type`; label weights; issue-field values mapped via config |

**Stage 2 (only the top-K, default 50):** hotspot score of the implicated
files, from merged-PR churn weighted by recency, plus the regression flag
(analog-based).

**CORRECTED (rev 3).** Rev 2 computed analogs for every open issue, which
costs one more-like-this query per issue.

**Gates.** Each gate names the reason it fired:

| Gate | How it is evaluated |
|---|---|
| Blocked | `blocked_by_open > 0` (A1), or open `blocked_by` edges after A2 |
| Assigned | assignee present |
| Open linked PR | Prefer `closedByPullRequestsReferences` (A2, open only). Before A2: `closes`/`closes_keyword`/`branch_ref` edges from an **open** PR. |
| Locked | item is locked |

`closing_ref` edges are append-only, so an unlinked PR can still gate an
issue. That gate is labeled `"gate": "linked_pr (append-only evidence; may be
stale)"`.

**Output.** Triage is paginated (`limit`/`cursor`), and every row carries the
raw features.

### 5.5 `bundle`

**Pool.** The pool is the top-K triage results, or the seed items. Gating is
relaxed for **blockers inside the pool**: an item blocked only by another pool
member stays in. **CORRECTED (rev 3).** Rev 2 gated every blocked item out and
then derived an internal order from `blocked_by`, which made that order always
empty.

**Edge guard. CORRECTED (rev 3).** A pair gets an edge only when affinity is
above the floor **and** at least one *anchor* signal is non-zero. Anchor
signals are `signature`, `files_direct`, `structural`, and `cochange` on
direct files. `lexical`, `title`, `files_analog`, and `context` (labels,
milestone) can raise an existing edge's score, but can never create one.
Rev 2 counted analog files and shared labels as non-lexical, so two issues
could bundle purely because their wording overlapped.

**Selection.** Greedy and deterministic, with ties broken by item number:

1. Seed with the highest unused triage score.
2. Add the pool member that maximizes `triage(n) × mean_affinity`, subject to:
   - a cap on the file set (`--max-files`);
   - collapsing `duplicate_of` pairs;
   - flagging and penalizing collisions with open PR files (the same check as
     `pr_overlap`).

**Each bundle carries:**

- a stable `bundle_id`: the sha1 of the sorted member numbers and the config
  hash;
- its members, with features;
- the rationale for each pair;
- the file set, split into direct, linked, and analog (each analog tagged
  `via #M`);
- the analogs' fix PRs, with diffstat;
- the test files that historically co-changed with these files (test globs
  come from config);
- open-PR collisions;
- the internal order given by in-pool `blocked_by`;
- a risk summary;
- cohesion;
- `why_not_bigger`.

**Bounds.** Pool K ≤ 50 means at most 1,225 pairs. The adjacency is built
once. The output is token-bounded with `--budget-tokens`.

### 5.6 `brief N`

`brief` returns a work packet that fits a token budget (chars/4):

- the issue's features;
- its body, truncated at paragraph boundaries;
- top comments, ordered by `author_association` (maintainers first, A1),
  then by recency;
- implicated files, with evidence;
- the closest analog and its fix diffstat;
- linked PRs, with live CI and review state only under `--live`.

### 5.7 Config

Config is read from the first of these that exists:

1. `--config`;
2. `.zaxbygraph.toml` at the main worktree root, for a matching checkout;
3. `<store dir>/config.toml`.

It is parsed with `tomllib` and never stored.

### 5.8 Surface fixes

- `open` gains `--kind`, labels, assignees, and `linked_open_prs`.
- `churn` gains `--merged-only`.

---

## 6. Workstream C: evaluation

Every evaluation runs on a **leakage-controlled snapshot** built in temp
tables on the read connection. For each target item it excludes:

- its closing PRs, and every edge to or from them;
- every PR merged after the target was created, which removes them from file
  IDF, co-change, and the vocabulary;
- comments created after the **creation of the fix PR** (rev 2 used the
  target's `closed_at`);
- for the `similar` evaluation, every edge between the target and its
  ground-truth partner: the `mentions` or `cross_referenced` edges created by
  a "Duplicate of #N" comment.

Text is also masked: `#N`, same-repo URLs, and 7–40 hex SHAs.

**Residual bias.** Body edits made after close cannot be undone, because only
the current body is stored. This bias is reported in every result. A result
whose controls cannot be applied is labeled `with_leakage: true`.

| Evaluation | Ground truth | Metric |
|---|---|---|
| `similar` | `duplicate_of` pairs (A2) | recall@5, MRR |
| `bundle` | issue sets closed by one merged PR that closes 2–5 issues; the linked file source is excluded | pair recall, proposed-pair precision |
| `localize` | the fix PR's `touches` vs. direct and analog sources only | file recall@k |

**Fixtures.**

- **Synthetic.** A fixture corpus built with the `tests/fixtures.py` helpers,
  containing planted duplicates, co-fixed sets, and stack traces. Score floors
  for it are asserted in CI.
- **Recorded real corpus (new in rev 3).** A recorded snapshot of a public
  repo's REST listing and GraphQL children, committed as fixture JSON (no live
  `gh` calls in checks, per CONTRIBUTING). It keeps the score floors from
  being tuned to the generator.

---

## 7. Workstream D: agent surface

**Use 2025-06-18 features now; no version bump is needed.**

- Add `structuredContent` (the envelope object) next to the existing text
  block.
- Add an `outputSchema` to each tool, with `additionalProperties: true`.
- Add annotations: `readOnlyHint: true` on every tool except `sync`.

**New tools.** Keep the tool count lean:

| Tool | Notes |
|---|---|
| `similar_items` | — |
| `triage` | — |
| `bundle` | — |
| `brief` | — |
| `churn` | currently missing |

**CORRECTED (rev 3).** `coupling` stays CLI-only, so rev 2's list of six MCP
tools drops to five.

**Version 2025-11-25.** Add it to `_PROTOCOL_VERSIONS` once its deltas have
been reviewed against this server.

**Version 2026-07-28** gets its own workstream:

- resolve the repo without Roots (an explicit argument or the server pin;
  where input is needed, `InputRequiredResult`);
- add `server/discover`;
- add `resultType` on every result;
- keep tool order deterministic;
- drop the `ping` and roots-notification paths.

**Skill.** Add a recipe:

1. Check `status`.
2. Run `triage`.
3. Run `bundle --from triage:30 --size 2-3 --count 3 --briefs`.
4. The lead verifies each rationale against the code before assigning work.

---

## 8. Sequencing (corrected)

Each row is one workstream issue and one PR, with EARS acceptance criteria.
The regression test lands first.

| # | PR | Depends on | Delivers |
|---|---|---|---|
| 1 | **A0**: linear ingest (T1–T3), indexed read paths, v7 title index | — | Cold sync and reads scale |
| 2 | **A0b**: GraphQL over stdin | — | Removes the argv ceiling |
| 3 | **A1**: v7 columns, `assignees`, `issue_field_values`, comment `author_association`, Python backfill, COALESCE on source-specific columns | 1 | Features and gates from stored REST data |
| 4 | **A3**: `item_path_refs`, `item_signatures`, `branch_ref` | 3 | Issue→file and issue↔PR evidence |
| 5 | **B**: vocabulary + `similar` + `coupling`; **C**: fixtures + `eval localize` / `eval bundle` (pre-A2 ground truths) | 4 | First measured relatedness |
| 6 | **B**: `triage` (two-stage) + config + `open`/`churn` fixes | 3, 5 | Ranked, workable pool |
| 7 | **B**: `bundle`, `brief` | 5, 6 | The requested capability |
| 8 | **A2**: capability probe, link stream, `duplicateOf`/`blockedBy`/`parent`, `closedByPullRequestsReferences`, `REOPENED_EVENT`; then **C**: `eval similar` | 2, 3, eval harness | Dependency, duplicate, and reopen signals |
| 9 | **D**: `structuredContent`/`outputSchema`/annotations, the new tools, 2025-11-25 | 7 | Agent surface |
| 10 | **D**: MCP 2026-07-28 | 9 | Spec currency |
| 11 | Follow-ups: review-only drift detection; Projects v2 (opt-in, `read:project`); CODEOWNERS on bundles; PR commit-message references | — | — |

**CORRECTED (rev 3).** Rev 2's ordering had three errors, now fixed:

- `eval similar` was listed before its ground truth (A2) existed.
- `triage` was listed as depending only on A1.
- The stdin transport was bundled into the riskiest PR.

Rows 1–7 deliver measured bundling without changing the GraphQL selection.

---

## 9. Invariant ledger

| Invariant | How it holds |
|---|---|
| EXTRACTED only | New edges come from GraphQL fields or from deterministic patterns. Raw extractions live in child tables. The derived tier never persists anything (`mode=ro`; only temp objects). |
| Node types | Unchanged. |
| Ownership | The `link` stream uses `src` = owner. It is replaced only when the section is fetched and complete; otherwise it is union-only. `timeline` and `closing_ref` stay append-only. Inbound edges are untouched. |
| One item, one transaction | Every new write sits inside `ingest_item`. Counts move to read time or to deltas. |
| Idempotence | Child tables are replaced inside the item transaction. `link` upserts on the existing unique key. |
| Watermark | Untouched. |
| Read-only `sql` | Untouched. |
| SQLite floor | 3.35 still holds: `fts5vocab` predates it, and backfill uses Python, not JSON1. |
| Storage faults are reported | The capability probe turns field errors into logged section absences. Real faults still record `last_error`. |
| Portability | `git` is opt-in and guarded. Every new test runs on the Windows leg. |

## Sources

- GitHub GraphQL official schema (`github/docs`, `src/graphql/data/fpt/schema.docs.graphql`), read by the critic pass.
- GitHub REST issue dependencies — https://docs.github.com/en/rest/issues/issue-dependencies
- GitHub REST sub-issues — https://docs.github.com/en/rest/issues/sub-issues
- Issue fields GA (2026-07-02) — https://github.blog/changelog/2026-07-02-issue-fields-are-now-generally-available
- MCP 2026-07-28 changelog — https://modelcontextprotocol.io/specification/2026-07-28/changelog
- Duplicate detection at scale, lexical vs learned — https://arxiv.org/abs/2504.14797 (not re-verified in rev 3; arXiv was unreachable from the sandbox)
- RepoMem, history-informed localization — https://arxiv.org/abs/2510.01003 (not re-verified in rev 3)
