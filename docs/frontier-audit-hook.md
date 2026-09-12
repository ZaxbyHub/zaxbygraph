# Frontier-audit Phase 1 hook

Replace the “enumerate GitHub history into the lead context” step with ZaxbyGraph.

The full program lives in [`skills/frontier-audit-enhance/`](../skills/frontier-audit-enhance/SKILL.md). This file is the Phase 1 contract for that skill.

1. `zaxbygraph sync --repo OWNER/REPO` (or `--force` if the watermark is suspect).
2. Collectors run only:
   - `zaxbygraph status`
   - `zaxbygraph search …`
   - `zaxbygraph item N`
   - `zaxbygraph related N`
   - `zaxbygraph churn`
   - `zaxbygraph open`
   - `zaxbygraph path A B`
   - `zaxbygraph sql 'SELECT …'`
3. Collectors paste truncated JSON. They do not interpret, cluster, or assign severity.
4. The lead model generates all candidates from those rows plus the codebase.
5. Independent reviewers re-read cited files only.

Do not page `gh api --paginate` of issues/PRs/comments into the lead after a
successful sync. `closes` in this DB is keyword-in-body/comments, not GitHub’s
connected-issue graph — say so if a candidate depends on auto-close.

If `zaxbygraph` is not installed, the skill falls back to `gh` pagination and
names the gap in `identity-packet.md`.
