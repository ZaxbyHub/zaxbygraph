# ZaxbyGraph: state-of-the-art gap review and the bundling plan

Status: proposal (2026-10-25). Baseline: `main` at b47913e, schema v6, 310 tests
green. Nothing here is implemented yet.

## 1. Where the tool stands

The ingest core is stronger than most comparable tools. It has a provenance
model where every edge records which stream reported it. Ingest is idempotent,
and each item commits in its own transaction, so an interrupted sync resumes
cleanly. Its watermark is verbatim and inclusive, so no update can fall between
two syncs. Read-only SQL is guarded by two independent layers. Every surface
returns the same JSON envelope, and the MCP server resolves the repo without a
cwd. None of that needs to be redone.

The gaps are on the other side. The tool records **what happened** faithfully.
It does not yet expose **what matters now** or **what belongs together**, which
are the two questions behind "find the most important issues and bundle 2–3".
Today an agent answering that question has to write SQL against `raw_json` and
`pr_files`, and then judge similarity from `search` snippets alone. That is
slow and inconsistent between runs, and it has three blind spots:

1. **Open issues have no file linkage.** `touches` exists only on PRs. An issue
   reaches a file only once a PR has closed it. Open issues are exactly the ones
   that need bundling, and they are the ones with no files.
2. **Demand, ownership, and blocking signals are not modeled.** Reactions,
   assignees, milestone, issue type, sub-issue parent, `blocked_by`/`blocking`,
   duplicates, reopen count, and PR CI/review state are either buried in
   `raw_json` or never fetched.
3. **There is no read-side relatedness primitive.** `related` returns only
   explicit edges, and `search` needs a query string. Nothing answers "items
   like #N" or "items that would collide with #N".

## 2. Gap inventory (ranked by impact on agent work selection)

| # | Gap | Evidence in the code | Fix tier |
|---|-----|----------------------|----------|
| G1 | Issue→file linkage missing for open issues | `touches` emitted only from `pr_files`; `extract.py` has no path extraction | Ingest (EXTRACTED, keyword stream) |
| G2 | Issue dependencies (`blocked_by`/`blocking`), sub-issue parent/children, issue type are not ingested | GraphQL selection in `graphql.py` has none of `blockedBy`, `parent`, `subIssues`, `issueType` | Ingest |
| G3 | Duplicates are not modeled | Timeline `itemTypes` is `[CLOSED_EVENT, CROSS_REFERENCED_EVENT]` only; `MarkedAsDuplicateEvent.canonical` is GraphQL-only (the REST timeline event lacks it) | Ingest |
| G4 | Demand/ownership signals live only in `raw_json` | No columns for reactions, comment count, assignees, milestone, `author_association` | Migration with backfill (no network) |
| G5 | Reopen history, manual "Development" links, and commit references are not captured | Timeline type list as above (`REOPENED_EVENT`, `CONNECTED_EVENT`, `REFERENCED_EVENT` absent) | Ingest |
| G6 | PR in-flight state is incomplete | No `reviewDecision`, `mergeable`, `statusCheckRollup`, `reviewRequests` | Ingest |
| G7 | Issue fields (GA 2026-07-02) and Projects v2 Priority/Status are not ingested | None | Opt-in ingest (needs the `read:project` scope via `gh auth refresh`) |
| G8 | No similarity, triage, or bundle primitives | `query.py` has no such functions | Read-side, derived, never persisted |
| G9 | No file co-change coupling | `churn` counts single files only | Read-side over `pr_files` (+ optional local `git log`) |
| G10 | No evaluation harness for retrieval quality | No labeled benchmark | Ground truth already in the graph (see §6) |
| G11 | MCP lags the spec | `_PROTOCOL_VERSIONS` tops out at `2025-06-18`; no `outputSchema`/`structuredContent`, no `readOnlyHint` annotations; the current revision is 2026-07-28 (stateless, `server/discover`) | MCP layer |
| G12 | Review-only changes need `--force` | Documented in README | Ingest: per-PR GraphQL `updatedAt` / review `submittedAt` delta check |
| G13 | No token-budgeted work packet | `item` returns everything; `--max-body-chars` is the only control | Read-side |
| G14 | No CODEOWNERS/ownership join | None | Read-side over a local checkout file (no network) |
| G15 | Discussions are not ingested | None | Low priority; a new `items.kind` would breach the node-type invariant only if exposed as a new type (it should stay `item`) |

G1–G8 and G13 decide whether bundling works. G10 decides whether anyone can
trust it. G11 is table stakes for agent hosts in late 2026.

