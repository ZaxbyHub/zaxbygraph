---
name: zaxbygraph
description: >
  Local incremental GitHub issue/PR knowledge graph. Sync once with `zaxbygraph sync`,
  then query SQLite instead of paging issue/PR threads into context. Use on any audit,
  history question, recurrence hunt, or "what touched this file" task for a GitHub repo.
---

# ZaxbyGraph

Forge graph for GitHub issues and pull requests. Complementary to Graphify (code graph).

## First action

```bash
zaxbygraph sync --repo OWNER/REPO
```

Then query. Never page GitHub into the lead after a successful sync.

```bash
zaxbygraph status
zaxbygraph search "QUERY"
zaxbygraph item N
zaxbygraph related N
zaxbygraph churn
zaxbygraph open
zaxbygraph path A B
zaxbygraph sql "SELECT ..."
```

## Forbidden after a successful sync

- `gh api --paginate` of issues, pulls, comments, or reviews into the lead context
- Re-fetching the same corpus “to be sure”

If `status` shows `last_error`, re-run `sync` (resume is the watermark). Use `--force`
only when you suspect review-only updates that did not bump `updated_at`.

## Collectors

May run `search|item|related|churn|sql` and paste **truncated** JSON.
Must not cluster, assign severity, or propose enhancements.

## `closes` is keyword-only

`closes` edges come from closing keywords in bodies and comments
(`close`, `closes`, `closed`, `fix`, `fixes`, `fixed`, `resolve`, `resolves`, `resolved`)
plus `#N` / same-repo URL. This is **not** GitHub’s connected-issue graph.
Commit-message auto-close is not in the DB in v0.1.

## Default DB

`.swarm/zaxbygraph/history.db` if `.swarm` exists or is gitignored, else
`.zaxbygraph/history.db`. Do not commit it.

## Frontier-audit Phase 1

Replace “page every issue into context” with: `zaxbygraph sync`, then collectors
run `search` / `sql` / `related` and return truncated rows. The lead model alone
generates candidates. Full program: [`../frontier-audit-enhance/SKILL.md`](../frontier-audit-enhance/SKILL.md).
Hook: `docs/frontier-audit-hook.md`.
