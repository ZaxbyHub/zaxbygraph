"""Issue #2: discovery and consolidation for scattered legacy DBs.

`doctor` reports every legacy per-checkout DB holding the resolved slug;
`doctor --consolidate` copies the freshest COMPLETE corpus into the
user-level store. Originals are never modified or deleted: inspection and
adoption always work on temp copies (a mode=ro open of a WAL-mode DB would
still materialize -shm/-wal beside the original, so even inspection goes
through a copy).

Report payload (the CLI wraps it in the issue-#3 envelope; these keys live
under `data`, with `ok`/`db`/`repo`/`freshness` on the envelope root):
    {"adopted": <path|null>,
     "revisions_adopted": <int>, "revisions_preserved": <int>,
     "scanned": [{"path", "items", "garbled", "complete", "adopted"}, ...]}

Pinned semantics:
  complete = last_full_sync_at IS NOT NULL and, when the column exists
             (v2 schema), full_sync_pending = 0.
  freshest = greatest sync_state.issues_since among complete candidates.
  garbled  = slug rows whose title/body/labels_text contain mojibake
             signatures (e.g. "Ã" or "â€").
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from zaxbygraph.db import (
    CURRENT_USER_VERSION,
    _quote_ident,
    _table_exists,
    connect,
    current_table_columns,
    fold_repo_casings,
    init_schema,
    open_existing,
)
from zaxbygraph.paths import legacy_db_paths
from zaxbygraph.store import recount

_MOJIBAKE_LIKE = (
    "title LIKE '%Ã%'",
    "title LIKE '%â€%'",
    "IFNULL(body, '') LIKE '%Ã%'",
    "IFNULL(body, '') LIKE '%â€%'",
    "IFNULL(labels_text, '') LIKE '%Ã%'",
    "IFNULL(labels_text, '') LIKE '%â€%'",
)


class _UnreadableCandidate(Exception):
    """The candidate could not be copied/recovered faithfully (issue #52):
    an active writer kept moving it, it is not a database, or its schema
    defeated the fold.

    The reason is carried, not just the path: `doctor` records
    `unreadable_reason` on the winner's scan entry so the report says WHY
    nothing was adopted instead of a bare `adopted: null` (issue #53,
    review PRR-019)."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason

#: tables copied on adoption -> the autoincrement pk column to omit (None =
#: keep every column; GitHub-assigned ids in items/releases/reviews are kept).
#: Issue #25/#40: `revisions` adopt per item through the dedicated per-item
#: block in consolidate() (covered numbers replace with FRESH ids in sequence
#: order; numbers the candidate holds no revision rows for keep their stored
#: history). Consolidation is the one sanctioned rewrite of revision history
#: (append-only everywhere else).
_COPY_TABLES = (
    ("items", None),
    ("labels", None),
    ("assignees", None),
    ("issue_field_values", None),
    ("comments", "pk"),
    ("reviews", None),
    ("pr_files", None),
    ("releases", None),
    ("edges", "id"),
    ("fetch_log", "id"),
    ("revisions", "id"),
    # v9 text-derived references (issue #26) and v10 events/commits
    # (issue #27): composite-keyed item-scoped children, so consolidation
    # must copy them like the other child tables or silently drop history.
    ("item_path_refs", None),
    ("item_signatures", None),
    ("item_events", None),
    ("pr_commits", None),
)


def candidates(cwd: Path | None, extra_scans: list[Path]) -> list[Path]:
    """Repo-local legacy DBs plus every history.db under the --scan dirs."""
    found: list[Path] = []
    for path in [*legacy_db_paths(cwd), *extra_scans]:
        if path.is_dir():
            for hit in sorted(path.rglob("history.db")):
                if hit.is_file() and hit not in found:
                    found.append(hit)
        elif path.is_file() and path not in found:
            found.append(path)
    return found


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _sidecars(path: Path) -> list[Path] | None:
    """The candidate's recovery logs, or None when one is not a log.

    Regular files only (issue #53, review PRR-022): a symlinked or
    non-regular sidecar (FIFO/device) is not a recovery log this copy can
    faithfully take — skipping it would silently truncate the candidate,
    and copying a FIFO would block forever, so such a candidate is reported
    unusable instead. `is_symlink()` is checked BEFORE `exists()` because
    `exists()` follows symlinks and a dangling one would otherwise be
    skipped silently (cubic PR #54 round 1).

    There is deliberately NO size bound. A size ceiling was tried first and
    removed: it refuses a legitimate crash-damaged candidate whose
    un-checkpointed `-wal` exceeds the ceiling — the exact base defect this
    module exists to fix (frozen check C1 caught the multiple-of-main-file
    form doing it; cubic PR #54 round 2 caught the absolute form doing it
    above 1 GiB). A runaway writer is already refused by the stat-stability
    check in `_faithful_copy`, which sees a growing source and refuses after
    bounded retries; a LARGE BUT STABLE log is a real crash-damaged
    candidate, not a runaway one.
    """
    sidecars = []
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        # `is_symlink()` BEFORE `exists()` (cubic PR #54 round 1): `exists()`
        # follows symlinks, so a DANGLING one is silently skipped and the
        # guard below never sees it. `is_symlink()` answers for a dangling
        # link too, so every symlinked sidecar is refused rather than
        # quietly dropped.
        if sidecar.is_symlink():
            return None
        if not sidecar.exists():
            continue
        if not sidecar.is_file():
            return None
        sidecars.append(sidecar)
    return sidecars


def _faithful_copy(path: Path, copy: Path) -> bool:
    """Copy a candidate DB — main file plus any un-checkpointed `-wal` (or
    hot `-journal`) frames — into `copy` WITHOUT opening the original
    (issue #52).

    A WAL-mode candidate whose writer was killed before a clean close keeps
    committed frames in its `-wal`; copying the main file alone (the old
    `shutil.copy2`) drops them, so the candidate is reported empty — or,
    worse, adopted as a truncated image that REPLACES the store's richer
    corpus (replace-the-slug). The recovery log is copied FIRST, then the
    main file: a checkpoint racing the copy then leaves a stale-but-
    consistent pair, never a torn one. The copy is opened read-write and
    checkpointed so recovery happens on the copy alone — the original's
    files are only ever READ (a read-only SQLite open of the original was
    rejected in planning: on this platform it still writes the `-shm`
    beside the original, violating the module's immutability contract).

    The `-shm` is deliberately NOT copied: it is a rebuildable index of the
    `-wal`, and a stale one beside a copied pair is ignored by recovery.

    Returns False when the source changed during the copy (an active
    writer — bounded retries, then refuse), when the copy cannot be
    recovered, or when the source is not a database; the caller keeps
    today's unreadable path.

    The sidecar set is re-enumerated TWICE per attempt (issue #53, review
    PRR-001) — once before the copy and once after, for the stability
    comparison. Re-enumerating only at the top of the attempt is not
    enough: a `-wal` a writer creates DURING the copy would be absent from
    the stale `sources` list, so the after-stat would not see it and a
    main-only image would be accepted as faithful. Re-enumerating for the
    comparison makes the late sidecar mismatch the attempt, and the next
    attempt's fresh top-of-loop enumeration then copies it. The same
    re-enumeration is what lets a sidecar that vanishes mid-copy be
    dropped from the retry instead of re-failing on the same stale path.
    """
    for _ in range(3):
        try:
            sidecars = _sidecars(path)
        except OSError:
            # The sidecar set itself could not be stat'd (the main file was
            # removed while a `-wal` lingers). Not a database fault: retry,
            # then refuse — one unreadable candidate must never abort the
            # scan (PRR-003).
            continue
        if sidecars is None:
            return False
        sources = sidecars + [path]
        try:
            # A previous attempt may have left a sidecar beside the
            # destination: a stale `-wal` would replay over THIS attempt's
            # newer main file on the recovery open and revert committed
            # frames, so the copy would be adopted as a truncated image of
            # the candidate (issue #56, AC1/AC2). The copy path and its
            # sidecars are wholly owned by this call, so every attempt
            # starts from a clean copy directory; a directory that cannot
            # be cleaned takes the same retry-then-refuse path as any other
            # unusable candidate.
            try:
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(str(copy) + suffix).unlink(missing_ok=True)
            except OSError:
                continue
            before = {
                p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                for p in sources
                if p.exists()
            }
            for src in sources:
                shutil.copy2(src, Path(str(copy) + src.name[len(path.name):]))
            after_sidecars = _sidecars(path)
            if after_sidecars is None:
                continue  # a sidecar appeared that is not a log: retry
            after = {
                p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                for p in after_sidecars + [path]
                if p.exists()
            }
        except OSError:
            # A source the OS refuses (a locked or permission-denied file) or
            # a sidecar a finishing writer removed between the enumeration and
            # the copy: not a database fault, an unusable candidate. Retry,
            # then refuse — one unreadable candidate must never abort the
            # scan (PRR-003).
            continue
        if before != after:
            continue  # an active writer moved the source: retry, then refuse
        try:
            conn = sqlite3.connect(str(copy))
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            finally:
                conn.close()
            return True
        except sqlite3.DatabaseError:
            continue
    return False


def inspect(path: Path, repo: str) -> dict | None:
    """Per-DB stats for one slug, read from a temp copy. None = unreadable."""
    repo_l = repo.lower()
    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / path.name
        if not _faithful_copy(path, copy):
            return None
        try:
            conn = open_existing(copy)
        except (FileNotFoundError, sqlite3.DatabaseError):
            return None
        try:
            try:
                # EVERY query below is inside this guard (PRR-003): a
                # partial/foreign sqlite file that happens to be named
                # history.db must be reported as unusable (None), never
                # crash the whole doctor command.
                if not _has_column(conn, "sync_state", "repo"):
                    return None
                pend = _has_column(conn, "sync_state", "full_sync_pending")
                complete_expr = "last_full_sync_at IS NOT NULL"
                if pend:
                    complete_expr += " AND full_sync_pending = 0"
                # Deterministic winner rule (issue #52): a case-split
                # candidate's sync_state rows are read through the same
                # freshest-first ordering the fold's merge uses, so
                # completeness/watermark never depend on scan order.
                # COALESCE matches _fold_sync_state's (issues_since or "")
                # ranking, where NULL and empty string tie.
                state = conn.execute(
                    "SELECT issues_since FROM sync_state WHERE lower(repo) = ?"
                    " ORDER BY COALESCE(issues_since, '') DESC, rowid DESC LIMIT 1",
                    (repo_l,),
                ).fetchone()
                items = conn.execute(
                    "SELECT COUNT(*) AS c FROM items WHERE lower(repo) = ?", (repo_l,)
                ).fetchone()["c"]
                garbled = conn.execute(
                    "SELECT COUNT(*) AS c FROM items WHERE lower(repo) = ? "
                    "AND (" + " OR ".join(_MOJIBAKE_LIKE) + ")",
                    (repo_l,),
                ).fetchone()["c"]
                complete = False
                if state is not None:
                    row = conn.execute(
                        f"SELECT ({complete_expr}) AS c FROM sync_state"
                        " WHERE lower(repo) = ?"
                        " ORDER BY COALESCE(issues_since, '') DESC, rowid DESC LIMIT 1",
                        (repo_l,),
                    ).fetchone()
                    complete = bool(row["c"])
            except sqlite3.DatabaseError:
                return None
            return {
                "path": str(path),
                "items": int(items),
                "garbled": int(garbled),
                "complete": complete,
                "issues_since": None if state is None else state["issues_since"],
            }
        finally:
            conn.close()


#: `sync_state` columns the fold and the adoption read (issue #56, AC4/AC5).
_REQUIRED_SYNC_STATE_COLUMNS = (
    "repo",
    "issues_since",
    "last_full_sync_at",
    "last_incr_sync_at",
    "last_error",
    "include_patches",
    "full_sync_pending",
)
#: NOT NULL flag columns: any non-integer storage class is a fabricated
#: state no genuine store can produce (the DDL declares them INTEGER NOT
#: NULL DEFAULT 0), so it is refused rather than adopted as a value (issue
#: #56, AC11).
_REQUIRED_SYNC_STATE_FLAGS = ("include_patches", "full_sync_pending")
#: TEXT columns the fold and the adoption compare or carry (issue #56):
#: the DDL declares them TEXT, so a divergent storage class (INTEGER, BLOB)
#: would raise an uncaught TypeError at the fold's max() or at the scan's
#: winner ranking — the same isolation escape as the flags, one type up.
_REQUIRED_SYNC_STATE_TEXTS = ("issues_since", "last_full_sync_at",
                              "last_incr_sync_at", "last_error")
#: columns the wholesale `actors` copy names (issue #56, AC9/AC10).
_REQUIRED_ACTOR_COLUMNS = ("login", "html_url")


def _require_adoptable_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Refuse a candidate whose schema cannot be adopted faithfully, BEFORE
    any store write (issue #56). Every read `consolidate` performs on the
    candidate is validated here — the `_COPY_TABLES` delete/copy loops, the
    per-item revisions read, the `sync_state` reads, and the wholesale
    `actors` copy — so a divergent schema is isolated as an unreadable
    candidate (recorded on the scan entry, the scan continues) instead of
    aborting the whole command: `sqlite3.Row` raises IndexError on a missing
    key and a missing table raises OperationalError, neither of which the
    sqlite3.DatabaseError handlers convert.

    Runs after `_MigratedCopy`'s optional migration, so an ordinary older
    candidate is repaired to the current shape first; only a genuinely
    divergent candidate refuses. Raises `_UnreadableCandidate` with the
    reason carried.
    """
    for table, _autoinc_pk in _COPY_TABLES:
        if not _table_exists(conn, table):
            raise _UnreadableCandidate(
                path, f"the candidate's schema lacks the {table} table")
    if not _table_exists(conn, "actors"):
        raise _UnreadableCandidate(
            path, "the candidate's schema lacks the actors table")
    # Column parity for every copied table (issue #56): the copy loops read
    # the candidate's columns into the store's, so a candidate lacking a
    # current column would be adopted with the store's defaults for it — a
    # truncated image (a candidate whose comments table lacked
    # author_association was probe-verified to be adopted with NULLs). A
    # candidate carrying EXTRA columns fails the copy's INSERT and is
    # already converted to a refusal.
    expected = current_table_columns()
    for table in [t for t, _pk in _COPY_TABLES] + ["actors"]:
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = expected.get(table, set()) - have
        if missing:
            raise _UnreadableCandidate(
                path,
                f"the candidate's {table} table lacks the"
                f" {', '.join(sorted(missing))} column(s)")
    actor_cols = {row[1] for row in conn.execute("PRAGMA table_info(actors)")}
    for column in _REQUIRED_ACTOR_COLUMNS:
        if column not in actor_cols:
            raise _UnreadableCandidate(
                path,
                f"the candidate's actors table lacks the {column} column")
    state_cols = {row[1] for row in conn.execute("PRAGMA table_info(sync_state)")}
    for column in _REQUIRED_SYNC_STATE_COLUMNS:
        if column not in state_cols:
            raise _UnreadableCandidate(
                path, f"the candidate's sync_state lacks the {column} column")
    # repo is the fold's grouping key: a NULL or non-text value (SQLite
    # permits NULL in a TEXT PRIMARY KEY on a rowid table) would raise an
    # uncaught AttributeError at .lower() on the consolidation path AND
    # through the migration fold (issue #56; implementation-review
    # round-3 finding 2).
    bad_repo = conn.execute(
        "SELECT COUNT(*) FROM sync_state WHERE typeof(repo) <> 'text'"
    ).fetchone()[0]
    if bad_repo:
        raise _UnreadableCandidate(
            path,
            f"the candidate's sync_state.repo is NULL or non-text in"
            f" {bad_repo} row(s): a divergent key value is refused")
    for column in _REQUIRED_SYNC_STATE_FLAGS:
        # The DDL declares these INTEGER NOT NULL DEFAULT 0, so anything
        # else — NULL, TEXT, REAL, BLOB — is a fabricated state: a NULL or
        # a heterogeneous type would abort the fold's aggregation with an
        # uncaught TypeError, and a single-cased TEXT value would be
        # written into the store's INTEGER column as-is (issue #56,
        # AC11; implementation-review finding 1). typeof() names the
        # storage class, so one check covers every non-integer shape.
        bad = conn.execute(
            f"SELECT COUNT(*) FROM sync_state"
            f" WHERE typeof({_quote_ident(column)}) <> 'integer'"
        ).fetchone()[0]
        if bad:
            raise _UnreadableCandidate(
                path,
                f"the candidate's sync_state.{column} holds a non-integer"
                f" value in {bad} row(s): a fabricated flag state is"
                " refused, not adopted")
    for column in _REQUIRED_SYNC_STATE_TEXTS:
        # The DDL declares these TEXT; a divergent storage class would
        # raise an uncaught TypeError at the fold's max() or at the scan's
        # winner ranking (issue #56; implementation-review round-2
        # finding 1). Refuse the candidate instead of aborting the command.
        bad = conn.execute(
            f"SELECT COUNT(*) FROM sync_state"
            f" WHERE typeof({_quote_ident(column)}) NOT IN ('text', 'null')"
        ).fetchone()[0]
        if bad:
            raise _UnreadableCandidate(
                path,
                f"the candidate's sync_state.{column} holds a non-text"
                f" value in {bad} row(s): a divergent storage class is"
                " refused, not adopted")


class _MigratedCopy:
    """Temp copy of a legacy DB, migrated to the current schema if needed
    and then case-folded for EVERY candidate version (issue #52).

    `connect`+`init_schema` runs the MIGRATIONS framework on the copy (case
    folds, adds full_sync_pending); the original is never opened for write.
    A candidate already at the current version never migrates, so without
    the explicit fold its dual-cased rows would still collide the adoption
    copy — `fold_repo_casings` runs on every copy (idempotent: a pre-v2
    candidate already folded inside migrate_v1_to_v2 is folded again as a
    no-op). The temp directory is removed on close (PRR-006: every
    --consolidate used to leak a full DB copy into the system temp dir).
    """

    def __init__(self, path: Path) -> None:
        self._td = tempfile.mkdtemp(prefix="zaxbygraph-doctor-")
        try:
            self.path = Path(self._td) / path.name
            if not _faithful_copy(path, self.path):
                raise _UnreadableCandidate(path, "the candidate could not be copied faithfully")
            version = None
            try:
                probe = sqlite3.connect(str(self.path))
                try:
                    version = int(probe.execute("PRAGMA user_version").fetchone()[0])
                finally:
                    probe.close()
            except sqlite3.DatabaseError as exc:
                raise _UnreadableCandidate(
                    path, "the candidate is not a readable database"
                ) from exc
            self.migrated = version < CURRENT_USER_VERSION
            conn = connect(self.path)
            try:
                if self.migrated:
                    init_schema(conn)
                # One structural preflight for everything the adoption reads
                # from the candidate (issue #56): an ordinary older
                # candidate was just migrated to the current shape, so this
                # refuses only genuinely divergent schemas — before any
                # store write and before the fold sees them.
                _require_adoptable_schema(conn, path)
                fold_repo_casings(conn)
                conn.commit()
            except sqlite3.DatabaseError as exc:
                # A candidate whose schema diverges from the tool's can still
                # defeat the fold (e.g. a missing unique index behind an ON
                # CONFLICT target). The fold runs on the temp copy only, so
                # the candidate is reported unusable — one divergent
                # candidate must never abort the whole scan (issue #53,
                # review PRR-005; base adopted such candidates).
                raise _UnreadableCandidate(
                    path, f"the candidate's schema defeated the fold: {exc}"
                ) from exc
            finally:
                conn.close()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        shutil.rmtree(self._td, ignore_errors=True)


@contextmanager
def _migrated_copy(path: Path):
    """Context manager yielding the migrated temp copy path."""
    copy = _MigratedCopy(path)
    try:
        yield copy.path
    finally:
        copy.close()


def _copy_table(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    autoinc_pk: str | None,
    repo_l: str,
) -> None:
    """Copy one repo-keyed table from the candidate into the store.

    Column names come from the CANDIDATE's own `PRAGMA table_info` output,
    so they are untrusted text until quoted: a divergent name that is a
    reserved word (`order`, `index`) would otherwise parse as SQL and abort
    the whole command (issue #53, review PRR-005 — the same defect the
    fold's quoting closes, in the copy loop the fold runs after). A
    divergence the quoting cannot absorb (an extra column the store lacks)
    raises sqlite3.DatabaseError, which the caller converts to an unusable
    candidate rather than a crash.
    """
    cols = [row[1] for row in src.execute(f"PRAGMA table_info({table})")]
    if autoinc_pk is not None:
        cols = [c for c in cols if c != autoinc_pk]
    if not cols:
        return
    quoted = [_quote_ident(c) for c in cols]
    if "repo" in cols:
        select_cols = ", ".join(
            "lower(repo)" if c == "repo" else q for c, q in zip(cols, quoted)
        )
    else:
        select_cols = ", ".join(quoted)
    placeholders = ", ".join("?" for _ in cols)
    # Ordered by the omitted pk (issue #25): destination ids are fresh, so
    # insertion order is the only place the source sequence survives —
    # deterministic for the omit-pk tables.
    order_by = f" ORDER BY {_quote_ident(autoinc_pk)}" if autoinc_pk else ""
    rows = src.execute(
        f"SELECT {select_cols} FROM {_quote_ident(table)}"
        + (" WHERE lower(repo) = ?" if "repo" in cols else "")
        + order_by,
        (repo_l,) if "repo" in cols else (),
    ).fetchall()
    insert_sql = (
        f"INSERT INTO {_quote_ident(table)} ({', '.join(quoted)}) "
        f"VALUES ({placeholders})"
    )
    dst.executemany(insert_sql, [tuple(r) for r in rows])


def consolidate(store_db: Path, path: Path, repo: str) -> tuple[int, int]:
    """Adopt one candidate into the store: REPLACE-THE-SLUG, revisions scoped per item.

    One transaction: delete every store row whose repo is the folded slug,
    then insert the candidate's rows with repo lowered - omitting
    synthetic-pk columns (comments.pk with its AUTOINCREMENT, edges.id,
    fetch_log.id, revisions.id) so they re-assign, keeping GitHub-assigned
    ids (items.id, releases.id, reviews.id). sync_state is replaced and counts recomputed. Plain
    INSERTs only: post-delete there is nothing to conflict with, and
    INSERT OR REPLACE's double-unique semantics could silently drop rows.

    Snapshot provenance (issue #34) is dropped by consolidation on
    purpose: the merged corpus is not the pulled snapshot, so
    `snapshot.*` meta keys are deleted from the target and `status`
    reports no snapshot provenance afterward. (Disclosure per plan v4,
    critic round 1 finding 6.)

    Revisions (issue #40) are the exception to replace-the-slug: the
    candidate's `revisions` table is an OBSERVATION, not the truth —
    a pre-v8 candidate migrated in by _MigratedCopy carries an empty,
    backfill-impossible table, so deleting the store's history for items
    the candidate never observed destroys evidence that cannot be
    re-derived (edit history is unobservable after the fact; ingest is
    append-only and this loop is the only revisions DELETE in the tree).
    The delete is therefore scoped per item: covered items (the candidate
    has rows for them) whose candidate rows are payload-identical to the
    stored rows are left untouched (a same-candidate re-adoption writes
    no rows and deletes none); covered items with different rows are
    replaced (within-item depth loss for a shallower candidate remains a
    recorded boundary — the candidate's rows are a real observation for
    that item); uncovered numbers' history is preserved. Returns
    (adopted_rows, preserved_rows): adopted = rows copied from the
    candidate; preserved = stored rows for numbers the candidate holds no
    revision rows for (a payload-identical covered number is a no-op —
    its rows are neither adopted nor preserved).
    """
    repo_l = repo.lower()
    with _migrated_copy(path) as source:
        src = open_existing(source)
        dst = connect(store_db)
        init_schema(dst)  # the store may not exist yet; doctor may create it
        try:
            dst.execute("BEGIN IMMEDIATE")
            try:
                for table, autoinc_pk in _COPY_TABLES:
                    if table == "revisions":
                        continue  # per-item scoping below (issue #40)
                    dst.execute(
                        f"DELETE FROM {_quote_ident(table)} WHERE repo = ?",
                        (repo_l,)
                    )
                dst.execute("DELETE FROM sync_state WHERE repo = ?", (repo_l,))

                # Snapshot provenance (issue #34) drops on consolidation:
                # the merged corpus is not the pulled snapshot, and the
                # meta table is global (not repo-scoped like the tables
                # above), so the keys are removed directly.
                dst.execute("DELETE FROM meta WHERE key LIKE 'snapshot.%'")

                # Issue #40: revisions adopt per item. A covered number
                # (the candidate has revision rows for it) whose candidate
                # rows are payload-identical to the stored rows is skipped
                # entirely (AC6: a same-candidate re-adoption is a no-op); a
                # covered number with different rows is replaced; numbers
                # with no candidate rows are never touched (AC1/AC2). One
                # indexed pass over the candidate's rows, grouped per number
                # here — the per-item lower(repo) query shape full-scans the
                # table (review PRR-006). repo = ? is exact: revisions rows
                # are only written with the folded slug (ingest via
                # validate_slug, consolidation via lower()).
                rev_payload_cols = (
                    "entity", "entity_id", "kind", "origin", "content",
                    "valid_from", "valid_to",
                )
                covered_rows: dict[int, list] = {}
                try:
                    for row in src.execute(
                        "SELECT number, " + ", ".join(rev_payload_cols)
                        + " FROM revisions WHERE repo = ? ORDER BY number, id",
                        (repo_l,),
                    ):
                        covered_rows.setdefault(int(row[0]), []).append(
                            tuple(row[1:])
                        )
                except (ValueError, TypeError) as exc:
                    # A TEXT (or NULL) revisions.number is a fabricated
                    # value the per-item read cannot take (SQLite's dynamic
                    # typing stores it despite the INTEGER declaration), and
                    # int() raises ValueError/TypeError — neither is a
                    # sqlite3.DatabaseError, so it would abort the whole
                    # command (issue #56; implementation-review round-3
                    # finding 3). Isolate the candidate instead.
                    raise _UnreadableCandidate(
                        path,
                        f"the candidate's revisions.number is not an integer:"
                        f" {exc!r}"
                    ) from exc
                store_revs_before = dst.execute(
                    "SELECT COUNT(*) FROM revisions WHERE repo = ?", (repo_l,)
                ).fetchone()[0]
                adopted_rows = 0
                covered_stored = 0
                for number in sorted(covered_rows):
                    cand_rows = covered_rows[number]
                    stored_rows = dst.execute(
                        "SELECT " + ", ".join(rev_payload_cols)
                        + " FROM revisions WHERE repo = ? AND number = ?"
                        " ORDER BY id",
                        (repo_l, number),
                    ).fetchall()
                    covered_stored += len(stored_rows)
                    if cand_rows == [tuple(r) for r in stored_rows]:
                        continue  # same observation: no-op (AC6); its rows
                        # are neither adopted nor preserved
                    dst.execute(
                        "DELETE FROM revisions WHERE repo = ? AND number = ?",
                        (repo_l, number),
                    )
                    insert_sql = (
                        f"INSERT INTO revisions ({', '.join(rev_payload_cols)}, repo, number) "
                        f"VALUES ({', '.join('?' for _ in rev_payload_cols)}, ?, ?)"
                    )
                    dst.executemany(
                        insert_sql,
                        [tuple(r) + (repo_l, number) for r in cand_rows],
                    )
                    adopted_rows += len(cand_rows)
                # Preserved = stored rows for numbers the candidate holds no
                # revision rows for: store total minus EVERY covered number's
                # stored rows, counted BEFORE the skip decision (so a
                # payload-identical skip is neither adopted nor preserved).
                # Arithmetic replaces the unbounded NOT IN placeholder list,
                # which hit SQLite's host-parameter ceiling (review PRR-005).
                preserved_rows = store_revs_before - covered_stored

                for table, autoinc_pk in _COPY_TABLES:
                    if table == "revisions":
                        continue  # adopted per item above (issue #40)
                    try:
                        _copy_table(src, dst, table, autoinc_pk, repo_l)
                    except sqlite3.DatabaseError as exc:
                        # A candidate whose schema diverges from the tool's
                        # can defeat the copy as well as the fold (an extra
                        # column, a missing one, a missing unique index
                        # behind an ON CONFLICT). The whole adoption is one
                        # transaction, so nothing has been written; report
                        # the candidate unusable rather than aborting the
                        # command (issue #53, review PRR-005 — base adopted
                        # such candidates for some tables).
                        raise _UnreadableCandidate(
                            path,
                            f"the candidate's schema defeated the copy: {exc}",
                        ) from exc

                # The copy is case-folded before this point, so the folded
                # slug has exactly one sync_state row; the ordering keeps
                # the read deterministic even if that ever changes.
                state = src.execute(
                    "SELECT * FROM sync_state WHERE lower(repo) = ?"
                    " ORDER BY COALESCE(issues_since, '') DESC, rowid DESC LIMIT 1",
                    (repo_l,),
                ).fetchone()
                if state is not None:
                    dst.execute(
                        "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
                        " last_incr_sync_at, last_error, include_patches,"
                        " full_sync_pending) VALUES (?,?,?,?,?,?,?)",
                        (
                            repo_l,
                            state["issues_since"],
                            state["last_full_sync_at"],
                            state["last_incr_sync_at"],
                            state["last_error"],
                            state["include_patches"],
                            state["full_sync_pending"],
                        ),
                    )
                    # The candidate may predate schema v4: copy the rate-limit
                    # columns only when the source actually carries them, so
                    # consolidation never silently nulls newer state. fetch_source
                    # (schema v11, issue #48) carries the same way: a pre-v11
                    # candidate leaves the column NULL (unset → the documented
                    # graphql default), a post-v11 candidate's recorded profile
                    # adopts with the row.
                    src_cols = {
                        row[1] for row in src.execute("PRAGMA table_info(sync_state)")
                    }
                    rate_cols = ("rate_limit_remaining", "rate_limit_reset_at")
                    if set(rate_cols) <= src_cols:
                        dst.execute(
                            "UPDATE sync_state SET rate_limit_remaining = ?,"
                            " rate_limit_reset_at = ? WHERE repo = ?",
                            (
                                state["rate_limit_remaining"],
                                state["rate_limit_reset_at"],
                                repo_l,
                            ),
                        )
                    if "fetch_source" in src_cols:
                        dst.execute(
                            "UPDATE sync_state SET fetch_source = ? WHERE repo = ?",
                            (state["fetch_source"], repo_l),
                        )
                # actors is a global login->url table (no repo column), so it
                # is copied wholesale despite the slug-scoped delete above.
                for actor, url in src.execute("SELECT login, html_url FROM actors"):
                    dst.execute(
                        "INSERT INTO actors(login, html_url) VALUES (?, ?) "
                        "ON CONFLICT(login) DO UPDATE SET html_url = excluded.html_url",
                        (actor, url),
                    )
                recount(dst, repo_l)
                dst.commit()
            except BaseException:
                dst.rollback()
                raise
        finally:
            dst.close()
            src.close()
    return (adopted_rows, preserved_rows)


def doctor(
    cwd: Path | None,
    repo: str,
    store_db: Path,
    extra_scans: list[Path] | None = None,
    consolidate_flag: bool = False,
) -> dict:
    """Report (and optionally consolidate) legacy DBs for one slug."""
    repo_l = repo.lower()
    scanned: list[dict] = []
    adopted: str | None = None
    best: tuple[str, dict] | None = None  # (issues_since, entry)
    for path in candidates(cwd, extra_scans or []):
        entry = inspect(path, repo_l)
        if entry is None:
            # An uninspectable candidate is REPORTED, not silently dropped:
            # "no candidates exist" and "one unreadable candidate" must
            # never look the same in the report (issue #56, AC7). `inspect`'s
            # None contract is unchanged (its direct callers pin it); the
            # entry never competes for adoption (no items, not complete).
            scanned.append({
                "path": str(path),
                "items": 0,
                "garbled": 0,
                "complete": False,
                "issues_since": None,
                "adopted": False,
                "unreadable": True,
                "unreadable_reason": "the candidate could not be read as a"
                                     " database for this repo",
            })
            continue
        entry["adopted"] = False
        scanned.append(entry)
        if entry["complete"] and entry["items"] > 0:
            # A candidate whose sync_state.issues_since lost its TEXT
            # affinity can hold an INTEGER (or other) value; comparing
            # that against a timestamp string raises an uncaught TypeError
            # that would abort the whole scan (issue #56;
            # implementation-review round-2 finding 1). Rank a non-text
            # watermark as the oldest — the candidate is still reported,
            # and the adoption's preflight refuses the divergent storage
            # class before any store write.
            raw_key = entry["issues_since"]
            key = raw_key if isinstance(raw_key, str) else ""
            if best is None or key > best[0]:
                best = (key, entry)
    if consolidate_flag and best is not None:
        winner = best[1]
        try:
            rev_adopted, rev_preserved = consolidate(
                store_db, Path(winner["path"]), repo_l
            )
        except _UnreadableCandidate as unreadable:
            # The winner passed inspection but cannot be adopted (its schema
            # defeated the fold). Report the scan without adopting — one
            # unusable candidate must never abort the whole command (PRR-003;
            # issue #53, review PRR-005). The store is untouched: the fold
            # runs on the temp copy before any store write. The REASON is
            # recorded on the winner's scan entry so the report says WHY
            # nothing was adopted instead of a bare `adopted: null`
            # (issue #53, review PRR-019).
            winner["adopted"] = False
            winner["unreadable"] = True
            winner["unreadable_reason"] = str(unreadable)
            rev_adopted, rev_preserved = 0, 0
        else:
            winner["adopted"] = True
            adopted = winner["path"]
    else:
        rev_adopted = 0
        rev_preserved = 0
    return {
        "ok": True,
        "adopted": adopted,
        "revisions_adopted": rev_adopted,
        "revisions_preserved": rev_preserved,
        "scanned": scanned,
    }
