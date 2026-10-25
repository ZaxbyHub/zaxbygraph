# ZaxbyGraph: state-of-the-art gap review and the bundling plan (rev 2)

## About this revision

**Status.** This is a proposal. Nothing in it is implemented.

**Anchor.** All code claims were re-checked against `main` at
`b47913e4` (schema v6, 310 tests green).

**What rev 2 changes.** Rev 1 was written from the docs, the schema and partial
reads of the code. Rev 2 follows a full read of `store.py`, `extract.py`,
`sync.py`, `graphql.py`, `github.py`, `query.py`, `db.py`, the MCP dispatch and
tool registry, the CLI read path, the relation-registry test, the fixtures and
`CONTRIBUTING.md`. It adds measurements on a synthetic corpus. Corrections to
rev 1 are kept inline as **CORRECTED (rev 2)**, following the CONTRIBUTING
evidence rules.

**Epistemic labels.**

| Label | Meaning |
|-------|---------|
| VERIFIED | Re-checked in code at the anchor, or measured. The method is stated. |
| INFERENCE | Motivating but unproven. It needs the named check before anyone builds on it. |

---

## 1. Verdict

The ingest core is strong and does not need redesign:

- per-stream edge provenance
- idempotent per-item transactions with an inclusive verbatim watermark
- a section-presence contract that distinguishes "source did not answer" from
  "empty", via `None` vs `[]` in `sync._resolve_children`
- a two-layer read-only SQL guard
- one envelope for every surface

The tool falls short of first class in three places:

- **One correctness/scale defect on the ingest path.** Per-item ingest cost
  grows linearly with corpus size, so a cold sync is quadratic (§2.1).
- **The signal for selecting work is missing.** Open issues have no file
  linkage. Demand, ownership and blocking signals are unmodeled.
- **There is no relatedness primitive and no way to measure one.** No command
  answers "items like #N" or "which 2–3 issues belong together", and nothing
  can measure whether such an answer is any good.

---

## 2. Verified findings

### 2.1 Cold sync is quadratic, driven by one unindexed DELETE (new in rev 2)

**Mechanism.** VERIFIED by `EXPLAIN QUERY PLAN` on a fresh v6 database.
`delete_owned_edges` runs this second DELETE on every item ingest
(`src/zaxbygraph/store.py:89-92`):

```python
conn.execute(
    "DELETE FROM edges WHERE repo = ? AND dst_type = 'item' AND dst_id = ? "
    "AND rel IN ('authored', 'commented', 'reviewed')",
```

SQLite's plan for it is
`SEARCH edges USING INDEX sqlite_autoindex_edges_1 (repo=?)`, which scans
every edge of the repo. It never uses `idx_edges_dst (repo, dst_type, dst_id)`.
No `ANALYZE` statistics exist, and the planner picks the unique autoindex on its
`repo` prefix.

**Measurement.** VERIFIED. The synthetic corpus has each item mentioning the 20
previous items: about 22 edges per item and 88k edges at 4,000 items. Single
calls at that size:

| Call | Time per call |
|------|---------------|
| `delete_owned_edges` | 11.7 ms |
| `recount` (`store.py:443`, called per item at `store.py:632`) | 3.6 ms |
| the title lookups at `store.py:363`/`:403` | 0.9 ms |

The 2,000-item batches took 12.3 s, 32.0 s, 51.4 s and 69.1 s at cumulative
2k, 4k, 6k and 8k items. Disabling `recount` and the title lookups still left
batch time growing (9.4 s, 23.1 s, 34.0 s). That isolates the DELETE as the
dominant term. The scripts were run outside the repo; method: `cProfile` plus
`EXPLAIN QUERY PLAN`.

**Fix.** Pin the access path. Either use `INDEXED BY idx_edges_dst`, or split
the statement into three equality deletes and confirm the plan. Then move
`recount` out of the per-item transaction to once per listing page and at
`mark_sync_finished`. `status` reads those counts, so per-page granularity stays
truthful at page boundaries.

