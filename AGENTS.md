# ZaxbyGraph — agent contracts

This repo is a **stdlib-only Python CLI**. Two standing constraints:

- **No runtime dependencies.** Anything a user must `pip install` beyond this
  package is a design change, not an implementation detail.
- **No GitHub PAT anywhere** — not in the repo, CI, the database, or a config
  file. `gh` is the credential, and it stays the only one.

## What this tool is for

A local, mechanically-derived index of one repo's issues and pull requests, so
an agent can query history instead of paging it into context. The value depends
on the database being *trustworthy as evidence*, which is why the invariants
below are about faithfulness rather than convenience.

Read [`README.md`](README.md) for usage and [`docs/schema.md`](docs/schema.md)
for columns before changing anything here.

## Invariants

Each of these is a property to preserve, not a line of code to match. Where a
mechanism is named, treat it as how the property holds *today* and re-derive the
current implementation before relying on it — mechanisms move, properties
shouldn't.

### Integrity of the data

- **Every edge is `confidence='EXTRACTED'`.** Nothing in the sync path may ask a
  model what an item means. No INFERRED edges, no "purpose" field, no
  clustering, no summarization. A CHECK constraint enforces the column value;
  the real invariant is that no code path *wants* to write anything else.
- **Node types are `actor`, `item`, `label`, `file` — and nothing else.** Never
  `issue`, `pr`, `comment`, or `review` as an edge endpoint type. An issue and a
  PR are both `item`; `items.kind` distinguishes them.
- **Evidence is a payload, never part of an edge's identity.** The unique key is
  `(repo, src_type, src_id, rel, dst_type, dst_id)`. Adding `evidence` to that
  key would let the same relationship appear many times with different
  provenance strings.
- **Actor `commented`/`reviewed` edges collapse** to one per actor×item. Full
  history lives in the `comments` and `reviews` tables; the graph stays
  item-centric rather than growing an edge per comment.
- **Mentions and closes from comments roll up to the owning item**, so the graph
  never has a comment as an endpoint.
- **A `closes` reference is not also a `mentions` reference.** The two are
  mutually exclusive for a given pair.
- **Cross-repo references are dropped, never localized.** A `#5` in another
  repo's URL must not attach to local item 5.
- **`closes` is keyword-derived and must keep being described that way.** It
  reflects closing keywords in bodies and comments — not GitHub's
  connected-issue graph, not merge-commit auto-close, not the timeline API. Any
  doc or output that implies otherwise is a correctness bug, because a caller
  will read a missing edge as "unrelated".

### Correctness of ingest

- **`items.id` and the watermark come from the issues-list payload.** Merging
  the `/pulls/{n}` response must not overwrite `id` or `updated_at`. There is a
  regression test for exactly this; it exists because getting it wrong silently
  corrupts incremental sync.
- **The watermark is passed back verbatim and inclusive.** Nothing is subtracted
  from it. Re-fetching the boundary item is harmless because ingest is
  idempotent; a one-second adjustment would create a window where an update is
  lost forever.
- **Each item is ingested in one `BEGIN IMMEDIATE` that includes its own
  watermark bump**, so an interrupted run resumes from the last fully committed
  item rather than skipping or re-doing everything.
- **Ingest is idempotent.** Re-fetching the same row must upsert, not raise.
  GitHub's pagination can legitimately hand you the same record twice, so any
  write that can see a duplicate needs a conflict target covering the
  constraint that would actually fire.
- **A storage fault is reported, not swallowed and not a traceback.** A failing
  sync records `last_error` and exits non-zero with a message, so a later
  `status` still shows that something went wrong.
- **Item-scoped child rows are replaced on re-ingest** (labels, comments,
  reviews, pr_files for that number), while edges pointing *at* the item from
  elsewhere survive — see the next point.
- **On item upsert, delete only edges the item owns**: its outbound edges and
  the actor relations pointing at it. Inbound `mentions`/`closes` from *other*
  items must survive, or re-syncing one item silently erases another item's
  references to it.

### The read-only escape hatch

- **No path through the `sql` subcommand can write, attach a database, set a
  pragma, or load an extension.** This is the invariant. It currently holds via
  two independent layers — a statement guard in `query.py` and a default-deny
  connection authorizer in `db.py` — and it needs to keep holding via *at least
  one* layer that is not a single regex. Re-derive both before trusting either;
  `docs/schema.md` records the verified accept/reject behavior.
- A caller-supplied row limit must not reach SQL unclamped. A negative `LIMIT`
  means *unlimited* in SQLite, so a non-positive value has to be normalized
  rather than forwarded.
- Query results are fetched incrementally. A query that *matches* an enormous
  number of rows must not materialize them all to return a handful.

### Portability

- Windows is a first-class platform, not an afterthought — it is where this
  mostly runs. CI covers Windows and Linux on 3.11 and 3.12. A test that
  short-circuits on Windows is worse than no test, because it reports green
  while asserting nothing.
- The SQLite floor is real and version-dependent (see `db.py`). If you use a
  newer SQLite feature, raise the documented floor and its check together —
  don't let it fail as a bare syntax error in someone else's environment.

## After a successful sync

**Forbidden:** paging `gh api --paginate` of issues, PRs, comments, or reviews
into the lead context. Query `zaxbygraph search|item|related|churn|open|path|sql`
instead. Re-fetching the corpus "to be sure" spends exactly the context the sync
existed to save.

Collectors may run read commands and paste **truncated** JSON. They do not
cluster, rank, assign severity, or propose fixes — that separation is the point
of the audit program in `skills/frontier-audit-enhance/`.

If `status` reports a non-null `last_error`, re-run `sync`; the watermark makes
it resume. Use `--force` only when you suspect updates that never bumped
`updated_at`.

## Tests

```bash
pip install -e .
python -m unittest discover -s tests -b
```

No network, no live GitHub, no credentials — `FakeGitHubSource` only, and it
should stay that way so the suite is runnable offline and in CI without secrets.

The `-b` flag matters: several tests exercise the CLI and print to stdout, which
otherwise buries the pass/fail summary.

When fixing a bug, add the regression test **first** and confirm it fails before
the fix. The invariants above each exist because something got them wrong once.
