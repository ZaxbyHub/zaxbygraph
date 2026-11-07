from __future__ import annotations

import json
import os
import sqlite3
from importlib.resources import files
from pathlib import Path

from zaxbygraph.extract import (
    CLOSES_REF_MARKER,
    merge_evidence,
)

SCHEMA_NAME = "schema.sql"
SCHEMA_VERSION = "11"

#: `PRAGMA user_version` is the authoritative schema state. Databases created
#: before this framework (v1) carry 0 with the tables already present and are
#: migrated in place; fresh databases are created at the current shape. The
#: `meta.schema_version` row is informational only.
CURRENT_USER_VERSION = 11

#: Feature gates compare against the slot that INTRODUCED a capability, never
#: against CURRENT_USER_VERSION: a read-only store several slots behind must
#: keep every feature its own schema supports (reads never migrate). v7
#: payload-signal reads gate on _V7_USER_VERSION; v8 revision history on
#: _V8_USER_VERSION; v9 text-derived references and v10 link events/commits
#: on their own slots; v11 fetch-source persistence gates on
#: _V11_USER_VERSION. A v12 slot MUST re-pin all five to their slots.
_V7_USER_VERSION = 7
_V8_USER_VERSION = 8
_V9_USER_VERSION = 9
_V10_USER_VERSION = 10
_V11_USER_VERSION = 11

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


def assert_fts5_available(conn: sqlite3.Connection) -> None:
    """Fail legibly when this SQLite build lacks FTS5 (issue #31 AC2).

    The corpus schema creates fts5 virtual tables (items_fts, comments_fts,
    reviews_fts), so on a build without FTS5 every read would surface as a
    bare "no such module: fts5" SQL error mid-query. The compile-option
    probe is a plain read (SQLITE_FUNCTION allows non-extension functions,
    so it also passes the `sql` command's default-deny authorizer) and is
    validated across the supported CI matrix (win/ubuntu x 3.11-3.14). The
    probe checks build metadata only and never touches database pages, so a
    corrupt file does NOT raise here (on newer SQLite builds the prepared
    statement itself fails on a corrupt header — also surfacing as
    DatabaseError); either way corruption keeps the store_unreadable
    contract from the first file-touching statement, never a guard
    misclassification (PR #47 review)."""
    row = conn.execute(
        "SELECT sqlite_compileoption_used('ENABLE_FTS5')"
    ).fetchone()
    if not row or not row[0]:
        raise RuntimeError(
            "zaxbygraph requires a SQLite build with FTS5 enabled, but this"
            f" Python is linked against {sqlite3.sqlite_version} without"
            " SQLITE_ENABLE_FTS5. Use a Python/SQLite build compiled with"
            " FTS5."
        )


#: PRAGMAs that a read-only query is allowed to trigger indirectly (never
#: written by user SQL, since PRAGMA cannot appear inside a SELECT/WITH/
#: EXPLAIN statement, but issued internally by SQLite/FTS5 while resolving
#: a plain read). `data_version` is a read-only counter used for cache
#: invalidation.
_ALLOWED_INTERNAL_PRAGMAS = {"data_version"}

#: The three characters that are significant to SQLite's URI parser inside a
#: path component: '#' starts a fragment, '?' starts the query, and '%XX' is a
#: percent-escape. Percent-encoding them keeps a filename that contains them
#: intact as data (issue #55).
_URI_PATH_ESCAPE = str.maketrans({"#": "%23", "?": "%3F", "%": "%25"})


