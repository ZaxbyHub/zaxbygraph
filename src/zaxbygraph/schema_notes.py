"""Per-column notes for `zaxbygraph schema` (issue #3 AC4).

The notes live next to the query surface instead of only in `docs/schema.md`
so the command and the doc cannot drift: `tests/test_schema_notes.py` asserts
the documented caveats appear here and in the doc.
"""

from __future__ import annotations

import sqlite3

# Curated, hand-written column notes. Keys are tables, then columns; values
# are the caveats a caller must know before writing SQL against this DB.
COLUMN_NOTES: dict[str, dict[str, str]] = {
    "edges": {
        "src_id": "TEXT-typed even for item numbers - filter with src_id = '10', not 10; a foreign cross-referenced source is a repo-qualified id (`owner/repo#N`), never a bare number",
        "dst_id": "TEXT-typed even for item numbers - use dst_id = '10' in SQL filters; a commit endpoint carries the sha so the graph joins to `git log`",
        "evidence": "Provenance payload; never part of an edge's identity - `source` is part of the key because the same pair reported by two streams is two facts, with the closes exception: rel='closes' holds one row per pair whose evidence carries each stream that reported it - both when both streams did (schema v6)",
        "source": "Provenance stream: 'keyword' (text patterns), 'timeline' (closed/cross-referenced/reopened events), 'closing_ref' (PR closingIssuesReferences), 'link' (v10 GitHub-reported blockedBy/parent/duplicateOf/open-linked-PR connections; replaced per connection when fetched complete, union-only on overflow), 'payload' (structured fields). Part of the unique key except for rel='closes', where GitHub-reported streams merge into one row (closing_ref preferred when present, each reporting stream's evidence kept)",
    },
    "pr_files": {
        "patch": "NULL unless the sync used --include-patches",
    },
    "assignees": {
        "login": "One row per listing-payload assignee; item-scoped, replaced with the other children on every re-ingest",
    },
    "issue_field_values": {
        "field_name": "One row per listing-payload custom field entry ({name, value}); item-scoped, replaced with the other children on every re-ingest",
        "value": "Stringified payload value; NULL when the entry carried an explicit null",
    },
    "items": {
        "id": "Comes from the issues-list payload; never overwritten by the /pulls merge",
        "reactions_total": "v7, listing-owned (reactions.total_count). Re-ingest from a source without the key preserves the stored value; a key present with JSON null clears it. NULL on rows whose last payload predated v7 (see status coverage null_v7_rows)",
        "milestone": "v7, listing-owned (milestone.title); same ABSENT/present-null rule as reactions_total",
        "item_type": "v7, listing-owned (type.name); same ABSENT/present-null rule",
        "sub_issues_total": "v7, listing-owned (sub_issues_summary.total); PR rows never carry the summaries in the listing payload, so this stays NULL for them",
        "sub_issues_completed": "v7, listing-owned (sub_issues_summary.completed)",
        "dependencies_blocked_by": "v7, listing-owned (issue_dependencies_summary.blocked_by) - the OPEN count; the link relations themselves shipped in schema v10",
        "dependencies_blocking": "v7, listing-owned (issue_dependencies_summary.blocking) - the OPEN count",
        "dependencies_total_blocked_by": "v7, listing-owned (issue_dependencies_summary.total_blocked_by) - kept distinct from the open count",
        "dependencies_total_blocking": "v7, listing-owned (issue_dependencies_summary.total_blocking)",
        "author_association": "v7, listing-owned (the item row's own author_association)",
        "merged_by": "v7, pull-owned (/pulls merged_by login). A listing-only re-ingest preserves it - the AC3 COALESCE-on-absent rule reproduced on merge_commit before v7",
        "comment_count": "v7, DERIVED: the stored-row count of comments for the item, recomputed on every ingest and by the migration - not a payload field",
        "paths_text": "v7, DERIVED: space-joined pr_files paths (ORDER BY path), recomputed on every ingest - the search surface for touched paths, weighted below title/body",
    },
    "comments": {
        "author_association": "v7. REST supplies it natively; GraphQL maps authorAssociation when the capability probe reports the field. A probe-degraded re-ingest preserves the stored value via the pre-replacement snapshot",
    },
    "revisions": {
        "id": "v8. Sequence order for one item's displaced-text history; assigned at insert and never updated. doctor --consolidate is the one sanctioned rewrite (fresh ids, per-item order preserved)",
        "content": "Exactly the displaced stored value - a pre-image, so first ingest writes nothing and unchanged re-ingests write nothing. NULL is legal and meaningful: the text was absent before this version",
        "origin": "'edit' (replaced by a newer payload) or 'deleted' (vanished from a fetch whose children_complete flag was True). A degraded fetch never writes tombstones, and a degraded absence still replaces children - so an absence first seen degraded leaves no trace for a later complete fetch to tombstone (documented boundary)",
        "valid_from": "GitHub time this version became current: updated_at else created_at (items/comments); submitted_at else the owning item's created_at (reviews). Never the sync wall clock",
        "valid_to": "GitHub time it was displaced: the incoming item's updated_at, except comment edits prefer the comment's own incoming updated_at",
    },
    "item_path_refs": {
        "origin": "v9. Which text the path came from: 'body', 'issue_comment:<github_id>', 'review_comment:<github_id>', or 'review:<id>'. Item-scoped, replaced with the other children on every re-ingest",
        "path": "v9. The RAW string as mentioned in text (frames, path:L:C tokens, same-repo blob URLs, backticked segments) - deliberately UNRESOLVED: resolution against the known file vocabulary happens at READ time (issue #29), never at ingest, so rows stay independent of sync order",
        "backticked": "v9. 1 when the path appeared inside backticks in its text",
    },
    "item_signatures": {
        "exception": "v9. The exception class name verbatim from a fenced block (e.g. TypeError)",
        "error_line": "v9. The exception line with run-varying constants masked (0x-hex -> '<hex>', digit runs -> '<n>') so the signature is stable across runs that differ only in addresses and line numbers",
        "frame_function": "v9. The nearest preceding Python frame's function name when the block carries frames; NULL otherwise",
    },
    "item_events": {
        "type": "v10. Timeline event types stored as rows (currently 'reopened'); these never become edges. Replaced only when the timeline section was fetched COMPLETE - a degraded re-ingest (REST source, 404, capability-dropped, truncation cap) leaves stored rows untouched",
        "created_at": "v10. GitHub time of the event; NOT NULL - a malformed event with no stamp is skipped rather than stored with a fabricated identity",
    },
    "pr_commits": {
        "sha": "v10. The commit oid; one row per commit of a PR. Replaced only when the commits section was fetched complete",
        "message": "v10. messageHeadline and messageBody composed; closes_keyword edges with 'commit <sha>' provenance derive from these RETAINED rows on every ingest, so a REST or overflowed re-ingest never retracts them",
    },
    "sync_state": {
        "issues_since": "Watermark passed back verbatim and inclusive; never adjusted",
        "last_error": "Non-null means the last sync stopped early - re-run sync",
        "item_count": "Finish-time snapshot: recomputed at sync finish (and by doctor --consolidate / migrations), so it can lag committed rows after an interrupted run",
        "comment_count": "Finish-time snapshot: recomputed at sync finish (and by doctor --consolidate / migrations), so it can lag committed rows after an interrupted run",
        "edge_count": "Finish-time snapshot: recomputed at sync finish (and by doctor --consolidate / migrations), so it can lag committed rows after an interrupted run",
    },
}