## 3. The one design decision this plan needs from the owner

`AGENTS.md` forbids clustering and ranking **in the sync path**, and collectors
may not rank. Bundling is ranking. The plan keeps the data-integrity
invariants intact by adding a **derived read tier** under these rules:

- It is computed at query time from EXTRACTED rows. It is never written to
  `edges`, never stored in `items`, and never fed back into sync.
- It is deterministic: no model calls, and ties break by item number. The same
  DB always gives the same answer.
- It is explainable. Every score ships with its per-signal breakdown and the
  raw evidence (shared paths, shared terms, edge paths), and the weights are
  printed in the payload.
- Its output is labeled. The envelope carries `"derived": true`, and the doc
  wording is always "heuristic rank", never "severity" or "priority".
- Weights are configurable per repo through `.zaxbygraph.toml`, read with
  `tomllib`, which is stdlib on 3.11+.

This needs one amendment to `AGENTS.md`: "the derived read tier may rank and
group; it may never persist, and its output is a heuristic, not evidence." The
collector rule needs a matching amendment: a collector may *run* `similar`,
`triage`, and `bundle` and paste their output, but it may not add judgment of
its own. If this is rejected, §5 shrinks to raw feature exports and agents keep
doing the scoring themselves.

## 4. Phase A: ingest the missing signal (sync path, EXTRACTED only)

### A1. Schema v7: promote payload fields, with backfill from `raw_json`

New nullable columns on `items`:

- `reactions_total` and `reactions_plus1`
- `comments_count` and `author_association`
- `milestone_title` and `milestone_due_on`
- `issue_type`
- `sub_issues_total` and `sub_issues_completed`
- `blocked_by_count` and `blocking_count`
- `reopened_count`

PR-only columns:

- `review_decision`
- `mergeable`
- `checks_state`

New child table `assignees(repo, number, login)`, which is replaced on
re-ingest.

The migration backfills every field the REST issues payload already carries
via `json_extract(raw_json, …)`, with no GitHub calls. Fields missing from old
payloads stay NULL, and `freshness` gains a `fields_partial` flag until the next
`--force`. Before writing the migration, verify the keys against a real corpus:

```bash
zaxbygraph sql "SELECT number, json_type(raw_json,'$.reactions'), json_type(raw_json,'$.sub_issues_summary'), json_type(raw_json,'$.issue_dependencies_summary'), json_type(raw_json,'$.type'), json_type(raw_json,'$.assignees') FROM items ORDER BY updated_at DESC LIMIT 5"
```

Milestone stays a column, not a node, because the node types are fixed at
`actor,item,label,file,commit`.

### A2. GraphQL selection extensions (`graphql.py`)

Add these to the issue fields:

- `issueType { name }` and `parent { number repository { nameWithOwner } }`
- `subIssues(first:50) { nodes { number repository { nameWithOwner } } }`
- `blockedBy(first:50) { nodes { number repository { nameWithOwner } } }`
- `stateReason(enableDuplicate: true)`

Extend the timeline `itemTypes` with `REOPENED_EVENT`,
`MARKED_AS_DUPLICATE_EVENT` (select `canonical { ... on Issue { number repository { nameWithOwner } } }`),
`CONNECTED_EVENT`, `DISCONNECTED_EVENT`, and `REFERENCED_EVENT` (commit oid).

Add these to the PR fields: `reviewDecision`, `mergeable`,
`reviewRequests(first:20)`, and
`commits(last:1){nodes{commit{statusCheckRollup{state}}}}`.

Validate every field name against the live schema in one smoke run before
merging. The current `blockedBy` shape is documented only by third parties.
The rendered-query character budget (`_MAX_QUERY_CHARS = 24000`) will drop the
chunk size again, so re-measure it, because Windows' argv limit is the binding
constraint. Moving the query to stdin (`gh api graphql --input -`) removes that
ceiling entirely, and is the better fix.

New relations, each registered in `extract.RELATIONS` and partitioned into
`STRUCTURAL` or `PATH_EXCLUDED_RELATIONS`. The registry test enforces that
partition.

| rel | src→dst | source | path-traversable |
|-----|---------|--------|------------------|
| `blocks` | item→item | `payload` | yes |
| `parent_of` | item→item | `payload` | yes |
| `duplicate_of` | item→item | `timeline` | yes |
| `connected` | pr→issue (both `item`) | `timeline` | yes |
| `references_commit` | item→commit | `timeline` | yes |
| `assigned` | actor→item | `payload` | no (actor hub) |

