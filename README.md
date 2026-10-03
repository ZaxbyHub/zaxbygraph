# ZaxbyGraph

Local, incremental **GitHub issue/PR knowledge graph**. Sync a repo's forge
history into SQLite once, then query it — instead of paging thousands of issue
threads through an agent's context window.

Graphify is the **code** graph. This is the **forge** graph. They join on file
path (`touches` → Graphify node).

- Python 3.11+, **stdlib only** — no runtime dependencies
- Auth is the `gh` CLI. No PAT is stored by this tool
- Edges are **`EXTRACTED`** only: no LLM, no inferred "purpose", no clustering
- Incremental watermark on `updated_at`; each item is one SQLite transaction
- FTS5 full-text search over titles, bodies, and comments

## Why

An agent auditing a repo's history has two options. It can page
`gh api --paginate` over every issue, PR, and comment thread — burning a large
fraction of its context on raw JSON before it has thought about anything — or it
can query a local index. This is the local index.

The design constraint that follows from that: **everything in the database is
mechanically derived.** No step in the sync path asks a model what an issue
"means". That keeps the store cheap to rebuild, auditable, and safe to treat as
evidence — if a row is here, GitHub said it. Interpretation is the caller's job.

## Install

```bash
pip install -e .
zaxbygraph --help
```

