# ZaxbyGraph

Local, incremental **GitHub issue/PR knowledge graph**. Agents run `zaxbygraph sync`
then query SQLite instead of paging thousands of issue threads into context.

Graphify is the **code** graph. This is the **forge** graph. Join later on file
path (`touches` → Graphify node).

- Python 3.11+, **stdlib only**
- Auth is the `gh` CLI. No PAT stored here.
- Edges are **EXTRACTED** only (no LLM, no “purpose”, no inferred clusters)
- Incremental watermark on `updated_at`; each item is one SQLite transaction
- FTS5 over titles, bodies, comments

## Install

```bash
pip install -e .
zaxbygraph --help
```

Requires [GitHub CLI](https://cli.github.com/) authenticated (`gh auth login`).

Also requires **SQLite 3.35.0+** (2021-03-12), which Python links at build
time. Python 3.11+ on any current platform satisfies this; a very old Linux
(e.g. Ubuntu 20.04, SQLite 3.31) does not. The CLI checks on connect and exits
with a clear message rather than a bare SQL syntax error.

## Sync

```bash
zaxbygraph sync --repo OWNER/REPO
zaxbygraph status
zaxbygraph search "init hang"
zaxbygraph item 14
zaxbygraph related 14
zaxbygraph churn
zaxbygraph open
zaxbygraph path 10 11
zaxbygraph sql "SELECT number, title FROM items WHERE state='open'"
zaxbygraph export-graph
```

Default database:

- `<git-root>/.swarm/zaxbygraph/history.db` if `.swarm/` exists or is gitignored
- otherwise `<git-root>/.zaxbygraph/history.db`

Do not commit `history.db`.

`--include-patches` stores pull file patches (off by default). To backfill
patches after a no-patch sync you must `--force` (the watermark would skip
unchanged items).

`search --limit N` caps result rows (default 20). `churn --limit N` caps
ranked files (default 30). `related --depth N` sets how many hops the
neighborhood walks (default 1).

`--jsonl [DIR]` appends one JSON object per fetched resource to
`DIR/events.jsonl` (default: sibling `jsonl/` next to the database). Resume may
duplicate lines; consumers should key by `(resource, payload.id)`.

### What `closes` means

`closes` edges are **closing keywords in issue/PR bodies and comments**
(`close[sd]?`, `fix(e[sd])?`, `resolve[sd]?` plus `#N`, `owner/repo#N`, or a
same-repo GitHub URL). Cross-repo references are ignored.

This is **not** GitHub’s connected-issue / auto-close graph. Merge-commit
messages and the timeline API are out of scope for v0.1. Use `--force` if you
suspect review-only updates that did not bump `updated_at`.

### Rate limits

Sync is sequential `gh api`. Rough cost: `≈ 1 + items + 4×prs` REST calls.
Zero-comment issues skip the comments GET. A mid-run 429 writes `last_error`,
keeps the watermark at the last **committed** item, and is resumable.

## Agent rule

After a successful `zaxbygraph sync`, **do not** page `gh api --paginate` of
issues/PRs/comments into the lead model. Query the DB. Collectors may paste
truncated `search` / `item` / `related` / `sql` JSON; they do not cluster.

Copy [`skills/zaxbygraph/SKILL.md`](skills/zaxbygraph/SKILL.md) into
`.agents/skills`, `.claude/skills`, or `.opencode/skills`.

The collector-only audit program (lead generates every candidate; independent
reviewers/critics verify) is [`skills/frontier-audit-enhance/`](skills/frontier-audit-enhance/SKILL.md).
Phase 1 prefers this DB over paging GitHub. Standalone paste prompt:
[`skills/frontier-audit-enhance/assets/PASTE_PROMPT.md`](skills/frontier-audit-enhance/assets/PASTE_PROMPT.md).

## Schema (v1)

Tables: `items`, `labels`, `comments`, `reviews`, `pr_files`, `releases`,
`actors`, `edges`, `sync_state`, `fetch_log`, plus FTS5 `items_fts` /
`comments_fts`.

Edge rels: `authored`, `has_label`, `commented`, `reviewed`, `touches`,
`closes`, `mentions`. Actor `commented` / `reviewed` edges are **collapsed**
(one per actor×item); full history lives in `comments` / `reviews`.

## Tests

```bash
pip install -e .
python -m unittest discover -s tests
```

No live GitHub in CI. `FakeGitHubSource` only.

## License

Apache-2.0