`blocks`, `parent_of`, `duplicate_of`, and `connected` are GitHub-reported link
facts. Treat them like `timeline`/`closing_ref` under the ownership rule: they
are append-only only if they cannot be retracted upstream. Dependencies and
sub-issue parents **can** be removed upstream, so make them item-owned and
replace them on re-ingest, unlike the `closes` streams. `disconnected` events
retract `connected` by event order. Cross-repo endpoints stay repo-qualified
(`owner/repo#N`), as cross-references already are.

### A3. Issue→file linkage: `mentions_file` (keyword stream)

This is the most important addition for bundling.

- **Vocabulary.** The vocabulary is the union of `pr_files.path` for the repo
  and, when sync runs from a checkout, `git ls-files`. Both are stdlib
  `subprocess` calls with no network. Store the vocabulary in
  `file_vocab(repo, path, origin)`.
- **Extraction from item and comment text.** Extract paths from item bodies,
  comment bodies, and review bodies, with code fences included. The patterns
  are explicit `a/b/c.ext` tokens, Python `File "…/x.py", line N`, JS/Java/Go
  `at … (path:line:col)` and `path:line` forms, and Markdown links to
  `/blob/<ref>/<path>`.
- **Resolution.** Strip absolute and `site-packages` prefixes by
  longest-suffix match against the vocabulary. A bare basename is accepted
  only when it resolves to **exactly one** vocabulary path, and is dropped
  otherwise. Dropping is never guessing, the same rule as cross-repo keyword
  references.
- **Edges.** Each resolved path becomes an `item→file` edge with
  `rel='mentions_file'`, `source='keyword'`. The evidence is the matched
  span, capped as today. Comment mentions roll up to the owning item.
- **Re-derivation.** The vocabulary grows over time, so a cheap post-sync
  pass re-derives `mentions_file` for open items whenever `file_vocab`
  changed.

Also add a child table `item_signatures(repo, number, kind, value)`, replaced
on re-ingest. It holds exception class names, normalized first error lines
from code fences (hex/addresses/numbers/paths masked), and stack-frame function
names. The extraction is purely mechanical and turns "same crash" into an exact
join.

### A4. Review-only change detection (closes G12)

During incremental sync, compare each open PR's GraphQL `updatedAt` and the
latest review `submittedAt` against the stored values, and re-ingest PRs that
drifted. This removes most `--force` use.

## 5. Phase B: the derived read tier

All of this lives in a new module, `src/zaxbygraph/derive.py`. It reuses the
read-only connection, and the authorizer stays default-deny, because nothing
here writes. Every payload carries `derived: true`, a `weights` echo, and a
`signals` breakdown per row.

### B1. `similar N [--limit 10] [--state open|closed|all]`

Candidates come from the union of these generators. Each one is bounded, and
none scans every pair.

| Signal | Mechanism (stdlib only) | Why |
|--------|-------------------------|-----|
| `lexical` | More-like-this over FTS5. Read document frequencies from an `fts5vocab` table built over `items_fts`. Take N's top-k terms by tf·idf, OR-query `items_fts`, and normalize bm25 by N's self-score. | Same topic in different words. Porter stemming already helps. |
| `title_shingle` | Jaccard over word bigrams and char-4 shingles, computed only on the lexical candidates | Near-duplicate titles |
| `signature` | Exact join on `item_signatures` | Same crash, same error string |
| `files` | IDF-weighted Jaccard over each item's **implicated file set**, defined below | Same code area |
| `cochange` | Lift/confidence of file pairs co-occurring in merged PRs (`pr_files` self-join, min support 2) between the two items' file sets | Different files that always change together |
| `graph` | Shortest structural path length (reuse `path_between`), plus shared `parent_of`, shared milestone, and IDF-weighted shared labels | Explicit human-asserted linkage |

An item's **implicated file set** is the union of four sources:

1. its own `mentions_file` edges;
2. the `touches` of its linked PRs (`closes`, `closes_keyword`, `connected`);
3. **analog files**: the `touches` of PRs that closed its top-3 closed
   `lexical`/`signature` neighbours, each tagged `via: #M`;
4. for PRs, their own `touches`.

Analog files are the history-informed localization step that recent
repository-memory work reports as effective (RepoMem, arXiv 2510.01003). They
are reported separately and weighted lower than direct mentions.

File IDF is computed over all PRs. That stops `README.md`, `CHANGELOG.md`, and
lockfiles from making everything look related. A repo-config `ignore_paths`
glob list handles the rest.

The composite score is a weighted sum of normalized signals, with the weights
published in the payload.