**Regression test.** Assert the `EXPLAIN QUERY PLAN` text names
`idx_edges_dst`. Also assert the batch-time ratio stays flat on a 2k-to-4k
fixture, with a generous bound so it is not flaky.

**This is step 0 of the plan.** It is the only finding here that defeats the
tool's core purpose on large repos.

### 2.2 Issues have no file linkage

VERIFIED. `touches` is emitted only from PR files
(`extract.edges_from_files`, `extract.py:173-179`). Every other item-to-file
path goes through a closing PR. Open issues, the ones that need bundling,
therefore reach no file at all.

### 2.3 Demand, ownership and type signals are not modeled

VERIFIED from the schema and `store.upsert_item_row` (`store.py:148-211`).
There is no column, child table or edge for any of these:

- assignees, milestone, reactions, comment count (the scalar)
- `author_association`, issue type
- sub-issues, issue dependencies

`raw_json` holds the **merged** listing plus pull payload: `ingest_item`
copies every pull key not in `list_keys` (`store.py:587-610`). Whatever the
REST issues listing returns is therefore already on disk.

Two claims here are INFERENCE and must be checked against a live corpus before
the migration is written:

- The REST listing includes `reactions`, `assignees`, `milestone`,
  `author_association`, `type`, `sub_issues_summary` and
  `issue_dependencies_summary`. The fixtures model none of these
  (`tests/fixtures.py:40-75`).
- The check is:

  ```bash
  zaxbygraph sql "SELECT number, json_type(raw_json,'$.reactions'), json_type(raw_json,'$.assignees'), json_type(raw_json,'$.milestone'), json_type(raw_json,'$.type'), json_type(raw_json,'$.sub_issues_summary'), json_type(raw_json,'$.issue_dependencies_summary') FROM items ORDER BY updated_at DESC LIMIT 5"
  ```

### 2.4 The timeline captures two event types only

VERIFIED. Both the page query and the continuation selection request
`itemTypes: [CLOSED_EVENT, CROSS_REFERENCED_EVENT]` (`graphql.py:141`,
`graphql.py:280`). Any other typename normalizes to a bare `{"type": ...}` and
yields no edge (`graphql.py:272`). Issue nodes request only `databaseId`,
comments and timeline (`_ISSUE_FIELDS`, `graphql.py:170-174`). There is no
`issueType`, `parent`, `subIssues`, `blockedBy`, `stateReason` or
`reactionGroups`.

### 2.5 Read commands run on `mode=ro` connections, and only `sql` has the authorizer

VERIFIED. `open_existing` opens `file:…?mode=ro` with no authorizer
(`db.py:121-137`). The default-deny authorizer is attached only by
`connect_readonly_query`, which serves `sql` alone (`db.py:100-118`). Reads
never migrate.

Consequences for the derived tier:

1. It can never create a table, an `fts5vocab` table included. Anything it
   needs must come from a migration.
2. Every new read must degrade gracefully on a v6 database. The existing
   pattern is `index_stale` (`query._edges_source_available`, `query.py:273`).

### 2.6 The MCP surface lags the spec and the CLI

VERIFIED.

- `_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")`
  (`mcp_server.py:65`).
- Tool results carry only a text block (`mcp_server.py:1137`). No tool has
  `outputSchema` or `annotations`.
- `churn` and `export-graph` have no MCP tool. The registry is at
  `mcp_server.py:815-905`.

The current revisions are 2025-11-25 and 2026-07-28. The 2026-07-28 revision
removes the initialize handshake and protocol sessions and adds
`server/discover`; this comes from the official changelog title and summary,
not a full read of the spec.

### 2.7 Smaller verified gaps