def _readonly_uri(db_path: Path) -> str:
    """Build the SQLite URI for a read-only open of `db_path`.

    `sqlite3.connect(..., uri=True)` parses its filename as a URI, not as a
    path, so a filename carrying a URI metacharacter is read as syntax: a
    '#' truncates the path at the fragment and swallows `mode=ro` into it
    (the connection then opens the truncated path in default
    read-write-create mode), and a '%XX' decodes to a different existing
    file. Both are silent wrong-file reads (issue #55 AC1/AC2).

    `Path.as_uri()` percent-encodes every character significant to the URI
    grammar, which is why `snapshot._open_readonly` uses it. Two shapes it
    cannot express are handled explicitly:

    * a **relative** path — `as_uri()` raises ValueError, so the path is made
      absolute. Only the Windows drive-relative shape needs `os.path.abspath`
      (a `C:foo` spelling stays relative under both `Path.cwd() / p` and
      `Path.absolute()`, and `abspath` is the one form that resolves it).
      Every other relative spelling is joined to the cwd verbatim, because
      `abspath` collapses `..` lexically while opening the original spelling
      resolves a symlink first and then applies `..` — on POSIX those can
      select different files, so normalizing them here would silently change
      which database a read opens.
    * a **UNC** path — SQLite's URI parser rejects an authority component
      ("invalid uri authority: <host>"), and `as_uri()` always emits one, so
      the backslash spelling is the only form SQLite accepts. The three
      metacharacters are percent-encoded by hand there.

    Every caller of this helper has already established that the file exists;
    nothing here creates anything.
    """
    p = Path(db_path)
    if not p.is_absolute():
        p = Path(os.path.abspath(p)) if p.drive else Path.cwd() / p
    if p.drive.startswith("\\\\"):
        return "file:" + str(p).translate(_URI_PATH_ESCAPE) + "?mode=ro"
    return p.as_uri() + "?mode=ro"


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
    assert_sqlite_supported()
    conn = sqlite3.connect(_readonly_uri(db_path), uri=True)
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
    try:
        assert_fts5_available(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def open_existing(db_path: Path) -> sqlite3.Connection:
    """Open an existing graph read-only; NEVER create anything (issue #2).

    Read commands route through this. mode=ro cannot create the file, and
    no PRAGMA here mutates it. On a WAL-mode database the open may
    materialize -shm/-wal beside it (documented; those are the store's own
    files and hold no new data — legacy candidates that must not be touched
    at all are read from temp copies by doctor/where). Raises
    FileNotFoundError when the file is absent, RuntimeError from the
    SQLite-floor guard or the FTS5 probe (issue #31 AC2) when the runtime
    cannot serve the corpus, and a corrupt file surfaces as
    sqlite3.DatabaseError from the first statement.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    assert_sqlite_supported()
    conn = sqlite3.connect(_readonly_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        assert_fts5_available(conn)
    except BaseException:
        conn.close()
        raise
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _schema_sql() -> str:
    return files("zaxbygraph").joinpath(SCHEMA_NAME).read_text(encoding="utf-8")


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _index_exists(conn: sqlite3.Connection, index: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (index,)
    ).fetchone() is not None


def _quote_ident(name: str) -> str:
    """Quote a schema-derived identifier for SQL interpolation.

    The fold's statements are assembled from the CANDIDATE's own
    `PRAGMA table_info` output (issue #52), so a column name is untrusted
    text until quoted: an unquoted divergent name (e.g. `"order"`) parses
    as SQL and aborts the whole doctor command (issue #53, review PRR-005).
    """
    return '"' + name.replace('"', '""') + '"'


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
    the upsert parse after INSERT ... SELECT. Column names come from the
    candidate's schema and are quoted (issue #53, review PRR-005).
    """
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    collist = ", ".join(_quote_ident(c) for c in cols)
    lowered = ", ".join(
        "lower(repo)" if c == "repo" else _quote_ident(c) for c in cols
    )
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


#: Every repo-keyed item-scoped table whose rows die with their item when a
#: distinct-id `(repo, number)` collision is resolved (issue #52): the five
#: v1-era tables plus the item-scoped tables added by v7/v9/v10. Coverage
#: is tested against schema.sql by a census pin in tests/test_migrations.py.
_LOSER_CHILD_TABLES = (
    "labels",
    "comments",
    "reviews",
    "pr_files",
    "assignees",
    "issue_field_values",
    "revisions",
    "item_path_refs",
    "item_signatures",
    "item_events",
    "pr_commits",
)


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

    The per-loser delete covers EVERY repo-keyed item-scoped table
    (_LOSER_CHILD_TABLES), not only the five that existed when this helper
    was written (issue #52): docs/schema.md contracts the cleanup across
    "every repo-keyed table". Table existence is guarded so the migration
    stays portable if the schema is ever re-ordered.
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
            for table in _LOSER_CHILD_TABLES:
                if _table_exists(conn, table):
                    conn.execute(
                        f"DELETE FROM {table} WHERE repo = ? AND number = ?",
                        (casing, number),
                    )
            conn.execute(
                "DELETE FROM edges WHERE repo = ? AND ("
                "(src_type = 'item' AND src_id = ?) OR (dst_type = 'item' AND dst_id = ?))",
                (casing, nid, nid),
            )


def current_table_columns() -> dict[str, set[str]]:
    """Every table's column set in the CURRENT schema (issue #56).

    Built from an in-memory database created by the schema DDL, so it is
    the exact shape `init_schema` gives the store at adoption time. The
    adoption's copy loops read the candidate's columns into the store's,
    so a candidate that lacks a current column would be adopted with the
    store's defaults for it — a truncated image. Callers compare the
    candidate's `PRAGMA table_info` against this and refuse the gap.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(_schema_sql())
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        return {
            table: {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for table in tables
        }
    finally:
        conn.close()


def current_column_rules() -> dict[str, dict[str, tuple[str, bool]]]:
    """Every table's {column: (declared type, NOT NULL)} in the CURRENT
    schema (issue #56; swarm-pr-review N-14/N-18).

    Built from the same in-memory schema DDL as `current_table_columns`, so
    it is the exact shape `init_schema` gives the store at adoption time.
    ONE derived rule replaces the four enumerated per-column checks that
    preceded it: a column declared TEXT must hold text (or NULL when the
    DDL allows it), a column declared INTEGER must hold an integer, and a
    column declared NOT NULL must not be NULL — SQLite's dynamic typing
    stores anything a divergent schema allows, and the adoption copies the
    value verbatim into a store column that then rejects it either
    silently (a fabricated value) or loudly (an IntegrityError that aborts
    the whole command). Columns with no declared type carry anything and
    are not constrained. `sync_state.repo` is special-cased by the caller:
    a TEXT PRIMARY KEY on a rowid table permits NULL in SQLite, but the
    fold's grouping needs a real key.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(_schema_sql())
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        return {
            table: {r[1]: ((r[2] or "").upper(), bool(r[3]))
                    for r in conn.execute(f"PRAGMA table_info({table})")}
            for table in tables
        }
    finally:
        conn.close()


#: The optional TEXT state columns the fold carries from the winner (issue
#: #56): the DDL declares them TEXT, so a divergent storage class is
#: refused exactly as the compared columns are.
_OPTIONAL_SYNC_STATE_TEXTS = ("rate_limit_reset_at", "fetch_source")


def _text_value(row: sqlite3.Row, column: str) -> str | None:
    """A TEXT column's value, refusing any other storage class (issue #56).

    `sync_state`'s timestamp columns are declared TEXT; a divergent schema
    whose column lost its affinity can hold INTEGER or BLOB values, and
    comparing those against strings raises an uncaught TypeError that
    would abort the whole command. Raise `sqlite3.DatabaseError` instead,
    so the caller's handler converts it to an unreadable candidate —
    the migration path has no preflight in front of it, exactly as the
    flag columns' fail-closed raise covers `include_patches`.
    """
    if column not in row.keys():
        return None
    value = row[column]
    if value is None or isinstance(value, str):
        return value
    raise sqlite3.DatabaseError(
        f"sync_state.{column} holds a {type(value).__name__} value: a"
        " divergent storage class is refused"
    )


def _fold_sync_state(conn: sqlite3.Connection,
                     repo: str | None = None) -> None:
    """Merge case-split `sync_state` rows, column by column.

    The insert is driven by `PRAGMA table_info`, NOT a hardcoded column
    list: a snapshot list silently drops every column added after it was
    written (the pre-v4 list would lose the v4 rate-limit columns and the
    v11 `fetch_source` — issue #52). Winner = greatest `issues_since`
    (NULL ranks lowest, ties by rowid). Timestamps and `include_patches`
    take MAX so no real sync fact is lost; the newer columns carry the
    winner's values; counts are recomputed afterwards.
    `full_sync_pending` is never carried from a loser: it is derived from
    the merged timestamp.

    `repo` scopes the fold to one identity (case-insensitively). The
    adoption passes it, because a candidate's rows for a repo the user is
    NOT consolidating are never copied into the store, so a malformed row
    there must not refuse the adoption — the same isolation the preflight's
    repo-scoped storage-class count gives (swarm-pr-review N-09; cubic
    review, PR #58). Only the per-IDENTITY steps are scoped: a NULL or
    non-text `repo` is still refused whole-table, because it is a broken
    primary key rather than another identity's row, and the fold's grouping
    genuinely needs a real key for it (issue #56; C14's null-repo shape
    pins that refusal).

    Reads go through `_text_value`, which tolerates an ABSENT column (a
    mid-migration schema shape) by returning None — the aggregation's
    natural default — instead of raising IndexError, which is not a
    `sqlite3.DatabaseError` and would abort the whole command (issue #56,
    AC4/AC5). A non-integer value in the NOT NULL flag
    column is a fabricated state no genuine store can produce (the DDL
    declares it INTEGER NOT NULL DEFAULT 0): the fold refuses with a
    `sqlite3.DatabaseError`, which `_MigratedCopy` converts to an
    unreadable candidate — adopting it would fabricate patch-retention
    state, and `max()` over mixed storage classes would raise an uncaught
    TypeError (issue #56, AC11/AC12).
    """
    rows = conn.execute("SELECT rowid AS rid, * FROM sync_state ORDER BY rowid").fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        # A NULL or non-text repo (SQLite permits NULL in a TEXT PRIMARY KEY
        # on a rowid table) would raise an uncaught AttributeError at
        # .lower() — the same isolation escape as a missing column, one
        # type up (issue #56; implementation-review round-3 finding 2).
        # Fail closed so the caller's sqlite3.DatabaseError handler
        # converts it to an unreadable candidate.
        repo_key = _text_value(row, "repo")
        if repo_key is None:
            raise sqlite3.DatabaseError(
                "sync_state.repo is NULL or non-text: a divergent key value"
                " is refused"
            )
        groups.setdefault(repo_key.lower(), []).append(row)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(sync_state)")]
    for lower_repo, group in groups.items():
        if repo is not None and lower_repo != repo.lower():
            continue  # not the consolidated identity: its rows are not copied
        if len(group) == 1 and group[0]["repo"] == lower_repo:
            continue
        # The timestamps are TEXT columns (or NULL); a divergent schema
        # whose column lost its affinity can hold INTEGER or BLOB values,
        # and max() over mixed storage classes raises an uncaught
        # TypeError — the same isolation escape as a missing column, one
        # type down (issue #56; implementation-review round-2 finding 1).
        # Fail closed so the caller's sqlite3.DatabaseError handler
        # converts it to an unreadable candidate.
        winner = max(
            group,
            key=lambda r: (_text_value(r, "issues_since") or "", r["rid"]),
        )
        full = max(
            (_text_value(r, "last_full_sync_at") for r in group
             if _text_value(r, "last_full_sync_at")), default=None
        )
        incr = max(
            (_text_value(r, "last_incr_sync_at") for r in group
             if _text_value(r, "last_incr_sync_at")), default=None
        )
        if "include_patches" in columns:
            values = [r["include_patches"] for r in group]
            # Fail closed on a heterogeneous group: max() over mixed
            # storage classes raises an uncaught TypeError (not a
            # sqlite3.DatabaseError), which would abort the whole command
            # — the same isolation escape as a missing column, one type
            # down (issue #56, AC11; implementation-review finding 1).
            if any(not isinstance(v, int) or isinstance(v, bool)
                   for v in values):
                raise sqlite3.DatabaseError(
                    "sync_state.include_patches holds a non-integer value in"
                    " a case-split group: a fabricated patch-retention state"
                    " is refused"
                )
            patches = max(values)
        else:
            patches = 0  # the column's own DDL default
        # A recorded fault is never merged away: the merged row keeps the
        # winner's last_error when it has one, else any casing's, so an
        # adopted corpus cannot report fault-free over a failed run
        # (AGENTS.md: a failing sync records last_error; issue #53, review
        # PRR-007). The test is `is not None`, not truthiness (cubic PR #54
        # round 1): a non-NULL but EMPTY last_error is still a recorded
        # state, and dropping it would let a corpus that stopped early
        # report complete.
        last_error = next(
            (_text_value(r, "last_error") for r in (winner, *group)
             if _text_value(r, "last_error") is not None),
            None,
        )
        for row in group:
            conn.execute("DELETE FROM sync_state WHERE rowid = ?", (row["rid"],))
        values: dict[str, object] = {}
        for column in columns:
            if column == "repo":
                values[column] = lower_repo
            elif column == "last_full_sync_at":
                values[column] = full
            elif column == "last_incr_sync_at":
                values[column] = incr
            elif column == "include_patches":
                values[column] = patches
            elif column == "last_error":
                values[column] = last_error
            elif column in ("item_count", "comment_count", "edge_count"):
                values[column] = 0
            elif column == "full_sync_pending":
                values[column] = 1 if full is None else 0
            elif column in _OPTIONAL_SYNC_STATE_TEXTS:
                # The optional TEXT state columns are carried from the
                # winner through the same storage-class guard the compared
                # columns use (issue #56): a BLOB value would be written
                # into the store's TEXT column verbatim, contradicting the
                # storage-class contract the docs promise. `_text_value`
                # refuses it (sqlite3.DatabaseError -> the caller converts
                # to an unreadable candidate).
                values[column] = _text_value(winner, column)
            else:
                keys = winner.keys()
                values[column] = winner[column] if column in keys else None
        conn.execute(
            f"INSERT INTO sync_state ({', '.join(_quote_ident(c) for c in values)}) "
            f"VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )


#: Every repo-keyed table the case-fold dedupes on its folded key, with the
#: conflict target for that table's declared uniqueness and the ordering
#: that picks the survivor (freshest-first for items, natural key order for
#: the composite-keyed children, rowid for the synthetic-pk ones). The
#: v1-era entries are the clauses migrate_v1_to_v2 has always used; the
#: post-v2 item-scoped tables join here with issue #52. `edges` is folded
#: separately (shape-aware clause plus the closes-pair handling).
_FOLD_TABLES = (
    ("items",
     "ON CONFLICT(id) DO NOTHING ON CONFLICT(repo, number) DO NOTHING",
     "updated_at DESC, id DESC"),
    ("labels",
     "ON CONFLICT(repo, number, name) DO NOTHING",
     "repo DESC, number DESC, name DESC"),
    ("comments",
     "ON CONFLICT(repo, kind, github_id) DO NOTHING",
     "updated_at DESC, pk DESC"),
    ("pr_files",
     "ON CONFLICT(repo, number, path) DO NOTHING",
     "repo DESC, number DESC, path DESC"),
    ("releases",
     "ON CONFLICT(repo, tag_name) DO NOTHING",
     "id DESC"),
    ("assignees",
     "ON CONFLICT(repo, number, login) DO NOTHING",
     "repo DESC, number DESC, login DESC"),
    ("issue_field_values",
     "ON CONFLICT(repo, number, field_name) DO NOTHING",
     "repo DESC, number DESC, field_name DESC"),
    ("revisions",
     "ON CONFLICT(id) DO NOTHING",
     "rowid"),
    ("item_path_refs",
     "ON CONFLICT(repo, number, origin, path) DO NOTHING",
     "rowid"),
    ("item_signatures",
     "ON CONFLICT(repo, number, exception, error_line) DO NOTHING",
     "rowid"),
    ("item_events",
     "ON CONFLICT(repo, number, type, created_at) DO NOTHING",
     "rowid"),
    ("pr_commits",
     "ON CONFLICT(repo, number, sha) DO NOTHING",
     "rowid"),
)


def _dedupe_revisions(conn: sqlite3.Connection) -> None:
    """Collapse byte-identical revision observations after the case-fold.

    `revisions` declares no unique key beyond its `id` (schema.sql:305-316),
    so the generic fold's `ON CONFLICT(id)` clause for it is inert: a
    case-split candidate's duplicate pre-images (same folded key, same
    payload, distinct ids) would both survive and be adopted twice
    (issue #53, review PRR-002). The observation's identity is its payload
    tuple; the lowest id survives each group. Distinct observations (a
    different edit window, entity, or content) never collapse.
    """
    conn.execute(
        "DELETE FROM revisions WHERE id NOT IN ("
        " SELECT MIN(id) FROM revisions GROUP BY repo, number, entity, entity_id,"
        " kind, origin, content, valid_from, valid_to)"
    )


#: Host parameters bound per statement. SQLite's ceiling
#: (SQLITE_MAX_VARIABLE_NUMBER) is 32,766 on the 3.35 floor and 327,666 on
#: newer builds; 500 is far below either, so an IN list built from an
#: aggregate id set can never exceed it.
_SQL_PARAM_CHUNK = 500


def _union_closes_same_source(conn: sqlite3.Connection) -> None:
    """Union evidence across FOLDED DUPLICATE groups of `rel='closes'`.

    Two dual-casing rows reporting the same closure through the same stream
    are ONE fact, so their evidence unions — with
    `reserve=(CLOSES_REF_MARKER,)`, exactly as production ingest unions a
    same-source closes refresh (store.py insert_edge). Without the reserve
    the marker is dropped once the merged evidence hits the 512-byte cap
    (issue #52, plan critic Round 4). Runs BEFORE the generic dedupe-fold so
    the fold can never discard a row whose evidence has not been merged.
    """
    groups = conn.execute(
        "SELECT lower(repo) AS lr, src_type, src_id, dst_type, dst_id, source,"
        " COUNT(*) AS n, GROUP_CONCAT(id) AS ids FROM edges WHERE rel = 'closes'"
        " GROUP BY lr, src_type, src_id, dst_type, dst_id, source HAVING n > 1"
    ).fetchall()
    for group in groups:
        # The ids come from the aggregate: re-selecting the members with a
        # lower(repo) predicate cannot use an index (a full scan per group),
        # which made the fold O(groups x edges) on large dual-cased
        # candidates (issue #53, review PRR-017). The IN list is bound in
        # chunks so a pathologically large duplicate group cannot exceed
        # SQLite's host-parameter ceiling (SQLITE_MAX_VARIABLE_NUMBER) and
        # abort the fold as an unusable candidate.
        ids = [int(x) for x in str(group["ids"]).split(",")]
        rows = []
        for start in range(0, len(ids), _SQL_PARAM_CHUNK):
            chunk = ids[start:start + _SQL_PARAM_CHUNK]
            rows.extend(
                conn.execute(
                    "SELECT id, evidence FROM edges WHERE id IN (%s)"
                    % ",".join("?" * len(chunk)),
                    chunk,
                ).fetchall()
            )
        rows.sort(key=lambda r: r["id"], reverse=True)
        merged = merge_evidence(
            *[r["evidence"] for r in rows], reserve=(CLOSES_REF_MARKER,)
        )
        conn.execute(
            "UPDATE edges SET evidence = ? WHERE id = ?", (merged, rows[0]["id"])
        )
        for other in rows[1:]:
            conn.execute("DELETE FROM edges WHERE id = ?", (other["id"],))


def _collapse_closes_pairs(conn: sqlite3.Connection) -> None:
    """At most one `closes` edge per (repo, dst, src) pair (issue #19).

    The two GitHub-reported closes streams — closingIssuesReferences
    (source='closing_ref') and the timeline closed event (source='timeline')
    — corroborate ONE closure fact, so stored pairs carrying both rows
    collapse: the closing_ref row survives when present (the structured
    declaration over the event stream), evidence merged first-seen via
    extract.merge_evidence, the other rows deleted. Extracted from
    migrate_v5_to_v6 so the doctor's case-fold (issue #52) reuses these
    survivor/evidence rules instead of reimplementing them; the partial
    UNIQUE index that makes the invariant physical is the caller's concern.
    """
    dupes = conn.execute(
        """
        SELECT repo, src_type, src_id, dst_type, dst_id
        FROM edges WHERE rel = 'closes'
        GROUP BY repo, src_type, src_id, dst_type, dst_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for pair in dupes:
        rows = conn.execute(
            """
            SELECT id, source, evidence FROM edges
            WHERE repo = ? AND src_type = ? AND src_id = ? AND rel = 'closes'
            AND dst_type = ? AND dst_id = ?
            ORDER BY (source = 'closing_ref') DESC, id
            """,
            (
                pair["repo"],
                pair["src_type"],
                pair["src_id"],
                pair["dst_type"],
                pair["dst_id"],
            ),
        ).fetchall()
        survivor = rows[0]
        merged = merge_evidence(
            *[r["evidence"] for r in rows], reserve=(CLOSES_REF_MARKER,)
        )
        conn.execute(
            "UPDATE edges SET evidence = ?, source = ? WHERE id = ?",
            (merged, survivor["source"], survivor["id"]),
        )
        for other in rows[1:]:
            conn.execute("DELETE FROM edges WHERE id = ?", (other["id"],))


def _prime_fts_for_fold(conn: sqlite3.Connection) -> None:
    """Repopulate an external-content index that a store predates.

    `_schema_sql()` creates `reviews_fts` (an external-content FTS5 index)
    and its three triggers with `IF NOT EXISTS` on EVERY open — including on
    a store from before schema v7, where neither object ever existed. Such a
    store's index is EMPTY while its `reviews` rows already exist, and FTS5's
    `'delete'` command — fired by the fold's
    `UPDATE reviews SET repo = lower(repo)` and by `_drop_collision_losers`'s
    per-loser delete — requires the rowid to BE in the index: it raises
    SQLITE_CORRUPT and the whole v1->v2 migration bricks, pinning
    `user_version` at 1 forever (executed probe: one reviews row, single
    casing, no distinct-id collision ->
    `sqlite3.DatabaseError: database disk image is malformed`).

    `COUNT(*)` over an external-content index is answered from the CONTENT
    table, so it cannot detect the anachronism; the schema slot can.
    Rebuilding first makes the index truthful, so the fold's writes keep it
    consistent through the triggers — the steady state `migrate_v6_to_v7`
    establishes, and which it re-establishes anyway (it drops and rebuilds
    these indexes), so nothing here outlives its slot. The `items` and
    `comments` indexes need no priming: both existed at v1 alongside their
    triggers, so they are already populated on every store that holds rows.
    """
    if not _table_exists(conn, "reviews_fts"):
        return
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) >= _V7_USER_VERSION:
        return  # post-v7: the index is populated and the triggers are correct
    conn.execute("INSERT INTO reviews_fts(reviews_fts) VALUES('rebuild')")


def fold_repo_casings(conn: sqlite3.Connection,
                      repo: str | None = None) -> None:
    """Collapse case-split repo identities to the folded slug.

    One mechanism, two callers: `migrate_v1_to_v2` (every store open below
    v2) and `doctor`'s consolidation (every candidate, any version —
    `_MigratedCopy` has already migrated the copy to the current shape, so
    the source-bearing steps are open there). Idempotent: a second call on
    already-folded data writes nothing.

    `repo` scopes the `sync_state` half of the fold to one identity, which
    the adoption passes so a malformed row for a repo it is not
    consolidating cannot refuse the candidate (swarm-pr-review N-09; cubic
    review, PR #58). The repo-keyed TABLE dedupes stay whole-table: they are
    conflict-target upserts over rows the copy filters by repo anyway, and
    scoping them would change the genuine single-repo behavior they exist
    to guarantee. A store the tool itself writes holds exactly one repo, so
    the two paths agree on every real input.

    Order matters: distinct-id collisions are resolved FIRST (so a loser's
    children can never attach to the winner's number), then the
    same-source closes evidence unions (so the generic dedupe can never
    discard a row whose evidence has not been merged), then every
    repo-keyed table dedupes on its folded key.

    The closes-specific steps (same-source union, cross-source collapse)
    are gated on the `edges.source` column: the genuine v1 `edges` table
    has no `source` column (it arrives with migrate_v4_to_v5, which also
    renames legacy keyword-derived `closes` rows to `closes_keyword`), so
    applying GitHub-backed closes semantics there would be wrong on two
    counts (issue #52, plan critic Round 5). In that window the fold keeps
    the shape-aware six-column edge clause and no index dance.

    `full_sync_pending` is derived on MERGED rows only (inside
    _fold_sync_state): the doctor path must not clobber a live
    crashed-mid-sync flag on a single-cased current store, so the
    migration's blanket derivation lives in migrate_v1_to_v2, not here.

    The fold REWRITES rows wholesale (delete + re-insert) and updates
    `reviews.repo` in place, so every external-content FTS index it touches
    must be populated first — `_prime_fts_for_fold` runs before any of it.
    """
    _prime_fts_for_fold(conn)
    _drop_collision_losers(conn)
    source_bearing = _column_exists(conn, "edges", "source")
    idx_existed = _index_exists(conn, "ux_edges_closes_pair")
    if idx_existed:
        conn.execute("DROP INDEX ux_edges_closes_pair")
    if source_bearing:
        _union_closes_same_source(conn)
    for table, clause, order in _FOLD_TABLES:
        if _table_exists(conn, table):
            _fold_dedupe_table(conn, table, clause, order)
    if _table_exists(conn, "revisions"):
        _dedupe_revisions(conn)
    _fold_dedupe_table(conn, "edges", _edges_conflict_clause(conn), "id DESC")
    if source_bearing:
        _collapse_closes_pairs(conn)
    if idx_existed:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_edges_closes_pair "
            "ON edges(repo, src_type, src_id, rel, dst_type, dst_id)"
            " WHERE rel = 'closes'"
        )
    conn.execute("UPDATE reviews SET repo = lower(repo)")
    conn.execute("UPDATE fetch_log SET repo = lower(repo)")
    _fold_sync_state(conn, repo)
    from zaxbygraph.store import recount  # local import: store owns the counts

    for (repo_name,) in conn.execute(
            "SELECT DISTINCT repo FROM sync_state").fetchall():
        recount(conn, repo_name)


def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 -> v2: `sync_state.full_sync_pending` + lowercase repo identity.

    Runs inside one transaction owned by init_schema. Idempotent: every step
    is guarded or a no-op on already-folded data. The fold itself lives in
    `fold_repo_casings` (shared with doctor's consolidation, issue #52).
    """
    if not _column_exists(conn, "sync_state", "full_sync_pending"):
        conn.execute(
            "ALTER TABLE sync_state ADD COLUMN full_sync_pending INTEGER NOT NULL DEFAULT 0"
        )
    fold_repo_casings(conn)
    # Single rule, applied once at the end: completeness is derived from the
    # merged timestamps, never carried per-loser. Legacy rows that never
    # finished a full sync stay pending (complete: false) until a clean run
    # proves the corpus — the migration cannot know the listing was drained.
    conn.execute(
        "UPDATE sync_state SET full_sync_pending = "
        "CASE WHEN last_full_sync_at IS NULL THEN 1 ELSE 0 END"
    )


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
    # The merge-commit lookup (issue #6's symmetric closes derivation) runs
    # once per commit-closed timeline event; without this index each lookup
    # scans every item row in the repo. Fresh databases get the same index
    # from schema.sql. IF NOT EXISTS keeps the no-op guard honest for
    # databases that already ran a schema.sql which included it.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_repo_merge ON items(repo, merge_commit)"
    )
    # Carry the AUTOINCREMENT high-water mark across the rebuild so ids once
    # issued to since-deleted edges are never re-issued (the docstring's "ids
    # are preserved" promise extends to the sequence).
    old_seq = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'edges_v4'"
    ).fetchone()
    if old_seq is not None:
        conn.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES ('edges', ?) "
            "ON CONFLICT(name) DO UPDATE SET seq = MAX(seq, excluded.seq)",
            (old_seq["seq"],),
        )


def migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """v5 -> v6: at most one `closes` edge per (repo, dst, src) pair (issue #19).

    The two GitHub-reported closes streams — closingIssuesReferences
    (source='closing_ref') and the timeline closed event (source='timeline')
    — corroborate ONE closure fact, so stored pairs carrying both rows
    collapse: the closing_ref row survives when present (the structured
    declaration over the event stream), evidence merged first-seen via
    extract.merge_evidence, the other rows deleted. The partial UNIQUE
    index then makes the invariant physical: any writer that bypasses the
    ingest-time merge (store.insert_edge) fails loudly.

    The index is created HERE, not in schema.sql: init_schema runs
    executescript(schema.sql) BEFORE the migration chain, so a UNIQUE index
    in the script would IntegrityError against a v5 store's
    not-yet-collapsed duplicates. Fresh databases get the index anyway —
    they enter init_schema at user_version 0 and this chain runs in full.
    Idempotent: a store with no duplicate pairs just gains (or already
    has) the index.

    The collapse itself lives in `_collapse_closes_pairs` (shared with the
    doctor case-fold, issue #52) so the survivor/evidence rules exist once.
    """
    _collapse_closes_pairs(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_edges_closes_pair "
        "ON edges(repo, src_type, src_id, rel, dst_type, dst_id) WHERE rel = 'closes'"
    )

#: v7 FTS objects (issue #24): items_fts gains the paths_text column,
#: reviews_fts is new. Kept as individual statements — NEVER `executescript`
#: (see _FTS_PORTER_STATEMENTS for the implicit-COMMIT rationale). The v3
#: tuple above stays untouched at the v3 shape because migrate_v2_to_v3 runs
#: before v7 on the migration chain.
_V7_FTS_STATEMENTS = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
        title,
        body,
        labels_text,
        paths_text,
        tokenize = 'porter unicode61',
        content='items',
        content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
        INSERT INTO items_fts(rowid, title, body, labels_text, paths_text)
        VALUES (new.id, new.title, new.body, new.labels_text, new.paths_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
        INSERT INTO items_fts(items_fts, rowid, title, body, labels_text, paths_text)
        VALUES ('delete', old.id, old.title, old.body, old.labels_text, old.paths_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
        INSERT INTO items_fts(items_fts, rowid, title, body, labels_text, paths_text)
        VALUES ('delete', old.id, old.title, old.body, old.labels_text, old.paths_text);
        INSERT INTO items_fts(rowid, title, body, labels_text, paths_text)
        VALUES (new.id, new.title, new.body, new.labels_text, new.paths_text);
    END
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS reviews_fts USING fts5(
        body,
        tokenize = 'porter unicode61',
        content='reviews',
        content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS reviews_ai AFTER INSERT ON reviews BEGIN
        INSERT INTO reviews_fts(rowid, body) VALUES (new.id, new.body);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS reviews_ad AFTER DELETE ON reviews BEGIN
        INSERT INTO reviews_fts(reviews_fts, rowid, body)
        VALUES ('delete', old.id, old.body);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS reviews_au AFTER UPDATE ON reviews BEGIN
        INSERT INTO reviews_fts(reviews_fts, rowid, body)
        VALUES ('delete', old.id, old.body);
        INSERT INTO reviews_fts(rowid, body) VALUES (new.id, new.body);
    END
    """,
)

#: (table, column, ALTER suffix) for the v7 payload-signal columns. The
#: derived pair carry NOT NULL DEFAULTs so the guarded ALTER is legal on a
#: populated v6 table; the backfill fills them from stored state.
_V7_ITEM_COLUMNS = (
    ("reactions_total", "INTEGER"),
    ("milestone", "TEXT"),
    ("item_type", "TEXT"),
    ("sub_issues_total", "INTEGER"),
    ("sub_issues_completed", "INTEGER"),
    ("dependencies_blocked_by", "INTEGER"),
    ("dependencies_blocking", "INTEGER"),
    ("dependencies_total_blocked_by", "INTEGER"),
    ("dependencies_total_blocking", "INTEGER"),
    ("author_association", "TEXT"),
    ("merged_by", "TEXT"),
    ("merge_commit", None),
    ("merged_at", None),
    ("comment_count", "INTEGER NOT NULL DEFAULT 0"),
    ("paths_text", "TEXT NOT NULL DEFAULT ''"),
)

#: The payload-derived item columns the v6->v7 backfill binds, in the
#: UPDATE's parameter order — one list drives the SQL and the binding so
#: they cannot drift apart.
_V7_ITEM_COLUMN_ORDER = (
    "reactions_total", "milestone", "item_type", "sub_issues_total",
    "sub_issues_completed", "dependencies_blocked_by", "dependencies_blocking",
    "dependencies_total_blocked_by", "dependencies_total_blocking",
    "author_association", "merged_by", "merge_commit", "merged_at",
)

#: SQLite's signed 64-bit integer domain; JSON carries arbitrary precision.
_SQLITE_INT_MIN = -(2 ** 63)
_SQLITE_INT_MAX = 2 ** 63 - 1


def _v7_scalar(value: object) -> object:
    """A payload-derived value must bind as a SQLite scalar.

    Stored `raw_json` is untrusted input: a wrong-typed field (a list where
    a string belongs) or an integer beyond the signed 64-bit range reaches
    the binder as a Python object SQLite cannot store, which would abort
    the migration chain and pin the store's `user_version` forever (issue
    #53). The honest degradation is NULL: the row keeps its non-payload
    columns and the chain advances. This is a PAYLOAD-validation boundary —
    a database execution fault is not swallowed here (AGENTS.md: a storage
    fault is reported, not swallowed).
    """
    if value is None or isinstance(value, (str, float)):
        return value
    if isinstance(value, int):  # bool is an int subclass and binds as 0/1
        return value if _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX else None
    return None


def _existing_row_v7(conn: sqlite3.Connection, repo: str, number: int, item_id: int):
    """The stored row's ABSENT-mergeable columns as a dict, resolving both
    conflict identities: (repo, number) first, then id (upsert_item_row
    carries two ON CONFLICT arms)."""
    row = conn.execute(
        "SELECT merged_by, merge_commit, merged_at FROM items"
        " WHERE repo = ? AND number = ?",
        (repo, number),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT merged_by, merge_commit, merged_at FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
    return None if row is None else {k: row[k] for k in row.keys()}


def migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    """v6 -> v7: payload signals become queryable columns, review bodies and
    changed paths join search (issue #24).

    Guarded ALTERs (fresh databases already carry the columns via
    schema.sql), new child tables, a Python-`json` backfill from stored
    `raw_json` (no SQLite JSON functions: the supported floor 3.35 predates
    guaranteed JSON1), and full rebuilds of items_fts (4 columns) and
    reviews_fts — creating an external-content FTS never indexes rows the
    store already holds. Every statement is per-statement `execute` (see
    _FTS_PORTER_STATEMENTS for why executescript is forbidden). No-op-safe
    on a fresh database.
    """
    from zaxbygraph.extract import (
        extract_listing_signals,
        extract_pull_owned,
        merge_absent,
    )

    for column, suffix in _V7_ITEM_COLUMNS:
        if column in ("merge_commit", "merged_at"):
            continue  # pre-existing columns; nothing to ALTER
        if not _column_exists(conn, "items", column):
            conn.execute(f"ALTER TABLE items ADD COLUMN {column} {suffix}")
    if not _column_exists(conn, "comments", "author_association"):
        conn.execute("ALTER TABLE comments ADD COLUMN author_association TEXT")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS assignees ("
        "repo TEXT NOT NULL, number INTEGER NOT NULL, login TEXT NOT NULL,"
        " PRIMARY KEY (repo, number, login))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS issue_field_values ("
        "repo TEXT NOT NULL, number INTEGER NOT NULL, field_name TEXT NOT NULL,"
        " value TEXT, PRIMARY KEY (repo, number, field_name))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_assignees_item ON assignees(repo, number)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issue_field_values_item"
        " ON issue_field_values(repo, number)"
    )

    # Backfill items from the stored (merged) payload. Keys absent from an
    # old payload stay NULL (AC2); the pull-owned trio uses the same
    # ABSENT-merge rule as ingest so a stored value never regresses. A
    # payload the store cannot parse or whose fields are wrong-typed
    # (issue #53) degrades THAT ROW's derived columns to NULL — the chain
    # advances and the row keeps its non-payload columns; a storage fault
    # is not swallowed (AGENTS.md).
    for row in conn.execute(
        "SELECT repo, number, id, raw_json FROM items"
    ).fetchall():
        try:
            raw = json.loads(row["raw_json"])
        except (TypeError, ValueError, RecursionError):
            # RecursionError: json.loads on a deeply nested stored payload
            # raises it (a RuntimeError, not a ValueError) — one such row
            # would otherwise re-brick the chain at v6 (issue #53, review
            # CUB-8).
            continue
        if not isinstance(raw, dict):
            continue
        values = merge_absent(extract_listing_signals(raw), {})
        pull_values = merge_absent(
            extract_pull_owned(raw),
            _existing_row_v7(conn, row["repo"], row["number"], row["id"]) or {},
        )
        values.update(pull_values)
        conn.execute(
            "UPDATE items SET reactions_total = ?, milestone = ?, item_type = ?,"
            " sub_issues_total = ?, sub_issues_completed = ?,"
            " dependencies_blocked_by = ?, dependencies_blocking = ?,"
            " dependencies_total_blocked_by = ?, dependencies_total_blocking = ?,"
            " author_association = ?, merged_by = ?, merge_commit = ?,"
            " merged_at = ? WHERE id = ?",
            (
                *(_v7_scalar(values[column]) for column in _V7_ITEM_COLUMN_ORDER),
                row["id"],
            ),
        )
        assignees = raw.get("assignees")
        if isinstance(assignees, list):
            for assignee in assignees:
                login = assignee.get("login") if isinstance(assignee, dict) else assignee
                if login:
                    conn.execute(
                        "INSERT OR IGNORE INTO assignees(repo, number, login)"
                        " VALUES (?, ?, ?)",
                        (row["repo"], row["number"], str(login)),
                    )
        fields = raw.get("issue_field_values")
        if isinstance(fields, list):
            for field in fields:
                if isinstance(field, dict) and field.get("name") is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO issue_field_values"
                        "(repo, number, field_name, value) VALUES (?, ?, ?, ?)",
                        (
                            row["repo"],
                            row["number"],
                            str(field["name"]),
                            None if field.get("value") is None else str(field["value"]),
                        ),
                    )

    # Derived columns and comment provenance (table-derived, not payload).
    for comment_count_row in conn.execute(
        "SELECT repo, number, COUNT(*) AS n FROM comments GROUP BY repo, number"
    ).fetchall():
        conn.execute(
            "UPDATE items SET comment_count = ? WHERE repo = ? AND number = ?",
            (comment_count_row["n"], comment_count_row["repo"], comment_count_row["number"]),
        )
    for comment_row in conn.execute(
        "SELECT pk, raw_json FROM comments WHERE author_association IS NULL"
    ).fetchall():
        try:
            raw = json.loads(comment_row["raw_json"])
        except (TypeError, ValueError, RecursionError):
            continue
        if not isinstance(raw, dict):
            continue
        value = raw.get("author_association")
        if value is not None:
            conn.execute(
                "UPDATE comments SET author_association = ? WHERE pk = ?",
                (_v7_scalar(value), comment_row["pk"]),
            )
    for item_row in conn.execute(
        "SELECT repo, number, kind FROM items WHERE kind = 'pr'"
    ).fetchall():
        paths = [
            r["path"]
            for r in conn.execute(
                "SELECT path FROM pr_files WHERE repo = ? AND number = ?"
                " ORDER BY path",
                (item_row["repo"], item_row["number"]),
            ).fetchall()
        ]
        conn.execute(
            "UPDATE items SET paths_text = ? WHERE repo = ? AND number = ?",
            (" ".join(paths), item_row["repo"], item_row["number"]),
        )

    # Full FTS rebuilds: drop-and-recreate, then the 'rebuild' command
    # repopulates each external-content index from its content table.
    for trigger in (
        "items_ai",
        "items_ad",
        "items_au",
        "reviews_ai",
        "reviews_ad",
        "reviews_au",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE IF EXISTS items_fts")
    conn.execute("DROP TABLE IF EXISTS reviews_fts")
    for statement in _V7_FTS_STATEMENTS:
        conn.execute(statement)
    conn.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO reviews_fts(reviews_fts) VALUES('rebuild')")


def migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    """v7 -> v8: evidence history — pre-images of displaced text (issue #25).

    Additive only: the `revisions` table plus its item index. Fresh databases
    already carry both via schema.sql, so every statement is idempotent
    (per-statement `execute`; executescript is forbidden in migrations — see
    _FTS_PORTER_STATEMENTS). No backfill is possible: prior text was never
    stored, so a migrated store starts with an empty history and `status`
    reports that honestly (coverage nulls on pre-v8, zeros on an empty v8).
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS revisions ("
        " id INTEGER PRIMARY KEY,"
        " repo TEXT NOT NULL,"
        " number INTEGER NOT NULL,"
        " entity TEXT NOT NULL CHECK (entity IN"
        "  ('item_title','item_body','comment','review')),"
        " entity_id INTEGER,"
        " kind TEXT,"
        " origin TEXT NOT NULL CHECK (origin IN ('edit','deleted')),"
        " content TEXT,"
        " valid_from TEXT NOT NULL,"
        " valid_to TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_revisions_item ON revisions(repo, number)"
    )


def _backfill_text_refs(conn: sqlite3.Connection) -> None:
    """Populate v9 surfaces from ALREADY-STORED payloads (issue #26).

    The current text of every item, comment and review is in raw_json, so —
    unlike v8's displaced text — the references are derivable without any
    GitHub call. Backfills: item_path_refs, item_signatures, the new
    duplicate_keyword edges, position-enriched mention/closes_keyword
    evidence (evidence-only upsert; edge identity untouched), and forward
    branch_ref from stored PR head_refs (single numeric token resolving to
    one stored issue). Keyword edges are only ever ADDED here, never
    retracted: the migration cannot see text that pre-dates the stored
    snapshot, and removal is the live ingest's job.
    """
    from zaxbygraph import extract as _extract

    conn.execute(
        "CREATE TABLE IF NOT EXISTS item_path_refs ("
        " repo TEXT NOT NULL, number INTEGER NOT NULL,"
        " origin TEXT NOT NULL, path TEXT NOT NULL,"
        " backticked INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (repo, number, origin, path))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_item_path_refs_path"
        " ON item_path_refs(path)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS item_signatures ("
        " repo TEXT NOT NULL, number INTEGER NOT NULL,"
        " exception TEXT NOT NULL, error_line TEXT NOT NULL,"
        " frame_function TEXT,"
        " PRIMARY KEY (repo, number, exception, error_line))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_repo_head ON items(repo, head_ref)"
        " WHERE kind = 'pr' AND head_ref IS NOT NULL"
    )

    # Branch targets are STORED ISSUES (kind='issue'), the same rule the
    # live forward pass enforces (store.issue_number_lookup) — a PR-numbered
    # token never satisfies it (review round 1).
    stored_numbers: dict[str, set[int]] = {}
    for row in conn.execute(
        "SELECT repo, number FROM items WHERE kind = 'issue'"
    ).fetchall():
        stored_numbers.setdefault(row["repo"], set()).add(row["number"])

    def _upsert_keyword_edge(
        repo: str, number: int, rel: str, dst: int, evidence: str
    ) -> None:
        # The same scoped-merge rule as collapse_edges (plan revision 4):
        # when several stored payloads close the SAME pair (item body AND a
        # comment), the migrated evidence unions instead of last-write-wins
        # dropping a provenance (final critic finding 2). Other relations
        # keep last-write (evidence-only enrichment; title beats body is
        # handled by the callers' mentioned set).
        if rel == "closes_keyword":
            row = conn.execute(
                "SELECT evidence FROM edges WHERE repo = ? AND src_type ="
                " 'item' AND src_id = ? AND rel = 'closes_keyword' AND"
                " dst_type = 'item' AND dst_id = ? AND source = 'keyword'",
                (repo, str(number), str(dst)),
            ).fetchone()
            if row is not None and row["evidence"]:
                evidence = _extract.merge_evidence(row["evidence"], evidence)
        conn.execute(
            "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
            " confidence, evidence, source)"
            " VALUES (?, 'item', ?, ?, 'item', ?, 'EXTRACTED', ?, 'keyword')"
            " ON CONFLICT(repo, src_type, src_id, rel, dst_type, dst_id, source)"
            " DO UPDATE SET evidence = excluded.evidence",
            (repo, str(number), rel, str(dst), evidence),
        )

    for row in conn.execute(
        "SELECT repo, number, raw_json, head_ref FROM items"
    ).fetchall():
        repo, number = row["repo"], row["number"]
        try:
            raw = json.loads(row["raw_json"])
        except (TypeError, ValueError, RecursionError):
            continue
        if not isinstance(raw, dict):
            continue
        # A wrong-typed payload field (`{"title": 5}`) is the #53
        # malformed-data class, but the regexes below take str only, so it
        # used to raise an uncaught TypeError that aborted the whole
        # migration — one candidate killing the entire doctor run, the same
        # observable outcome as a divergent storage class. A non-string
        # value is treated as EMPTY text, never coerced with str(): a list's
        # or dict's repr can contain backticked paths and `closes #N`, and
        # extracting references from it would let a malformed payload write
        # edges (final critic round-7 question; cubic review, PR #58).
        title = raw.get("title")
        body = raw.get("body")
        if not isinstance(title, str):
            title = ""
        if not isinstance(body, str):
            body = ""
        for path, backticked in _extract.parse_path_refs(body, repo):
            conn.execute(
                "INSERT OR IGNORE INTO item_path_refs(repo, number, origin,"
                " path, backticked) VALUES (?, ?, 'body', ?, ?)",
                (repo, number, path, backticked),
            )
        for exception, error_line, frame_function in (
            _extract.parse_error_signatures(body)
        ):
            conn.execute(
                "INSERT OR IGNORE INTO item_signatures(repo, number,"
                " exception, error_line, frame_function) VALUES (?, ?, ?, ?, ?)",
                (repo, number, exception, error_line, frame_function),
            )
        for n, location in _extract.iter_duplicate_locations(title, repo, "title"):
            _upsert_keyword_edge(repo, number, "duplicate_keyword", n, location)
        for n, location in _extract.iter_duplicate_locations(body, repo):
            _upsert_keyword_edge(repo, number, "duplicate_keyword", n, location)
        # Parity with the live edges_from_item (review round 1): a closing
        # keyword reference is NOT also a mention within one payload, and an
        # item never references itself.
        closed = {n for n, _ in _extract.iter_closing_locations(body, repo)}
        mentioned: set[int] = set()
        for n, location in _extract.iter_mention_locations(title, repo, "title"):
            if n != number and n not in closed:
                _upsert_keyword_edge(repo, number, "mentions", n, location)
                mentioned.add(n)
        for n, location in _extract.iter_mention_locations(body, repo):
            # Title beats body (the live setdefault keeps the title's
            # evidence); a body-only mention upserts.
            if n != number and n not in closed and n not in mentioned:
                _upsert_keyword_edge(repo, number, "mentions", n, location)
        for n, location in _extract.iter_closing_locations(body, repo):
            if n != number:
                _upsert_keyword_edge(
                    repo, number, "closes_keyword", n, f"{location} closing keyword"
                )
        head_ref = row["head_ref"]
        if head_ref:
            tokens = _extract.branch_number_tokens(head_ref)
            if len(tokens) == 1:
                token = tokens[0]
                if token in stored_numbers.get(repo, set()) and token != number:
                    _upsert_keyword_edge(
                        repo, number, "branch_ref", token, f"head_ref {head_ref}"
                    )

    for row in conn.execute(
        "SELECT repo, number, kind, github_id, body FROM comments"
    ).fetchall():
        repo, number = row["repo"], row["number"]
        body = row["body"] or ""
        origin = f"{row['kind']}:{row['github_id']}"
        for path, backticked in _extract.parse_path_refs(body, repo):
            conn.execute(
                "INSERT OR IGNORE INTO item_path_refs(repo, number, origin,"
                " path, backticked) VALUES (?, ?, ?, ?, ?)",
                (repo, number, origin, path, backticked),
            )
        for exception, error_line, frame_function in (
            _extract.parse_error_signatures(body)
        ):
            conn.execute(
                "INSERT OR IGNORE INTO item_signatures(repo, number,"
                " exception, error_line, frame_function) VALUES (?, ?, ?, ?, ?)",
                (repo, number, exception, error_line, frame_function),
            )
        # Edge evidence carries the LIVE prefix (final critic finding 1):
        # live edges_from_comment labels every reference comment:<github_id>,
        # never the default body@.
        prefix = f"comment:{row['github_id']}"
        closed = {n for n, _ in _extract.iter_closing_locations(body, repo, prefix)}
        for n, location in _extract.iter_duplicate_locations(body, repo, prefix):
            _upsert_keyword_edge(repo, number, "duplicate_keyword", n, location)
        for n, location in _extract.iter_mention_locations(body, repo, prefix):
            if n != number and n not in closed:
                _upsert_keyword_edge(repo, number, "mentions", n, location)
        for n, location in _extract.iter_closing_locations(body, repo, prefix):
            if n != number:
                _upsert_keyword_edge(
                    repo, number, "closes_keyword", n, f"{location} closing keyword"
                )

    for row in conn.execute(
        "SELECT repo, number, id, body FROM reviews"
    ).fetchall():
        repo, number = row["repo"], row["number"]
        body = row["body"] or ""
        origin = f"review:{row['id']}"
        for path, backticked in _extract.parse_path_refs(body, repo):
            conn.execute(
                "INSERT OR IGNORE INTO item_path_refs(repo, number, origin,"
                " path, backticked) VALUES (?, ?, ?, ?, ?)",
                (repo, number, origin, path, backticked),
            )
        for exception, error_line, frame_function in (
            _extract.parse_error_signatures(body)
        ):
            conn.execute(
                "INSERT OR IGNORE INTO item_signatures(repo, number,"
                " exception, error_line, frame_function) VALUES (?, ?, ?, ?, ?)",
                (repo, number, exception, error_line, frame_function),
            )
        # Review loops carry the same live-path parity: duplicate_keyword
        # (the live edges_from_review emits it; round-1 review), the
        # closing/mentions exclusion, and the LIVE evidence prefix
        # review:<id> (final critic finding 1).
        prefix = f"review:{row['id']}"
        closed = {n for n, _ in _extract.iter_closing_locations(body, repo, prefix)}
        for n, location in _extract.iter_duplicate_locations(body, repo, prefix):
            _upsert_keyword_edge(repo, number, "duplicate_keyword", n, location)
        for n, location in _extract.iter_mention_locations(body, repo, prefix):
            if n != number and n not in closed:
                _upsert_keyword_edge(repo, number, "mentions", n, location)
        for n, location in _extract.iter_closing_locations(body, repo, prefix):
            if n != number:
                _upsert_keyword_edge(
                    repo, number, "closes_keyword", n, f"{location} closing keyword"
                )


def migrate_v8_to_v9(conn: sqlite3.Connection) -> None:
    """v8 -> v9: text-derived references (issue #26).

    Additive tables (item_path_refs, item_signatures), the branch-head partial
    index, and a backfill from already-stored raw_json — the current text IS
    stored, so unlike v8 the references are derivable without GitHub calls.
    Per-statement `execute`; no-op-safe on a fresh database.
    """
    _backfill_text_refs(conn)


def migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
    """v9 -> v10: GitHub-reported links, events and commit messages (#27).

    Additive only: item_events and pr_commits. No backfill is possible —
    reopen events and commit messages were never fetched before this schema
    slot, so a migrated store starts with both tables empty and reads keep
    working through the missing-table degradation arms.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS item_events ("
        " repo TEXT NOT NULL, number INTEGER NOT NULL,"
        " type TEXT NOT NULL, created_at TEXT NOT NULL,"
        " PRIMARY KEY (repo, number, type, created_at))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pr_commits ("
        " repo TEXT NOT NULL, number INTEGER NOT NULL,"
        " sha TEXT NOT NULL, message TEXT,"
        " PRIMARY KEY (repo, number, sha))"
    )


def migrate_v10_to_v11(conn: sqlite3.Connection) -> None:
    """v10 -> v11: fetch-source persistence (issue #48).

    Additive only: one nullable sync_state column, stamped at listing-sync
    start by the sync path. No backfill is possible — the requested source
    was never recorded before this slot, so a migrated store's column is
    NULL, which every reader treats as the documented graphql default.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sync_state)")}
    if "fetch_source" not in cols:
        conn.execute("ALTER TABLE sync_state ADD COLUMN fetch_source TEXT")


#: Forward-only, ordered. Each entry runs in its own transaction that ends by
#: stamping `PRAGMA user_version` (the pragma is transactional).
MIGRATIONS = [
    (2, migrate_v1_to_v2),
    (3, migrate_v2_to_v3),
    (4, migrate_v3_to_v4),
    (5, migrate_v4_to_v5),
    (6, migrate_v5_to_v6),
    (7, migrate_v6_to_v7),
    (8, migrate_v7_to_v8),
    (9, migrate_v8_to_v9),
    (10, migrate_v9_to_v10),
    (11, migrate_v10_to_v11),
]


#: The schema version whose migration CREATES each table that the v1 DDL
#: does not carry (issue #56): a candidate whose `user_version` predates a
#: table its schema DOES carry was left by an interrupted upgrade —
#: `init_schema` commits the current schema DDL and only then runs the
#: migration chain in separate transactions, so a crash between them
#: leaves the newer tables present under the old stamp, never populated.
#: The consolidation's preservation rule keys off this map so such a
#: candidate cannot take the store's rows for a table its version never
#: wrote. Pinned against drift by a census test: every table in the
#: current schema is either in the v1 DDL or named here.
_TABLE_INTRODUCED_VERSION = {
    "assignees": 7,
    "issue_field_values": 7,
    "revisions": 8,
    "item_path_refs": 9,
    "item_signatures": 9,
    "item_events": 10,
    "pr_commits": 10,
}


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