### B2. `triage [--limit 20] [--type bug] [--label L] [--include-claimed]`

`triage` returns open issues with a raw feature vector and a transparent
heuristic score.

**Demand features:**

- `reactions_plus1`
- unique participants
- `comments_count`
- inbound `duplicate_of` count (each duplicate is another reporter)
- inbound `mentions` from distinct items

**Leverage features:**

- `blocking_count` of open items (fixing this one unblocks them)
- sub-issue completion gap
- milestone due date proximity

**Risk features:**

- `reopened_count`
- hotspot score of the implicated files (`churn` × recency)
- `reverts` history on those files
- a regression flag: a similar closed item exists whose fix touched the same
  files

**Explicit priority features:**

- `issue_type`
- issue-field Priority and project Priority (when ingested)
- label→weight map from `.zaxbygraph.toml`

**Workability gates**, which exclude an item by default and list the reason:

- open `blocked_by` exists
- an assignee exists
- an open linked PR exists (in flight)
- the item is locked

Age and staleness are reported but neutral by default.

### B3. `bundle [--size 2-3] [--count 5] [--seed N ...] [--max-files 15] [--from triage:30]`

**Candidate pool.** Take the workable pool from `triage`, or the seeds given.

**Affinity graph.** Build an affinity graph over the pool. The edge weight is
the `similar` composite, and an edge exists only when the composite is above a
floor **and** at least one non-lexical signal is non-zero. That condition stops
two issues that merely share vocabulary from bundling.

**Selection.** Selection is greedy and deterministic:

1. Seed with the highest-triage item not yet used.
2. Add the neighbour that maximizes `triage(n) × affinity(seed∪bundle, n)`,
   subject to the constraints below.
3. Stop at `--size`, or when the marginal gain falls below a threshold.

**Constraints:**

- The union of implicated files stays within `--max-files`.
- Dependencies are respected: if A blocks B, both may join the bundle, with
  the order emitted.
- An item conflicting with an open PR's files is penalized and flagged, using
  `pr_overlap`.
- An item that is a `duplicate_of` another pool member collapses into it.

**Bundle payload.** Each bundle carries:

- its items with their triage features
- the `rationale` signals per pair: shared paths, shared signature, shared
  terms, and the structural path
- the combined implicated file set, split into `direct` and `analog via #M`
- prior fix PRs for the analogs
- **test files historically co-changed** with the implicated files: `pr_files`
  paths matching the repo's test globs, from the same PRs
- open-PR collisions, blockers and order, and a risk summary (churn, reverts)
- a cohesion score and a `why_not_bigger` note naming the next-best candidate
  and the constraint that excluded it

### B4. `brief N [--budget-tokens 2000]`

`brief` returns a token-budgeted work packet:

- the title, state, and triage features
- the body truncated at paragraph boundaries
- the top comments by recency and author association (maintainer first)
- implicated files with evidence
- the closest closed analog and its fix PR diffstat
- linked PRs and their CI/review state

Token estimation stays at chars/4. It must never need a tokenizer dependency.
`bundle --briefs` inlines one `brief` per item.

### B5. `coupling PATH [--limit 20]`

`coupling` returns the files most co-changed with `PATH`, with support,
confidence, and lift. It takes optional `--git` to include direct-push commits
from local `git log --name-only` (no network). It is standalone, and it is also
the engine for the `cochange` signal.

## 6. Phase C: evaluation (what makes it trustworthy rather than plausible)

The graph already holds human-made ground truth, so the evaluation needs no
labelling.

- **Duplicates.** `duplicate_of` pairs are ground truth for `similar`, measured
  as recall@5 and MRR.
- **Co-fixed issues.** When one PR closes several issues, maintainers have
  already bundled them. `closes` edges sharing a PR are positive pairs for
  `bundle` affinity.
- **Localization.** For each closed issue, compare the files touched by its
  closing PR with the implicated file set computed **from text only** (no
  link to that PR). This measures file recall@k for A3 plus analogs.

`zaxbygraph eval similar|bundle|localize [--before DATE]` runs a time-split
evaluation, using only rows created before each target item's close.
Information from the future must never leak in, the same rule as SWE-bench-Live
style splits. The test suite gets a synthetic fixture corpus with planted
duplicates, co-fixed sets, and stack traces. CI asserts thresholds there, so a
weight change that degrades retrieval fails the build. The published
literature still reports lexical retrieval beating learned methods at ~10k
reports (arXiv 2504.14797), which supports the stdlib-only, lexical-plus-
structural design. Signals should be added because they move these metrics, not
because they are fashionable.