Requires the [GitHub CLI](https://cli.github.com/), authenticated:

```bash
gh auth login
```

Tested on Windows and Linux; CI runs both on Python 3.11 and 3.12.

**SQLite 3.35.0+** (March 2021) is also required — Python links SQLite at build
time, so this is a property of your Python, not a package you install. Any
current platform satisfies it; a very old Linux (Ubuntu 20.04 ships SQLite
3.31) does not. The CLI checks on connect and fails with an explicit message
rather than a confusing SQL syntax error.

## Quickstart

```bash
zaxbygraph sync --repo OWNER/REPO     # fetch (incremental after the first run)
zaxbygraph status                     # what's in the DB, and the watermark
zaxbygraph search "init hang"         # full-text over titles, bodies, comments
zaxbygraph item 14                    # one item, with comments/files/edges
```

`sync` prints the envelope with the run summary under `data` (keys `ingested`,
`last_number`, `full`, `finished_at`, `issues_since`, `item_count`,
`comment_count`, `edge_count`, `last_error`; `repo` and `db` live on the
envelope root):

```json
{
  "ok": true, "db": "/path/to/.zaxbygraph/history.db", "repo": "acme/forgegate",
  "freshness": {"synced_at": "2026-09-12T19:23:04Z", "age_s": 0, "complete": true},
  "data": {
    "ingested": 3, "last_number": 10, "full": true,
    "finished_at": "2026-09-12T19:23:04Z", "issues_since": "2026-01-03T05:40:10Z",
    "item_count": 3, "comment_count": 1, "edge_count": 10, "last_error": null
  },
  "truncated": false
}
```

`ingested` counts items touched by *this* run — `0` on a second run with no
upstream changes is the expected result, not a failure.

### Where the database goes

The graph is keyed by **repo**, not by checkout (issue #2). Once the
user-level store exists, every worktree, clone, and subagent of the same
repository resolves the same file; during migration (a checkout still
serving a legacy in-repo DB, before `doctor --consolidate`) run
`zaxbygraph where` to see which file each checkout is serving.

| Order | Source |
| --- | --- |
| 1 | `--db PATH` when passed (explicit always wins) |
| 2 | `ZAXBYGRAPH_DB` environment variable |
| 3 | The user-level store: `<root>/<host>/<owner>/<repo>/history.db` |
| 4 | A legacy in-repo DB under the **main** worktree or the current checkout (`.zaxbygraph/` or `.swarm/zaxbygraph/`), for migration continuity until `doctor --consolidate` runs |
| 5 | Nothing yet — reads exit `3`; `sync` creates the store |

The store root is `%LOCALAPPDATA%\zaxbygraph` on Windows and
`${XDG_DATA_HOME:-~/.local/share}/zaxbygraph` elsewhere; `ZAXBYGRAPH_HOME`
overrides it. `<host>` comes from the origin URL host (`github.com` default —
a bare `--repo OWNER/REPO` carries no host, so GitHub Enterprise users should
sync from a checkout whose origin is the GHE URL rather than passing `--repo`
by hand, or their store keys collide with github.com slugs).

Reads never create anything: when no corpus exists for the resolved repo they
exit **3** with the resolved path and the exact `zaxbygraph sync --repo <slug>`
command on stderr. A DB that holds other repos but not the resolved one exits
**2** and lists them; if it holds only a pre-fold (mixed-case) row of
the resolved repo itself, exit **2** says so and points at
`doctor --consolidate` - it is never reported as another repo. `zaxbygraph where` prints the full resolution chain;
`zaxbygraph doctor [--consolidate] [--scan DIR]` reports (and optionally
consolidates, copy-only) scattered legacy DBs. `where`'s `db`/`exists`/`items`/`watermark`/`complete` describe the
user-level store for the slug, while `serving` names the DB reads
actually use (they differ while a legacy DB is being served);
`cwd`/`git_common_dir`/`slug`/`legacy` are the chain steps, and
`store_error` is set when the store file exists but cannot be read;
`sync_lock` surfaces the lock holder when one exists - a lock left by
a dead machine (different host) is never stolen; delete
`<db>.sync.lock` by hand to clear it. `doctor --db` overrides the
consolidation DESTINATION (the store), not the scan sources.

Concurrent `sync` runs of one
repo serialize through a lock file (`<db>.sync.lock`): a second sync joins
with `{"ok": true, ..., "data": {"joined": true}}` and zero GitHub calls, `--wait` blocks for
the lock, and a lock left by a dead same-host pid is recovered automatically.

**Do not commit `history.db`** — it is a rebuildable cache, not source.

## Commands

Every command accepts:

| Flag | Meaning |
| --- | --- |
| `--db PATH` | Database location. Default as above. |
| `--repo OWNER/REPO` | Which repo to act on. Defaults to the `origin` remote of the current git repo — for real, on every command (issue #2). Pass `--repo ''` for an explicit no-filter across the whole DB. |
| `--format json\|compact\|jsonl\|text` | Output format. Defaults to `text` on a TTY and `json` when piped. `json` and `compact` always carry the envelope; `jsonl` prints list payloads one object per line (any `head -n` prefix parses; identity is on the stderr line); `text` is a light flattening of the payload only — **prefer `json` for anything parsed**. |
| `--fields a,b` | Project rows to those keys (unknown keys are omitted). Applies to list payloads and `sql` rows in both row modes; not to nested arrays inside dict payloads. |
| `--rows objects\|array`, `--limit N` | `sql` only: row shape (objects by default) and row cap (default 200). |
| `--max-body-chars N` | `item` only: truncate bodies to N chars and mark them `truncated: true`. |

### `sync` — fetch into the graph

```bash
zaxbygraph sync --repo OWNER/REPO [--force] [--include-patches] [--jsonl [DIR]]
```

| Flag | Meaning |
| --- | --- |
| `--force` | Ignore the watermark and do a full pull. Needed when you suspect updates that did not bump `updated_at` (review-only changes), and required to backfill patches after a no-patch sync. |
| `--include-patches` | Store unified diffs in `pr_files.patch`. Off by default — patches dominate the database size. |
| `--jsonl [DIR]` | Also append one JSON object per fetched resource to `DIR/events.jsonl` (default: a `jsonl/` directory beside the database). A resumed sync may duplicate lines, so consumers should key on `(resource, payload.id)`. |

Sync is sequential and resumable. Rough cost is `≈ 1 + items + 4×prs` REST
calls; issues with zero comments skip the comments request, and PRs with zero
changed files skip the files request. If it stops partway — rate limit, network,
anything — the watermark stays at the last **fully committed** item and
`last_error` is recorded, so re-running `sync` resumes rather than restarting.

### `status` — counts and watermark

Returns an object with `repos` (one row per repo, from `sync_state`) and
`counts` (items grouped by kind and state). Reads no bodies, so it is cheap.

The payload below is what you get under `data` (the envelope adds
`ok`/`db`/`repo`/`freshness`/`truncated` around it):

```json
{
  "repos": [{
    "repo": "acme/forgegate", "issues_since": "2026-01-03T05:40:10Z",
    "last_full_sync_at": "2026-09-12T19:23:04Z", "last_incr_sync_at": null,
    "last_error": null, "item_count": 3, "comment_count": 1,
    "edge_count": 10, "include_patches": 0, "full_sync_pending": 0,
    "complete": true
  }],
  "counts": [
    {"repo": "acme/forgegate", "kind": "issue", "state": "open", "c": 2},
    {"repo": "acme/forgegate", "kind": "pr", "state": "open", "c": 1}
  ]
}
```

Each `repos` row carries `complete`: true only when no full sync is pending
and the last sync recorded no error — the readiness signal to gate on. Check
`last_error` first. Non-null means the last sync stopped early.

### `search` — full-text

```bash
zaxbygraph search "QUERY" [--limit 20]
```

Searches titles, bodies, labels, and comment bodies with the porter-stemmed
FTS5 index (`memory` matches `memories`, `reconnect` matches
`reconnection`). Tokens are quoted individually, so user input is never
interpreted as FTS5 syntax (`OR`, `NEAR`, column filters in a query are
inert words) — there is no operator surface, just words.

Ranking is bm25 with the title weighted highest, then body, then labels;
`updated_at` breaks ties. If the strict all-tokens pass finds fewer items
than the page, the query is retried with common English stopwords removed
and tokens OR-joined, and `matched_mode` reports which pass produced the
page (`"all"` or `"any"`). Comment hits merge into their parent item — the
item appears once with `matching_comments` (exact count) and
`comment_snippet` (a highlighted snippet from the best-matching comment);
an item-text hit keeps its highlight in `snippet`.

```json
{
  "items": [{
    "repo": "acme/forgegate", "number": 10, "kind": "issue",
    "title": "Init hangs on empty repo", "state": "open", "author": "alice",
    "updated_at": "2026-01-03T05:40:10Z",
    "html_url": "https://github.com/acme/forgegate/issues/10",
    "snippet": "«Init» hangs on empty repo",
    "matching_comments": 0,
    "comment_snippet": ""
  }],
  "matched_mode": "all",
  "total_matches": 1,
  "corpus_items": 42,
  "index_stale": false
}
```

`total_matches` counts distinct matching items before the limit and
`corpus_items` counts the repo-scoped corpus, so "no hits in 42 items" is
distinguishable from an empty database. Every result also carries
`index_stale`: true when the database predates the current schema (reads
never migrate), so a zero-hit answer from an un-migrated v2 index is not
mistaken for prior-art absence. **Shape change (v0.3): the separate
`comments` list is gone** — comment hits are items now, as shown above.

### `item` — one issue or PR in full

```bash
zaxbygraph item N
```

Returns every `items` column plus nested `labels`, `comments`, `reviews`,
`files`, and `edges`. This is the one command that returns bodies, so it is the
expensive one — reach for `search` or `sql` when you need breadth.

Exits 1 with `error: item #N not found` if the number is not in the DB.

### `related` — neighborhood

```bash
zaxbygraph related N [--depth 1]
```

Returns `{number, repo, nodes, edges}`. `nodes` are the items reachable within
`--depth` hops; `edges` carry `src_type`/`src_id`/`rel`/`dst_type`/`dst_id`
plus `confidence` and `evidence`. Raising `--depth` grows results quickly.

### `churn` — files ranked by PR touches

```bash
zaxbygraph churn [--limit 30]
```

Returns the ranked rows under `data` (an array), by how many PRs touched each path:

```json
[{"path": "src/zaxbygraph/sync.py", "prs": 1, "additions": 3, "deletions": 1}]
```

### `open` — open issues and PRs

Returns the open items under `data` (an array), newest first.

### `path` — how two things connect

```bash
zaxbygraph path A B
```

`A` and `B` are item numbers or file paths. Undirected breadth-first search over
`touches`, `closes`, and `mentions`. The `data` payload:

```json
{"a": "14", "b": "11", "repo": "acme/forgegate",
 "path": [{"type": "item", "id": "14"}, {"type": "item", "id": "11"}]}
```

When no route exists, `path` is `null` and the exit code is still `0` — "not connected" is an answer, not a failure.

### `sql` — read-only SQL

Result rows carrying a `repo` column are filtered to the resolved repo
(issue #2). A projection without a `repo` column is only store-scoped by
construction - filter explicitly when aiming `--db` at a multi-repo file.
A file that is not a zaxbygraph database exits 1 with a clean
`error: ... is not a zaxbygraph database` message. When the resolved repo is filtered, `truncated` reports the
PRE-filter rowset: it stays `true` while any unfiltered rows remain above
the limit, so raise the limit to converge.

```bash
zaxbygraph sql "SELECT number, title FROM items WHERE state='open'"
```

The escape hatch for anything the other commands don't shape. **Column
reference: [`docs/schema.md`](docs/schema.md)** — read it before writing
queries; several columns behave in ways worth knowing (`edges.src_id` is TEXT
even for item numbers, `patch` is NULL without `--include-patches`).

Read-only is enforced in two independent layers: a statement guard
(`SELECT`/`WITH`/`EXPLAIN` only, single statement, string-literal aware) and a
default-deny SQLite authorizer on the connection. `docs/schema.md` has the
verified accept/reject table, including the one case that passes the first layer
and is stopped by the second.

Results are capped (default 200 rows) and fetched incrementally, so matching a
million rows does not materialize a million rows.

### `export-graph` — Graphify-shaped JSON

Returns `{nodes, edges}` with `id` values namespaced by type (`item:14`,
`file:src/…`, `actor:alice`, `label:bug`) and edges as
`{source, target, rel, confidence}`. Intended for handing to a graph
viewer or joining with a code graph on `file:` nodes.

## Output and error contract

Machine-readable rules, worth knowing before scripting against this:

- **One envelope (v0.2, breaking).** Every command's JSON output is a single
  self-describing object: `{"ok": true|false, "db": "<path>", "repo": "<slug>",
  "freshness": {"synced_at": iso|null, "age_s": secs|null, "complete": bool},
  "data": <payload>, "truncated": bool}` - plus `"error": {"code", "message",
  "hint"?}` on failures. The payload you used to get at the top level now lives
  under `data`; `ok` is still at the root. `churn` and `open` payloads are
  JSON arrays under `data`. Every successful read that resolves a repo also
  writes exactly one identity line to stderr (slug-less reads — `--repo ''` —
  print none; `sync` and `doctor` never do):
  `# db=<path> repo=<slug> items=<n> synced=<age> complete=<yes|no>`
  (`synced=0` means "no recorded sync age"; `synced=<age>` is whole seconds).
- **Formats and rows.** `--format json` (pretty, default when piped) and
  `compact` (one line) always carry the envelope; `jsonl` prints one JSON
  object per line for list payloads (rows only — the envelope identity is on
  the stderr line) and the whole envelope for single-payload commands, so any
  `head -n` prefix parses; `text` (default on a TTY) renders the payload only,
  never envelope keys. `--fields repo,number` projects rows to exactly those
  keys that exist (unknown keys are omitted) — it applies to list payloads and
  to `sql` rows in both modes (objects: keys; `--rows array`: positions), not
  to nested arrays inside dict payloads. With duplicate-suffixed objects
  rows, project by the suffixed key (`columns` keeps the true names). `sql` rows are objects keyed by
  column by default — duplicate column names are suffixed `name_2`, `name_3`,
  … so no value is lost (`columns` keeps the true names; `--rows array` keeps
  positional lists and exact duplicates); `--limit N` overrides the 200-row
  cap. `item N --max-body-chars C` truncates bodies and marks them
  `"truncated": true`. `zaxbygraph schema [TABLE]` prints live DDL plus
  per-column notes - use it instead of a file path to learn the schema.
- **Output encoding.** stdout and stderr are UTF-8 on every platform,
  regardless of the ambient locale — emoji and CJK in titles and bodies
  round-trip exactly. `--repo` matches case-insensitively; canonical storage
  is lowercase.
- **Exit codes.** `0` success. **`2`** — usage or guard rejection, i.e. the
  request was malformed (bad flags, non-read SQL, multiple statements), or the
  resolved DB holds other repos but not this one. **`1`** — a runtime failure:
  item not found, sync error, or an authorizer denial at execution time.
  **`3`** — no corpus for the resolved repo (issue #2): the resolved DB is
  missing or holds nothing for it; the message names the path and the exact
  `sync` command. The distinction matters for retry logic: a `2` or `3` will
  never succeed on retry unchanged (fix the request, or sync first), while a
  `1` sometimes will.
- **Errors answer on stdout in JSON mode**: a failed command writes the
  envelope with `ok: false` and a coded `error` object (`no_such_column` with a
  `hint` naming the real columns, `no_such_table`, `not_found`, `no_corpus`,
  `bad_request`, `bad_sql`, `runtime`) to **stdout**, so `json.load(stdin)`
  never sees an empty stream. The same `error: MESSAGE` one-liner is still
  printed to stderr for humans; argparse usage errors remain plain stderr.
- `sync` failures also persist to `sync_state.last_error`, so a later `status`
  still reports a sync that failed hours ago.

## Agent usage

After a successful `sync`, **do not** page `gh api --paginate` of issues, PRs,
or comments into the lead model. Query the database. That rule is the point of
the tool — re-fetching the corpus "to be sure" spends the context the sync was
meant to save.

Install the skill by copying [`skills/zaxbygraph/SKILL.md`](skills/zaxbygraph/SKILL.md)
into `.agents/skills`, `.claude/skills`, or `.opencode/skills`.

The collector-only audit program — collectors gather, the lead model alone
generates candidates, independent reviewers and critics verify — is
[`skills/frontier-audit-enhance/`](skills/frontier-audit-enhance/SKILL.md). Its
Phase 1 prefers this database over paging GitHub; the contract is
[`docs/frontier-audit-hook.md`](docs/frontier-audit-hook.md). A standalone paste
prompt is at
[`skills/frontier-audit-enhance/assets/PASTE_PROMPT.md`](skills/frontier-audit-enhance/assets/PASTE_PROMPT.md).

## Data model

Tables: `items`, `labels`, `comments`, `reviews`, `pr_files`, `releases`,
`actors`, `edges`, `sync_state`, `fetch_log`, `meta`, plus FTS5 `items_fts`
and `comments_fts` (porter-stemmed since schema v3).

Schema versions are forward-only (`PRAGMA user_version`). A v2 database is
rebuilt in place — both FTS tables drop and re-create with the porter
tokenizer, no resync — by the next `sync` that opens it (sync is the one
command that resolves to and writes a legacy database in place). Reads never
migrate a database, and plain `doctor` never writes one either: it reads
legacy files through migrated temp copies and `doctor --consolidate`
migrates the *store* it builds, not the original. Two consequences worth
knowing: a v2 database keeps the old tokenizer (search reports
`index_stale: true`) until that first in-place `sync`, and once v3 is
stamped, older zaxbygraph builds refuse the file. See
[`docs/schema.md`](docs/schema.md) for the full migration reference.

Edge relationships: `authored`, `has_label`, `commented`, `reviewed`, `touches`,
`closes`, `mentions`. Node types are only `actor`, `item`, `label`, `file` — an
issue and a PR are both `item`, distinguished by `items.kind`.

Actor `commented`/`reviewed` edges are **collapsed** to one per actor×item; the
full history lives in the `comments` and `reviews` tables.

Full column-level reference: [`docs/schema.md`](docs/schema.md).

### What `closes` means

`closes` edges come from **closing keywords in bodies and comments** —
`close`/`closes`/`closed`, `fix`/`fixes`/`fixed`,
`resolve`/`resolves`/`resolved` — followed by `#N`, `owner/repo#N`, or a
same-repo GitHub URL. Cross-repo references are ignored rather than attached to
a same-numbered local item.

This is **not** GitHub's connected-issue graph. Auto-close from merge-commit
messages, links made in the GitHub UI, and anything visible only through the
timeline API are out of scope for v0.1. Treat a missing `closes` edge as "no
keyword said so", not as "these are unrelated".

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `gh: command not found`, or auth errors during sync | The GitHub CLI is the credential. `gh auth login`, then `gh auth status` to confirm. |
| `zaxbygraph requires SQLite >= 3.35.0` | Your Python is linked against an older SQLite. Use a newer Python build; it is not fixable with pip. |
| `error: item #N not found` (exit 1) | That number was never synced, or a later `--force` is needed. Check `status` for `item_count` and `last_error`. |
| `error: SQL must start with SELECT, WITH, or EXPLAIN` (exit 2) | `sql` is read-only. Also raised for `PRAGMA` and `ATTACH`. |
| `error: multiple statements are not allowed` (exit 2) | One statement per call. A trailing `;` is fine. |
| `error: not authorized …` (exit 1) | The connection authorizer refused something at execution — e.g. `load_extension`. |
| `status` shows a non-null `last_error` | The previous sync stopped early. Just re-run `sync`; it resumes from the watermark. |
| Sync returns `ingested: 0` | Nothing changed upstream. Use `--force` if you suspect updates that did not bump `updated_at`. |
| `pr_files.patch` is NULL | Patches are off by default. Re-sync with `--include-patches --force` — `--force` is required because the watermark would otherwise skip unchanged items. |
| No database found / wrong repo | Resolution is repo-keyed (see "Where the database goes"): the origin slug picks the store. Run `zaxbygraph where` to see the chain, `--db` to override the path, `--repo` to override the slug. |

## Development

```bash
pip install -e .
python -m unittest discover -s tests
```

Tests run against `FakeGitHubSource` — no network, no live GitHub, no
credentials. Pass `-b` to keep the pass/fail summary from being buried under
CLI output the tests produce:

```bash
python -m unittest discover -s tests -b
```

Invariants that tests enforce, and that changes must not regress, are listed in
[`AGENTS.md`](AGENTS.md).

## License

Apache-2.0