_TABLE_SCOPE = (
    "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
    " AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
    " AND name NOT LIKE '%\\_fts%' ESCAPE '\\'"
    " ORDER BY name"
)


def _columns(conn: sqlite3.Connection, table: str) -> list[dict]:
    # Double any embedded quote so a hand-crafted table name cannot break
    # out of the PRAGMA identifier (mode=ro bounds the blast radius, but
    # the honest form is to quote properly).
    rows = conn.execute(f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")').fetchall()
    out = []
    for row in rows:
        name, col_type, notnull = row[1], row[2], row[3]
        note = COLUMN_NOTES.get(table, {}).get(name)
        out.append(
            {
                "name": name,
                "type": col_type,
                "notnull": bool(notnull),
                "note": note,
            }
        )
    return out


def describe_schema(conn: sqlite3.Connection, table: str | None = None) -> dict:
    """Live-DB DDL plus curated notes for one table or every table.

    Raises LookupError when `table` names no known table (the CLI maps that
    to a not_found error envelope)."""
    if table is not None:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchall()
        if not rows:
            raise LookupError(table)
    else:
        rows = conn.execute(_TABLE_SCOPE).fetchall()
    tables = []
    for row in rows:
        name = row[0]
        tables.append(
            {
                "name": name,
                "ddl": row[1],
                "columns": _columns(conn, name),
            }
        )
    return {"tables": tables}
