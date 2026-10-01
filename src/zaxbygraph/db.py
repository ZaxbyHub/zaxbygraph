from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

SCHEMA_NAME = "schema.sql"
SCHEMA_VERSION = "2"

#: `PRAGMA user_version` is the authoritative schema state. Databases created
#: before this framework (v1) carry 0 with the tables already present and are
#: migrated in place; fresh databases are created at the current shape. The
#: `meta.schema_version` row is informational only.
CURRENT_USER_VERSION = 2

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


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _fold_dedupe_table(
    conn: sqlite3.Connection,
    table: str,
    conflict_clause: str,
    order_by: str,
) -> None:
    """Case-fold `repo` and drop duplicates on folded keys, freshest first.

    Copies the table into a temp table (an ordered INSERT ... SELECT preserves
    insertion order, unlike CREATE TABLE AS SELECT), clears the real table —
    the external-content FTS triggers keep the index consistent through the
    delete/reinsert — and re-inserts with ON CONFLICT ... DO NOTHING so the
    first (winning) row survives each folded key. `WHERE true` disambiguates
    the upsert parse after INSERT ... SELECT.
    """
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    collist = ", ".join(cols)
    lowered = ", ".join("lower(repo)" if c == "repo" else c for c in cols)
    conn.execute(f"CREATE TEMP TABLE _fold AS SELECT {collist} FROM main.{table} WHERE 0")
    conn.execute(f"INSERT INTO _fold SELECT {collist} FROM main.{table} ORDER BY {order_by}")
    conn.execute(f"DELETE FROM main.{table}")
    conn.execute(
        f"INSERT INTO main.{table} ({collist}) "
        f"SELECT {lowered} FROM _fold WHERE true {conflict_clause}"
    )
    conn.execute("DROP TABLE _fold")


def _drop_collision_losers(conn: sqlite3.Connection) -> None:
    """Resolve distinct-id `(repo, number)` collisions before folding.

    Impossible for case-splits of one live repo (both casings carry identical
    GitHub `id`s and `number`s), but possible when a casing's corpus predates
    a repo delete-and-recreate or rows were hand-built. The winner is the
    freshest row (`updated_at DESC, id DESC` — the same rule as the items
    fold); every loser's number-scoped children and edges are deleted under
    the loser's own casing, so one item's rows cannot attach to another
    item's number. Children stranded under the *winner's* casing by the
    pre-migration corruption are indistinguishable from the winner's own and
    survive; `sync --force` rebuilds children truthfully.
    """
    collisions = conn.execute(
        "SELECT lower(repo) AS lr, number, COUNT(DISTINCT id) AS c "
        "FROM items GROUP BY lower(repo), number HAVING c > 1"
    ).fetchall()
    for lr, number, _count in collisions:
        winner_id = conn.execute(
            "SELECT id FROM items WHERE lower(repo) = ? AND number = ? "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (lr, number),
        ).fetchone()[0]
        loser_casings = conn.execute(
            "SELECT DISTINCT repo FROM items WHERE lower(repo) = ? AND number = ? AND id != ?",
            (lr, number, winner_id),
        ).fetchall()
        nid = str(number)
        for (casing,) in loser_casings:
            conn.execute("DELETE FROM labels WHERE repo = ? AND number = ?", (casing, number))
            conn.execute("DELETE FROM comments WHERE repo = ? AND number = ?", (casing, number))
            conn.execute("DELETE FROM reviews WHERE repo = ? AND number = ?", (casing, number))
            conn.execute("DELETE FROM pr_files WHERE repo = ? AND number = ?", (casing, number))
            conn.execute(
                "DELETE FROM edges WHERE repo = ? AND ("
                "(src_type = 'item' AND src_id = ?) OR (dst_type = 'item' AND dst_id = ?))",
                (casing, nid, nid),
            )


