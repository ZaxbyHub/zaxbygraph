# ZaxbyGraph — agent contracts

This repo is a **stdlib Python CLI**. Do not add runtime dependencies.
Do not store GitHub PATs. Auth is `gh`.

## Invariants

- Edges are `confidence='EXTRACTED'` only. No INFERRED, no purpose, no clustering.
- Node types: `actor`, `item`, `label`, `file`. Never `issue`/`pr`/`comment`/`review` as edge types.
- Actor `commented` and `reviewed` are collapsed (one per actor×item). History is in tables.
- Mentions/closes from comments roll up to the owning item.
- On item upsert, delete **owned** edges only. Never inbound `mentions`/`closes` from other items.
- Replace labels/comments/reviews/pr_files for that number every ingest.
- Each item is one `BEGIN IMMEDIATE` that includes the watermark bump.
- Pass `issues_since` back to GitHub **verbatim** (inclusive).
- FTS is external content (`content='items'|'comments'`), kept in sync by triggers.
- `sql` uses `PRAGMA query_only=ON` and never `executescript`.
- Cross-repo closing URLs must not attach to local numbers.

## After sync

Forbidden: paging `gh api --paginate` of issues/PRs/comments into the lead context.
Query `zaxbygraph search|item|related|churn|open|sql` instead.

## Tests

`python -m unittest discover -s tests` from this directory after `pip install -e .`.
No live network.
