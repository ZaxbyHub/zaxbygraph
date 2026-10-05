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
        "evidence": "Provenance payload; never part of an edge's identity - `source` IS part of the key because the same pair reported by two streams is two facts",
        "source": "Provenance stream: 'keyword' (text patterns), 'timeline' (closed/cross-referenced events), 'closing_ref' (PR closingIssuesReferences), 'payload' (structured fields)",
    },
    "pr_files": {
        "patch": "NULL unless the sync used --include-patches",
    },
    "items": {
        "id": "Comes from the issues-list payload; never overwritten by the /pulls merge",
    },
    "sync_state": {
        "issues_since": "Watermark passed back verbatim and inclusive; never adjusted",
        "last_error": "Non-null means the last sync stopped early - re-run sync",
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