## 7. Phase D: agent surface

**MCP** gets four new tools: `similar_items`, `triage`, `bundle`, and
`brief`.

- Every tool declares an `outputSchema` and returns `structuredContent`, keeping
  the text block for backward compatibility.
- Every tool carries `annotations.readOnlyHint: true`, except `sync`.
- Protocol `2025-11-25` is added to `_PROTOCOL_VERSIONS`, with a tracked issue
  for `2026-07-28`: the stateless handshake (per-request `_meta` version), the
  `server/discover` RPC, and the removal of reliance on `roots/list`
  server-initiated requests. The roots-stall caveat in `SKILL.md` disappears
  under that model.

**Skill** gets a new "Pick and bundle work" recipe in
`skills/zaxbygraph/SKILL.md`:

```
zaxbygraph status                     # gate: complete, last_error null
zaxbygraph triage --limit 30          # workable, ranked, with reasons
zaxbygraph bundle --from triage:30 --size 2-3 --count 3 --briefs
# lead verifies each bundle's rationale against the code before assigning
```

The lead keeps the final decision. The tool supplies the evidence and a
reproducible heuristic first cut.

**Repo config** lives in `.zaxbygraph.toml`, which is optional and read with
`tomllib`. It holds:

- `[triage.labels]` weights (for example, `"P0" = 5`, `"good first issue" = -1`)
- `[paths] ignore = ["CHANGELOG.md", "*.lock"]` and `tests = ["tests/**", "**/*_test.go"]`
- `[bundle]` defaults
- `[weights]` for `similar`

The config is never stored in the DB, and it is echoed in the payload.

## 8. Sequencing (each line is one PR, regression test first)

1. A1: migration v7 and backfill, plus `assignees`. This unlocks `triage` with
   no network change.
2. A3: `file_vocab`, `mentions_file`, and `item_signatures`.
3. B5 and B1: `coupling`, then `similar`. Then C's fixture corpus and `eval
   similar`. Tune here.
4. A2: GraphQL extensions and the new relations (live smoke for field names),
   plus moving the query to stdin.
5. B2: `triage` and `.zaxbygraph.toml`.
6. B3 and B4: `bundle` and `brief`. Then `eval bundle` against co-fixed ground
   truth.
7. D: the MCP tools, `outputSchema`/`structuredContent`/annotations, and
   protocol 2025-11-25. File the 2026-07-28 issue.
8. A4, G7 (opt-in issue fields/projects), and G14 (CODEOWNERS owners on
   bundles).

Steps 1–3 deliver usable bundling on existing corpora with the GraphQL query
unchanged. Step 4 is the riskiest change on the sync path, and it lands after
the evaluation harness exists, so its effect on retrieval can be measured.

## 9. Invariants touched (and how each holds)

- **EXTRACTED only.** Every new edge comes from a payload field, a timeline
  event, or a deterministic text pattern resolved against a known vocabulary.
  The derived tier never writes.
- **Node types.** No new types. Milestone, issue type, CI state, and
  signatures are columns or child tables.
- **Ownership on re-ingest.** `mentions_file`, `assigned`, `blocks`, and
  `parent_of` are item-owned and replaced. `duplicate_of` and `connected`
  follow timeline append-only semantics, with `disconnected` handled by event
  order. Inbound edges from other items survive, as today.
- **SQLite floor.** `fts5vocab` ships with FTS5 and needs no new version.
  `json_extract` is already permitted by the authorizer. No floor change.
- **Portability.** `git ls-files`/`git log` are optional, guarded for a missing
  `git`, and tested on the Windows leg with real temp repos. Tests are never
  skipped on Windows.

## Sources

- GitHub REST: issue dependencies — https://docs.github.com/en/rest/issues/issue-dependencies
- GitHub REST: sub-issues — https://docs.github.com/en/rest/issues/sub-issues
- Issue fields GA (2026-07-02) — https://github.blog/changelog/2026-07-02-issue-fields-are-now-generally-available
- `MarkedAsDuplicateEvent.canonical` REST gap — https://github.com/orgs/community/discussions/201055 (community; verify against the live schema)
- MCP 2026-07-28 changelog — https://modelcontextprotocol.io/specification/2026-07-28/changelog
- Duplicate detection at scale, lexical vs learned — https://arxiv.org/abs/2504.14797
- Cupid, LLM + retrieval duplicate detection — https://arxiv.org/abs/2308.10022
- RepoMem, repository memory for localization — https://arxiv.org/abs/2510.01003
