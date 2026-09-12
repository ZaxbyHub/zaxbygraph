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

`sync` prints a summary:

```json
{
  "repo": "acme/forgegate", "ingested": 3, "last_number": 10, "full": true,
  "finished_at": "2026-09-12T19:23:04Z", "issues_since": "2026-01-03T05:40:10Z",
  "item_count": 3, "comment_count": 1, "edge_count": 10, "last_error": null,
  "ok": true, "db": "/path/to/.zaxbygraph/history.db"
}
```

`ingested` counts items touched by *this* run — `0` on a second run with no
upstream changes is the expected result, not a failure.

### Where the database goes

| Condition | Path |
| --- | --- |
| `.swarm/` exists, or `.gitignore` mentions `.swarm` | `<git-root>/.swarm/zaxbygraph/history.db` |
| otherwise | `<git-root>/.zaxbygraph/history.db` |

Override with `--db PATH` on any command. **Do not commit `history.db`** — it is
a rebuildable cache, not source. The shipped `.gitignore` already excludes it.

## Commands

Every command accepts:

| Flag | Meaning |
| --- | --- |
| `--db PATH` | Database location. Default as above. |
| `--repo OWNER/REPO` | Which repo to act on. Defaults to the `origin` remote of the current git repo. Required when the DB holds several repos and you want one. |
| `--format json\|text` | Output format. Defaults to `text` on a TTY and `json` when piped, so scripts and agents get JSON without asking. Pass it explicitly when the destination is ambiguous. `text` is a light flattening — top-level scalars print as `key: value`, while nested objects and lists still print as indented JSON — so **prefer `json` for anything parsed**. |

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

```json
{
  "repos": [{
    "repo": "acme/forgegate", "issues_since": "2026-01-03T05:40:10Z",
    "last_full_sync_at": "2026-09-12T19:23:04Z", "last_incr_sync_at": null,
    "last_error": null, "item_count": 3, "comment_count": 1,
    "edge_count": 10, "include_patches": 0
  }],
  "counts": [
    {"repo": "acme/forgegate", "kind": "issue", "state": "open", "c": 2},
    {"repo": "acme/forgegate", "kind": "pr", "state": "open", "c": 1}
  ]
}
```

Check `last_error` first. Non-null means the last sync stopped early.

### `search` — full-text

```bash
zaxbygraph search "QUERY" [--limit 20]
```

Searches titles, bodies, labels, and comment bodies. Returns `items` and
`comments` separately; `snippet` marks hits with `«` `»`.

```json
{
  "items": [{
    "repo": "acme/forgegate", "number": 10, "kind": "issue",
    "title": "Init hangs on empty repo", "state": "open", "author": "alice",
    "updated_at": "2026-01-03T05:40:10Z",
    "html_url": "https://github.com/acme/forgegate/issues/10",
    "snippet": "«Init» hangs on empty repo"
  }],
  "comments": []
}
```

FTS5 syntax applies: `"exact phrase"`, `a OR b`, `NOT b`, `pref*`. A bare
multi-word query is an implicit AND.

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

Returns a **bare JSON array**, ranked by how many PRs touched each path:

```json
[{"path": "src/zaxbygraph/sync.py", "prs": 1, "additions": 3, "deletions": 1}]
```

### `open` — open issues and PRs

Returns a **bare JSON array** of open items, newest first.

### `path` — how two things connect

```bash
zaxbygraph path A B
```

`A` and `B` are item numbers or file paths. Undirected breadth-first search over
`touches`, `closes`, and `mentions`:

```json
{"a": "14", "b": "11", "repo": "acme/forgegate",
 "path": [{"type": "item", "id": "14"}, {"type": "item", "id": "11"}]}
```

When no route exists, `path` is `null` and the exit code is still `0` — "not connected" is an answer, not a failure.

### `sql` — read-only SQL

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

- **Object vs array.** `status`, `search`, `item`, `related`, `path`, and
  `export-graph` return JSON **objects**. `churn` and `open` return bare JSON
  **arrays**. Indexing `result["items"]` into `churn` output will fail.
- **Exit codes.** `0` success. **`2`** — usage or guard rejection, i.e. the
  request was malformed (bad flags, non-read SQL, multiple statements). **`1`** —
  a runtime failure: item not found, sync error, or an authorizer denial at
  execution time. The distinction matters for retry logic: a `2` will never
  succeed on retry unchanged, while a `1` sometimes will.
- **Errors** print `error: MESSAGE` to **stderr**, leaving stdout clean for
  parsing — with one exception worth special-casing: **`sync` reports failure as
  a normal result object on stdout**, `{"ok": false, "error": …, "db": …,
  "repo": …}`, and exits 1. So check `ok` on sync output rather than assuming an
  empty stderr means success.
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
`actors`, `edges`, `sync_state`, `fetch_log`, `meta`, plus FTS5 `items_fts` and
`comments_fts`.

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
| No database found / wrong repo | Resolution depends on the git root and the `origin` remote. Pass `--db` and `--repo` explicitly to remove the ambiguity. |

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