| Gap | Evidence |
|-----|----------|
| `churn` counts every PR, unmerged and closed-unmerged included, so it is not a defect-hotspot signal as is. | `query.py:413-426` |
| `open` returns no labels, assignees or linkage. | `query.py:429-450` |
| `path_between` loads every structural edge of the repo into Python on each call, O(E) per call. A bundler must not call it per candidate pair. | `query.py:503-506` |
| The GraphQL query travels in argv (`-f query=…`), and chunk size is bounded by `_MAX_QUERY_CHARS` for the Windows command-line limit. Every field added shrinks chunks and adds calls. `gh api --input -` (stdin request body) is available (VERIFIED via `gh api --help`) and removes the ceiling. | `graphql.py:304`, `graphql.py:512-521` |
| Review-only PR changes need `--force`. The listing is REST `since=` on issue `updated_at`. | `github.py:166`, README |

### 2.8 Rev 1 claims withdrawn or corrected

- **CORRECTED (rev 2): `fts5vocab` from the read path.** Rev 1 had
  `derive.py` build an `fts5vocab` table at read time. That is impossible
  under §2.5. It is now created by migration v7. VERIFIED that `fts5vocab`
  works over the porter `items_fts` on SQLite 3.45. It has shipped with FTS5
  since 3.11, so there is no floor change.
- **CORRECTED (rev 2): `json_extract` in the migration.** Rev 1 backfilled
  with `json_extract`. JSON functions are built in only from SQLite 3.38, and
  the floor is 3.35 (`db.py:21`). The backfill now parses `raw_json` in
  Python, so it has no SQLite dependency.
- **CORRECTED (rev 2): `mentions_file` edges.** Rev 1 made issue-to-file
  links into item-owned `mentions_file` edges, re-derived after sync.
  Resolution depends on a path vocabulary that changes as later PRs ingest.
  Edges would be order-dependent, or need a re-derivation pass that writes
  edges outside the owning item's transaction. Both break the ownership
  model in `delete_owned_edges`. Rev 2 stores the **raw extracted path
  strings** in a child table and resolves them at read time (§4.3).
- **CORRECTED (rev 2): edges for dependencies, parents and duplicates.**
  Rev 1 proposed `blocks` with `src = the blocker`, under source `payload`.
  Two things break:
  - **Ownership.** `delete_owned_edges` deletes by `src_id` = the ingesting
    item (`store.py:84-88`). A `blocks` edge learned from issue X's
    `blockedBy` list has `src = Y`, so X can never retract it.
  - **Degraded ingest.** Under `source='payload'`, a degraded re-ingest (no
    GraphQL section, so the REST fallback runs) deletes the edge and cannot
    rebuild it.

  Rev 2 stores these from the owner's side under a new presence-gated
  stream (§4.2).
- **CORRECTED (rev 2): `CONNECTED_EVENT` dropped.** The INFERENCE behind it:
  Development-sidebar links already surface through
  `closingIssuesReferences`, which the tool ingests as `closes` /
  `closing_ref`. Check before relying on it: link an issue manually to a
  test PR and confirm it appears in that connection.
