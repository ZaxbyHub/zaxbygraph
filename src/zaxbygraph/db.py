from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

SCHEMA_NAME = "schema.sql"
SCHEMA_VERSION = "1"

#: `upsert_item_row` in store.py uses two ON CONFLICT clauses in one INSERT,
#: which SQLite only parses from 3.35.0 (2021-03-12). Without this check an
#: older build fails with a bare syntax error far from the cause.
MIN_SQLITE_VERSION = (3, 35, 0)


def assert_sqlite_supported() -> None:
    """Fail early and legibly on a too-old SQLite."""
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        want = ".".join(str(n) for n in MIN_SQLITE_VERSION)
        raise RuntimeError(
            f"zaxbygraph requires SQLite >= {want}, but this Python is linked "
            f"against {sqlite3.sqlite_version}. Upgrade SQLite or use a newer "
            "Python build."
        )


def connect(db_path: Path) -> sqlite3.Connection:
    assert_sqlite_supported()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


#: PRAGMAs that a read-only query is allowed to trigger indirectly (never
#: written by user SQL, since PRAGMA cannot appear inside a SELECT/WITH/
#: EXPLAIN statement, but issued internally by SQLite/FTS5 while resolving
#: a plain read). `data_version` is a read-only counter used for cache
#: invalidation.
_ALLOWED_INTERNAL_PRAGMAS = {"data_version"}


def _deny_write_authorizer(
    action: int,
    arg1: str | None,
    arg2: str | None,
    dbname: str | None,
    trigger: str | None,
) -> int:
    """`sqlite3.Connection.set_authorizer` callback for the read-only `sql`
    escape hatch.

    Defense in depth alongside `PRAGMA query_only = ON` and `mode=ro`:
    those alone do not stop `PRAGMA query_only = OFF`, `ATTACH`ing a second
    writable database file, or `load_extension`. This authorizer denies
    every mutating/schema/attach/pragma/transaction action outright and
    allows only what a plain read (including recursive CTEs and FTS5
    MATCH/snippet/bm25 queries) needs.
    """
    if action in (
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_RECURSIVE,
    ):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        name = (arg2 or arg1 or "").lower()
        return sqlite3.SQLITE_DENY if name == "load_extension" else sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or "").lower()
        return (
            sqlite3.SQLITE_OK
            if name in _ALLOWED_INTERNAL_PRAGMAS
            else sqlite3.SQLITE_DENY
        )
    # Default-deny: covers SQLITE_ATTACH, SQLITE_DETACH, SQLITE_INSERT,
    # SQLITE_UPDATE, SQLITE_DELETE, every SQLITE_CREATE_*/SQLITE_DROP_*,
    # SQLITE_ALTER_TABLE, SQLITE_REINDEX, SQLITE_TRANSACTION,
    # SQLITE_SAVEPOINT, and anything not explicitly allowed above.
    return sqlite3.SQLITE_DENY


def connect_readonly_query(db_path: Path) -> sqlite3.Connection:
    """Separate connection for the sql command: query_only, no extensions,
    and a connection authorizer that denies writes/attach/pragma as a
    second, independent layer of defense. Used only by the `sql` CLI path —
    never share this connection factory with the read/write paths."""
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.enable_load_extension(False)
    except AttributeError:
        pass
    except sqlite3.OperationalError:
        pass
    conn.set_authorizer(_deny_write_authorizer)
    return conn


def _schema_sql() -> str:
    return files("zaxbygraph").joinpath(SCHEMA_NAME).read_text(encoding="utf-8")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_schema_sql())
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])