def _fold_sync_state(conn: sqlite3.Connection) -> None:
    """Merge case-split `sync_state` rows, column by column.

    Winner = greatest `issues_since` (NULL ranks lowest, ties by rowid).
    Timestamps and `include_patches` take MAX so no real sync fact is lost;
    counts are recomputed afterwards. `full_sync_pending` is never carried
    from a loser: it is derived from the merged timestamp.
    """
    rows = conn.execute("SELECT rowid AS rid, * FROM sync_state ORDER BY rowid").fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["repo"].lower(), []).append(row)
    for lower_repo, group in groups.items():
        if len(group) == 1 and group[0]["repo"] == lower_repo:
            continue
        winner = max(group, key=lambda r: (r["issues_since"] or "", r["rid"]))
        full = max(
            (r["last_full_sync_at"] for r in group if r["last_full_sync_at"]), default=None
        )
        incr = max(
            (r["last_incr_sync_at"] for r in group if r["last_incr_sync_at"]), default=None
        )
        patches = max(r["include_patches"] for r in group)
        for row in group:
            conn.execute("DELETE FROM sync_state WHERE rowid = ?", (row["rid"],))
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at, last_incr_sync_at,"
            " last_error, item_count, comment_count, edge_count, include_patches,"
            " full_sync_pending) VALUES (?,?,?,?,?,0,0,0,?,?)",
            (
                lower_repo,
                winner["issues_since"],
                full,
                incr,
                winner["last_error"],
                patches,
                1 if full is None else 0,
            ),
        )

def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 -> v2: `sync_state.full_sync_pending` + lowercase repo identity.

    Runs inside one transaction owned by init_schema. Idempotent: every step
    is guarded or a no-op on already-folded data.
    """
    if not _column_exists(conn, "sync_state", "full_sync_pending"):
        conn.execute(
            "ALTER TABLE sync_state ADD COLUMN full_sync_pending INTEGER NOT NULL DEFAULT 0"
        )
    _drop_collision_losers(conn)
    _fold_dedupe_table(
        conn, "items",
        "ON CONFLICT(id) DO NOTHING ON CONFLICT(repo, number) DO NOTHING",
        "updated_at DESC, id DESC",
    )
    _fold_dedupe_table(
        conn, "labels",
        "ON CONFLICT(repo, number, name) DO NOTHING",
        "repo DESC, number DESC, name DESC",
    )
    _fold_dedupe_table(
        conn, "comments",
        "ON CONFLICT(repo, kind, github_id) DO NOTHING",
        "updated_at DESC, pk DESC",
    )
    _fold_dedupe_table(
        conn, "pr_files",
        "ON CONFLICT(repo, number, path) DO NOTHING",
        "repo DESC, number DESC, path DESC",
    )
    _fold_dedupe_table(
        conn, "releases",
        "ON CONFLICT(repo, tag_name) DO NOTHING",
        "id DESC",
    )
    _fold_dedupe_table(
        conn, "edges",
        "ON CONFLICT(repo, src_type, src_id, rel, dst_type, dst_id) DO NOTHING",
        "id DESC",
    )
    conn.execute("UPDATE reviews SET repo = lower(repo)")
    conn.execute("UPDATE fetch_log SET repo = lower(repo)")
    _fold_sync_state(conn)
    # Single rule, applied once at the end: completeness is derived from the
    # merged timestamps, never carried per-loser. Legacy rows that never
    # finished a full sync stay pending (complete: false) until a clean run
    # proves the corpus — the migration cannot know the listing was drained.
    conn.execute(
        "UPDATE sync_state SET full_sync_pending = "
        "CASE WHEN last_full_sync_at IS NULL THEN 1 ELSE 0 END"
    )
    from zaxbygraph.store import recount  # local import: store owns the counts

    for (repo,) in conn.execute("SELECT DISTINCT repo FROM sync_state").fetchall():
        recount(conn, repo)


#: Forward-only, ordered. Each entry runs in its own transaction that ends by
#: stamping `PRAGMA user_version` (the pragma is transactional).
MIGRATIONS = [
    (2, migrate_v1_to_v2),
]


def init_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > CURRENT_USER_VERSION:
        raise RuntimeError(
            f"database schema version {version} is newer than this build supports "
            f"({CURRENT_USER_VERSION}); upgrade zaxbygraph"
        )
    # Idempotent create-if-missing at the current shape; on an existing v1
    # database every statement is a no-op, leaving the migration to do the work.
    conn.executescript(_schema_sql())
    for target, migrate in MIGRATIONS:
        if version >= target:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            migrate(conn)
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        version = target
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
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