- **CORRECTED (rev 2): `derived` moves into `data`.** Rev 1 put
  `"derived": true` on the envelope root. The root shape is the documented
  "one envelope" contract (`build_envelope`, `cli.py:175-201`, issue #3).
  `tests/test_cli_envelope.py:187` asserts the six required keys; it does
  not forbid extra ones, so this is a contract choice rather than a test
  constraint. The marker now goes inside `data`.

---

## 3. Design decision required from the owner

`AGENTS.md` forbids clustering and ranking in the sync path, and says
collectors do not rank. Bundling is ranking. This plan adds a **derived read
tier** under these rules:

- **Computed, never stored.** It runs at query time from stored rows. It is
  never written to `edges`, `items` or any table, and never feeds sync. It
  cannot write anyway, because it reads on `mode=ro` connections (§2.5).
- **Deterministic.** No model calls. Ties break by item number, and the same
  DB plus the same config always gives byte-identical output.
- **Explainable.** Each score ships with its per-signal components, the raw
  evidence (shared paths, shared stemmed terms, the graph route) and the
  weights in effect.
- **Labeled.** Output carries `data.derived = true`, and the wording is
  always "heuristic rank" or "affinity", never "severity" or "priority".

The required `AGENTS.md` amendment: "The derived read tier may score and group
at query time; it may never persist, and its output is a heuristic, not
evidence." The collector rule changes too: collectors may run `similar`,
`triage` and `bundle` and paste the output, but may not add judgment of their
own.

If this is rejected, Workstream B shrinks to raw feature exports. Workstreams
A, C and D stand alone.

---

## 4. Workstream A: ingest (sync path, EXTRACTED only)

### 4.0 PR A0: fix the quadratic ingest (§2.1)

This PR has no schema change. The test requirements are in §2.1.

### 4.1 PR A1: migration v7, payload columns plus child tables (no GraphQL change)

**New columns on `items`.** All are nullable and populated from the merged
payload by `upsert_item_row`:

| Columns | Source fields |
|---------|---------------|
| `reactions_total`, `reactions_plus1` | `reactions.total_count`, `reactions["+1"]` |
| `comments_count` | `comments` |
| `author_association` | `author_association` |
| `milestone_title`, `milestone_due_on` | `milestone` |
| `issue_type` | `type.name` |
| `sub_issues_total`, `sub_issues_completed` | `sub_issues_summary` |
| `blocked_by_count`, `blocking_count` | `issue_dependencies_summary` |

The field names are INFERENCE per §2.3.

**New child table** `assignees(repo, number, login, PRIMARY KEY(repo, number, login))`.
It is replaced in `replace_item_children` (`store.py:63-67`), like labels.

**Backfill.** Migration v7 iterates `SELECT id, raw_json FROM items` and parses
each row with `json.loads`. That is O(items) in a single transaction, the same
shape as the v2→v3 FTS rebuild. A field absent from an old payload stays NULL.

**Partial-field marker.** `sync_state.fields_partial` is set when any backfilled
row lacked a key, and cleared by the next `--force`. The `status` docs gain the
field.

**The vocab table.** v7 also creates
`items_vocab USING fts5vocab(items_fts, 'row')` for §5.1.

**Migration test duties.** v7 must:

- extend `tests/test_migrations.py`'s chain tests and the fresh-vs-migrated
  parity tests;
- keep the forward-only, per-migration transaction contract (`db.py:605-626`);
- bump `CURRENT_USER_VERSION`, `SCHEMA_VERSION`, `schema.sql`,
  `schema_notes.py` and `docs/schema.md` together.

`test_docs.py` and the schema-notes tests pin these.

### 4.2 PR A2: GraphQL link sections (presence-gated stream)

**Selection changes.**

- `_ISSUE_FIELDS` (`graphql.py:170`) gains these. The field names are
  INFERENCE; validate them with one recorded live smoke, stored as a
  fixture, per CONTRIBUTING "Upstream fragility":
  - `issueType { name }`
  - `parent { number repository { nameWithOwner } }`
  - `blockedBy(first: 50) { totalCount pageInfo { hasNextPage } nodes { number repository { nameWithOwner } } }`
  - `stateReason(enableDuplicate: true)`
- Both timeline selections gain `REOPENED_EVENT`,
  `MARKED_AS_DUPLICATE_EVENT` and `UNMARKED_AS_DUPLICATE_EVENT`.
  `MarkedAsDuplicateEvent` selects
  `canonical { ... on Issue { number repository { nameWithOwner } } ... on PullRequest { number repository { nameWithOwner } } }`.
  `_timeline_event` (`graphql.py:229`) gets branches for each new type.
- `_PULL_SCALARS` gains `reviewDecision`, `mergeable`,
  `reviewRequests(first: 20) { totalCount }` and
  `commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }`.
  These ride the existing pull dict (`graphql.py:650-668`) into `raw_json` via
  the pull-merge, and A1 adds the matching columns (`review_decision`,
  `mergeable`, `checks_state`). INFERENCE: `commits(last:1)` may
  conflict with the existing `commits { totalCount }` alias. Alias it.

**Transport change.** Move the query to stdin: `gh api graphql --input -` with
body `{"query": …}`. This removes the argv ceiling that the new fields would
otherwise tighten. The `_MAX_QUERY_CHARS` chunking stays as a size guard, with a
higher bound justified by a measured GitHub query-cost check (INFERENCE: the
node-limit and cost ceilings become the binding constraint).

**New edge stream: `source='link'`, presence-gated.** This mirrors how
`timeline=None` already means "the source could not provide it"
(`store.ingest_item` docstring, `store.py:581-585`).

- `delete_owned_edges` excludes `link` along with `timeline` and
  `closing_ref`.
- `rebuild_edges` gains a `links: dict | None` argument. When `links` is
  not `None` (the section was fetched), it first deletes this item's
  outbound `source='link'` rows, then inserts the current set. When it is
  `None` (REST source, a degraded node, the 404 path), link rows are left
  untouched.

This gives retraction when upstream removes a dependency, with no loss on a
degraded re-ingest. Existing streams keep their semantics.

**Relations.** Each is stored from the owner's side, `src = the ingesting
item`, so ownership holds:

| rel | src → dst | source | Origin | `path` |
|-----|-----------|--------|--------|--------|
| `blocked_by` | item → item | `link` | `blockedBy` nodes | structural |
| `child_of` | item → item | `link` | `parent` | structural |
| `duplicate_of` | item → item | `timeline` | latest Marked/Unmarked pair resolves to marked | structural |

Each relation is added to `extract.RELATIONS` (`extract.py:253`) and to
`query.STRUCTURAL` (`query.py:458`). The Relationships table in
`docs/schema.md` gains the rows; `tests/test_relation_registry.py` enforces
this. Cross-repo endpoints stay repo-qualified, `owner/repo#N`, the same as
cross-references (`extract.py:330-341`).

`duplicate_of` stays in the append-only `timeline` stream. An unmark is
recorded as `rel='unmarked_duplicate_of'`, and readers take the later evidence
timestamp. That keeps the timeline invariant intact: never retract, always
append.

**Child tables.** `item_events(repo, number, type, created_at, actor)` holds
`reopened` events and is presence-gated the same way. `reopened_count` is
derived at read time; it is never a column. REST has no reopen data, so it
must not be zeroed on a degraded re-ingest.

**Truncation.** `blockedBy` connections that overflow log to `fetch_log`, the
same way as `closing_refs_incomplete` (`sync.py:510-524`).

### 4.3 PR A3: path and signature extraction (child tables, raw strings)

**`item_path_refs(repo, number, raw_path, line, origin, evidence)`.** It is
replaced on re-ingest. `origin` is one of `body`, `comment:<id>` or
`review:<id>`. Extraction is purely lexical, over bodies, comments and review
bodies with code fences included:

- slash-containing `seg/seg.ext` tokens
- Python `File "<path>", line N`
- `at fn (<path>:L:C)` and `<path>:L[:C]` frames
- GitHub `blob/<ref>/<path>` URLs, same repo only, dropped otherwise, like
  cross-repo keyword references

No resolution happens at ingest.

**`item_signatures(repo, number, kind, value)`.** It is replaced on
re-ingest. Kinds:

- `exception`: a `\b[A-Z]\w*(Error|Exception)\b` class name
- `error_line`: the first line inside a fence that matches
  `error|panic|fatal|traceback`, with hex, digits, quoted strings and paths
  masked
- `frame_fn`: function names from stack frames

The extraction is mechanical. The match key is exact.

Both tables live inside `ingest_item`'s transaction, so the one-item-one-
transaction invariant holds.

---

## 5. Workstream B: derived read tier (`src/zaxbygraph/derive.py`)

Every function takes the `mode=ro` connection and checks `PRAGMA user_version`
(already allowed). On a database below v7 it returns `index_stale: true`, with
only the signals the v6 schema supports. Weights come from config (§5.6) and
are echoed back.

### 5.1 `similar N [--limit 10] [--state open|closed|all]`

**Candidate generation.** Generators are bounded, and nothing does all-pairs:

1. **Lexical more-like-this.** Take N's top 12 stemmed terms by tf·idf.
   Term frequency comes from N's own title and body. Document frequency
   comes from `items_vocab`, with title terms counted twice. Then OR-query
   `items_fts` with the existing bm25 weights (`query.py:36`) and keep the
   top 50.
2. **Signature join** on `item_signatures`.
3. **Implicated-file overlap.** Items sharing any resolved path, capped at
   50 per path.
4. **Graph neighbours** within 2 structural hops. Adjacency is built once per
   call from the item's neighbourhood, never by calling `path_between` per
   pair.

**Signals** for each candidate:

| Signal | Definition |
|--------|------------|
| `lexical` | bm25 normalized by N's self-match score |
| `title` | Jaccard of word bigrams |
| `signature` | Count of shared exact signatures |
| `files` | IDF-weighted Jaccard over implicated file sets. File IDF is computed over merged PRs. `ignore_paths` is applied. |
| `cochange` | Max lift between the two file sets. File pairs come from merged PRs touching 2–50 files, with min support 2, computed only for the files involved. |
| `graph` | 1/hops over structural relations, plus shared `child_of` parent, shared milestone, and IDF-weighted shared labels |

**Implicated file set.** For an item it is:

- **direct:** `item_path_refs` resolved against the vocabulary;
- **linked:** `touches` of its `closes`/`closes_keyword` PRs;
- **analog:** `touches` of the fix PRs of its top-3 closed lexical or
  signature neighbours. These are tagged `via: #M` and down-weighted.

The vocabulary is the union of `pr_files.path` for the repo. When the read
runs inside a checkout whose origin resolves to the same slug, it also
includes `git ls-files` (read-only subprocess; `paths.py` already locates the
main worktree).

Resolution rules:

- Strip absolute prefixes by longest suffix match.
- A bare basename resolves only if it is unique in the vocabulary.
- An ambiguous path is dropped and counted in `unresolved_paths`.

**Payload.** Each candidate carries `number`, `title`, `state`, `score`,
`signals{…}` and `evidence{shared_paths, shared_terms, shared_signatures,
route}`. The payload also has `weights` and `derived: true`.

### 5.2 `coupling PATH [--limit 20] [--git]`

`coupling` returns the files most co-changed with PATH, with support,
confidence and lift. With `--git` it also counts direct-push commits from
local `git log --name-only --no-merges`, read-only. It also powers the
`cochange` signal.

### 5.3 `triage [--limit 20] [--type T] [--label L] [--include-claimed]`

`triage` covers open issues. Each row carries raw features, the gates that
fired, and a transparent score.

**Demand:**

- `reactions_plus1`
- distinct participants, from `authored`/`commented`/`reviewed` edges
- `comments_count`
- inbound `duplicate_of` count
- inbound `mentions` from distinct items

**Leverage:**

- open items it blocks, from inbound `blocked_by`
- sub-issue completion gap
- days to milestone due date

**Risk:**

- reopen count
- hotspot score of the implicated files, from **merged**-PR churn weighted by
  recency, not `churn` as is (§2.7)
- `reverts` touching those files
- a regression flag: a closed similar item whose fix touched the same files

**Explicit priority:**

- `issue_type`
- a label weight map from config

**Workability gates.** These exclude by default and name the reason:

- **open `blocked_by` exists**
- **an assignee exists**
- **an open PR links to it.** This is derivable **today** from
  `closes`/`closes_keyword` edges whose source PR is open.
- **the item is locked**

Age is reported but weighted 0 by default.

### 5.4 `bundle [--size 2-3] [--count 5] [--seed N…] [--from triage:K] [--max-files 15]`

**Pool.** The pool is the gated `triage` output, top K, or the given seeds.
Pairwise affinity is the `similar` composite, computed only within the pool
(K ≤ 50 means at most 1,225 pairs, with one adjacency build).

**Edge rule.** A pair forms an edge only when affinity exceeds the floor
**and** at least one non-lexical signal is non-zero. Shared vocabulary alone
never bundles.

**Selection.** Greedy, deterministic, ties broken by number:

1. Seed with the highest unused triage score.
2. Add the neighbour maximizing `triage(n) × mean_affinity(bundle, n)`, as
   long as these hold:
   - the union of implicated files stays within `--max-files`;
   - no member is blocked by an item outside the bundle;
   - `duplicate_of` pairs collapse into their canonical item;
   - open-PR file collisions (`pr_overlap` semantics) are flagged and
     penalized.
3. Stop at the size cap or the marginal-gain floor.

**Per-bundle payload:**

- members with triage features
- per-pair rationale
- the combined file set split into direct, linked and analog
- prior fix PRs for the analogs, with diffstat
- **historically co-changed test files**: `pr_files` paths matching the
  config test globs in the same merged PRs as the implicated files
- open-PR collisions
- an internal ordering derived from `blocked_by`
- a risk summary
- a cohesion score
- `why_not_bigger`: the next-best candidate and the constraint that excluded
  it

### 5.5 `brief N [--budget-tokens 2000]`

`brief` returns a token-budgeted packet. The budget uses a chars/4 estimate,
so it needs no tokenizer. Contents:

- triage features
- the body truncated at paragraph boundaries
- top comments, maintainer `author_association` first and then most recent
- implicated files with evidence
- the closest closed analog and its fix diffstat
- linked PRs with `review_decision` and `checks_state`

`bundle --briefs` inlines one brief per member.

### 5.6 Config

**Lookup order:**

1. `--config PATH`
2. `.zaxbygraph.toml` at the main-worktree root, when the read runs inside a
   matching checkout
3. `<store dir>/config.toml`, which MCP can always reach because it is
   slug-keyed

It is parsed with `tomllib`, stdlib on 3.11 (the floor). It is never stored in
the DB, and it is echoed back as `data.config_source` plus `data.weights`.

**Sections:**

- `[triage.labels]`
- `[paths] ignore = [...]`, `tests = [...]`
- `[weights]`
- `[bundle]`

### 5.7 Small surface fixes

- `open` gains `labels`, `assignees` and `linked_open_prs`.
- `churn` gains `--merged-only`.

---

## 6. Workstream C: evaluation

`zaxbygraph eval similar|bundle|localize` runs as a read-only command. Its
ground truth already sits in the graph:

| Evaluation | Ground truth | Metrics |
|------------|--------------|---------|
| `similar` | `duplicate_of` pairs (after A2) | recall@5, MRR |
| `bundle` | Issue sets closed by one merged PR. Only PRs closing **2–5** issues count, which excludes release and sweep PRs. | pair recall, plus precision of proposed pairs |
| `localize` | For each closed issue, its fix PR's `touches` versus the implicated set computed with **direct and analog sources only** (linked sources excluded, since they are the answer) | file recall@k |

**Leakage controls.** These are VERIFIED as necessary, because the store holds
only the current body (`upsert_item_row` overwrites `body`):

- Restrict comments to `created_at < closed_at`.
- Mask `#N`, same-repo URLs and 7–40-hex SHAs in evaluated text.
- Exclude analogs closed after the target was created.
- Report the residual leakage, body edits after close, as a known bias in
  the output.

**CI.** A synthetic fixture corpus has planted duplicates, co-fixed sets and
stack traces, and is built with `tests/fixtures.py` helpers. CI asserts
threshold floors on it, so a weight change that degrades retrieval fails the
build. Live-repo evaluation stays manual; CONTRIBUTING forbids live `gh` in
checks.

**Rationale.** Published work still finds lexical retrieval competitive with
learned methods at ~10k reports (arXiv 2504.14797). That supports stdlib
lexical-plus-structural signals. Every new signal must move a C metric to stay
in.

---

## 7. Workstream D: agent surface

**MCP tools.**

- Add `similar_items`, `triage`, `bundle`, `brief`, `coupling` and `churn`
  (`churn` is missing today, §2.6).
- Every tool gets `annotations` (`readOnlyHint: true` on all but `sync`) and
  an `outputSchema` with `additionalProperties: true`, so validation cannot
  reject evolving payloads.
- `tools/call` returns `structuredContent` (the envelope object) alongside
  the existing text block. Keep the text block, since clients differ in which
  one they read.
- Add `2025-11-25` to `_PROTOCOL_VERSIONS` only after its deltas are
  reviewed.
- File a separate issue for 2026-07-28: the stateless per-request `_meta`
  version, `server/discover`, and how roots resolution
  (`mcp_server._roots_slugs`) works without server-initiated requests.

**Skill.** `skills/zaxbygraph/SKILL.md` gets a "pick and bundle work" recipe:
`status` gate → `triage` → `bundle --from triage:30 --size 2-3 --count 3
--briefs`. The lead verifies each rationale against code before assigning.

---

## 8. Sequencing

Per CONTRIBUTING, each line below is one workstream issue and one PR, with
EARS acceptance criteria, and the regression test lands first.

| # | PR | Depends on | Delivers |
|---|----|-----------|----------|
| 1 | A0: quadratic ingest fix | — | Cold sync scales linearly |
| 2 | A1: v7 columns, `assignees`, `items_vocab`, Python backfill | A0 | Triage features on existing corpora, no new fetches |
| 3 | A3: `item_path_refs`, `item_signatures` | A1 | Issue→file evidence |
| 4 | B: `coupling`, `similar` + C fixture corpus + `eval similar/localize` | A3 | First measurable relatedness |
| 5 | B: `triage` + config | A1 | Ranked workable pool |
| 6 | B: `bundle`, `brief` + `eval bundle` | 4, 5 | The requested capability |
| 7 | A2: link stream, new timeline types, PR state, stdin transport | A1, eval harness | `blocked_by`, `child_of`, `duplicate_of`, CI/review state; dup-based eval |
| 8 | D: MCP tools, `structuredContent`/`outputSchema`/annotations, 2025-11-25 | 6 | Agent surface |
| 9 | Follow-ups: review-only drift detection, issue fields/Projects v2 (opt-in, `read:project` scope), CODEOWNERS on bundles | — | — |

Steps 1–6 deliver working bundling with the GraphQL query unchanged. A2, the
riskiest sync-path change, lands after the eval harness, so its effect is
measured rather than assumed.

---

## 9. Invariant ledger

| Invariant | How it holds |
|-----------|--------------|
| EXTRACTED only | New edges come only from GraphQL fields or timeline events. Path and signature rows are raw lexical extractions in child tables. The derived tier writes nothing (`mode=ro`). |
| Node types | Unchanged. Milestone, type, CI state, signatures and paths are columns or child tables. |
| Ownership on re-ingest | `blocked_by`/`child_of` use `src` = owner, with the presence-gated `link` stream. `duplicate_of` stays append-only. Inbound edges from other items are untouched. Child tables are replaced in `replace_item_children`. |
| One item, one transaction | All new writes sit inside `ingest_item`. The recount move in A0 keeps `status` counts truthful at page granularity. |
| Watermark | Untouched. |
| Read-only `sql` | Untouched. New tables are readable through it and the authorizer still default-denies. |
| SQLite floor | Holds at 3.35. `fts5vocab` predates it, and the backfill is Python, not JSON1. |
| Portability | `git` subprocesses are optional and guarded. The eval and fixture tests run on the Windows leg, with no skips. |

## Sources

- GitHub REST: issue dependencies — https://docs.github.com/en/rest/issues/issue-dependencies
- GitHub REST: sub-issues — https://docs.github.com/en/rest/issues/sub-issues
- Issue fields GA (2026-07-02) — https://github.blog/changelog/2026-07-02-issue-fields-are-now-generally-available
- `MarkedAsDuplicateEvent.canonical` only via GraphQL — https://github.com/orgs/community/discussions/201055 (community; verify against the live schema)
- MCP 2026-07-28 changelog — https://modelcontextprotocol.io/specification/2026-07-28/changelog
- Duplicate detection at scale (lexical vs learned) — https://arxiv.org/abs/2504.14797
- RepoMem, history-informed localization — https://arxiv.org/abs/2510.01003
