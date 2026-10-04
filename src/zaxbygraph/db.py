from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

SCHEMA_NAME = "schema.sql"
SCHEMA_VERSION = "5"

#: `PRAGMA user_version` is the authoritative schema state. Databases created
#: before this framework (v1) carry 0 with the tables already present and are
#: migrated in place; fresh databases are created at the current shape. The
#: `meta.schema_version` row is informational only.
CURRENT_USER_VERSION = 5

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
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
    except BaseException:
        # A PRAGMA failure (e.g. "file is not a database") must not orphan an
        # open handle: on Windows that locks the file against cleanup.
        conn.close()
        raise
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


def open_existing(db_path: Path) -> sqlite3.Connection:
    """Open an existing graph read-only; NEVER create anything (issue #2).

    Read commands route through this. mode=ro cannot create the file, and
    no PRAGMA here mutates it. On a WAL-mode database the open may
    materialize -shm/-wal beside it (documented; those are the store's own
    files and hold no new data — legacy candidates that must not be touched
    at all are read from temp copies by doctor/where). Raises
    FileNotFoundError when the file is absent; a corrupt file surfaces as
    sqlite3.DatabaseError from the first statement.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
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
    conn.execute("DROP TABLE IF EXISTS _fold")
    conn.execute(f"CREATE TEMP TABLE _fold AS SELECT {collist} FROM main.{table} WHERE 0")
    conn.execute(f"INSERT INTO _fold SELECT {collist} FROM main.{table} ORDER BY {order_by}")
    conn.execute(f"DELETE FROM main.{table}")
    # ORDER BY rowid keeps the staging table's insertion order (and therefore
    # the freshest-first survivor rule) SQL-guaranteed, not scan-order luck.
    conn.execute(
        f"INSERT INTO main.{table} ({collist}) "
        f"SELECT {lowered} FROM _fold WHERE true ORDER BY rowid {conflict_clause}"
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

    The explicit INSERT column list below is a snapshot of the PRE-v4 shape
    and is order-dependent: MIGRATIONS runs v1->v2 before v3->v4, so the
    rate-limit columns do not exist yet when this fold runs. Adding a v5
    column requires revisiting this list (or copying via PRAGMA table_info).

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
        conn, "edges", _edges_conflict_clause(conn), "id DESC"
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


#: FTS objects as schema.sql creates them at the current shape (porter
#: tokenizer). Kept as individual statements — NEVER run through
#: `executescript`, which implicitly COMMITs the migration's open
#: `BEGIN IMMEDIATE` and would break the rollback-whole guarantee that
#: `test_migration_failure_rolls_back` (and the migration framework's
#: per-migration transaction contract) relies on. The parity test
#: (`test_fresh_shape_and_migrated_db_share_tokenizer`) pins this copy and
#: schema.sql's copy to the same tokenizer behavior.
_FTS_PORTER_STATEMENTS = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
        title,
        body,
        labels_text,
        tokenize = 'porter unicode61',
        content='items',
        content_rowid='id'
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5(
        body,
        tokenize = 'porter unicode61',
        content='comments',
        content_rowid='pk'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
        INSERT INTO items_fts(rowid, title, body, labels_text)
        VALUES (new.id, new.title, new.body, new.labels_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
        INSERT INTO items_fts(items_fts, rowid, title, body, labels_text)
        VALUES ('delete', old.id, old.title, old.body, old.labels_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
        INSERT INTO items_fts(items_fts, rowid, title, body, labels_text)
        VALUES ('delete', old.id, old.title, old.body, old.labels_text);
        INSERT INTO items_fts(rowid, title, body, labels_text)
        VALUES (new.id, new.title, new.body, new.labels_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS comments_ai AFTER INSERT ON comments BEGIN
        INSERT INTO comments_fts(rowid, body) VALUES (new.pk, new.body);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS comments_ad AFTER DELETE ON comments BEGIN
        INSERT INTO comments_fts(comments_fts, rowid, body)
        VALUES ('delete', old.pk, old.body);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS comments_au AFTER UPDATE ON comments BEGIN
        INSERT INTO comments_fts(comments_fts, rowid, body)
        VALUES ('delete', old.pk, old.body);
        INSERT INTO comments_fts(rowid, body) VALUES (new.pk, new.body);
    END
    """,
)


def migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """v2 -> v3: rebuild both FTS tables with the porter tokenizer.

    Dropping an external-content FTS table drops its sync triggers with it,
    so the six triggers are recreated alongside the tables, and
    `INSERT INTO <fts>(<fts>) VALUES('rebuild')` repopulates each index from
    its content table — no resync of items/comments is needed. Every
    statement here is per-statement `execute` (see _FTS_PORTER_STATEMENTS
    for why executescript is forbidden). No-op-safe on a fresh database,
    where executescript already created the porter tables and the migration
    loop still runs: drop/recreate/rebuild of already-current tables is
    harmless.
    """
    for trigger in (
        "items_ai",
        "items_ad",
        "items_au",
        "comments_ai",
        "comments_ad",
        "comments_au",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE IF EXISTS items_fts")
    conn.execute("DROP TABLE IF EXISTS comments_fts")
    for statement in _FTS_PORTER_STATEMENTS:
        conn.execute(statement)
    conn.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO comments_fts(comments_fts) VALUES('rebuild')")


def migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """v3 -> v4: `sync_state` rate-limit columns (issue #5).

    `rate_limit_remaining` is `0` when the last clean run observed its
    budget at the floor (it slept a window through, or proceeded with the
    floor already reached) and NULL otherwise; `rate_limit_reset_at` carries
    the reset instant of the window or observation.
    Two guarded ALTERs, idempotent on any shape.
    """
    if not _column_exists(conn, "sync_state", "rate_limit_remaining"):
        conn.execute("ALTER TABLE sync_state ADD COLUMN rate_limit_remaining INTEGER")
    if not _column_exists(conn, "sync_state", "rate_limit_reset_at"):
        conn.execute("ALTER TABLE sync_state ADD COLUMN rate_limit_reset_at TEXT")


def _edges_conflict_clause(conn: sqlite3.Connection) -> str:
    """The ON CONFLICT target for the edges table AS IT EXISTS RIGHT NOW.

    Fresh databases run the whole migration chain on the CURRENT shape, so by
    the time migrate_v1_to_v2's fold runs, edges may already carry `source`
    inside its unique key (v5) or not (a genuine legacy v1 table). SQLite
    validates a conflict target against the table's unique indexes at prepare
    time — a hardcoded literal cannot serve both shapes (issue #6)."""
    if _column_exists(conn, "edges", "source"):
        return "ON CONFLICT(repo, src_type, src_id, rel, dst_type, dst_id, source) DO NOTHING"
    return "ON CONFLICT(repo, src_type, src_id, rel, dst_type, dst_id) DO NOTHING"


def _edges_unique_key_has_source(conn: sqlite3.Connection) -> bool:
    for index in conn.execute("PRAGMA index_list(edges)").fetchall():
        if not index["unique"]:
            continue
        columns = [
            row[2]
            for row in conn.execute(f"PRAGMA index_info({index['name']})").fetchall()
        ]
        if "source" in columns:
            return True
    return False


def migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """v4 -> v5: `edges.source` provenance + the closes_keyword rename
    (issue #6).

    Rebuilds the edges table: `source TEXT NOT NULL` appended after evidence,
    the unique key widened to include it (the same pair reported by two
    provenance streams is two facts), keyword `closes` rows renamed to
    `closes_keyword`, and a mechanism-honest backfill — text-derived rows
    (closes, mentions) get source='keyword', structured-payload rows
    (authored, has_label, commented, reviewed, touches) get source='payload'.
    Row count and ids are preserved; the three indexes are recreated.
    Idempotent: no-op when the table already carries `source` inside its
    unique key, because fresh databases run the whole migration chain on the
    current shape."""
    if _column_exists(conn, "edges", "source") and _edges_unique_key_has_source(conn):
        return
    conn.execute("DROP INDEX IF EXISTS idx_edges_src")
    conn.execute("DROP INDEX IF EXISTS idx_edges_dst")
    conn.execute("DROP INDEX IF EXISTS idx_edges_rel")
    conn.execute("ALTER TABLE edges RENAME TO edges_v4")
    conn.execute(
        """
        CREATE TABLE edges (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            repo       TEXT NOT NULL,
            src_type   TEXT NOT NULL,
            src_id     TEXT NOT NULL,
            rel        TEXT NOT NULL,
            dst_type   TEXT NOT NULL,
            dst_id     TEXT NOT NULL,
            confidence TEXT NOT NULL CHECK (confidence IN ('EXTRACTED')),
            evidence   TEXT,
            source     TEXT NOT NULL,
            UNIQUE (repo, src_type, src_id, rel, dst_type, dst_id, source)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO edges (id, repo, src_type, src_id, rel, dst_type, dst_id,
                           confidence, evidence, source)
        SELECT id, repo, src_type, src_id,
               CASE WHEN rel = 'closes' THEN 'closes_keyword' ELSE rel END,
               dst_type, dst_id, confidence, evidence,
               CASE WHEN rel IN ('closes', 'mentions') THEN 'keyword' ELSE 'payload' END
        FROM edges_v4
        """
    )
    conn.execute("DROP TABLE edges_v4")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(repo, src_type, src_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(repo, dst_type, dst_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_rel ON edges(rel)")


#: Forward-only, ordered. Each entry runs in its own transaction that ends by
#: stamping `PRAGMA user_version` (the pragma is transactional).
MIGRATIONS = [
    (2, migrate_v1_to_v2),
    (3, migrate_v2_to_v3),
    (4, migrate_v3_to_v4),
    (5, migrate_v4_to_v5),
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
