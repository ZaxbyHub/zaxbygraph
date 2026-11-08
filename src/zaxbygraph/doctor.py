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
     "tables_preserved": [<table>, ...],
     "scanned": [{"path", "items", "garbled", "complete", "issues_since",
                  "adopted", "unreadable", "unreadable_reason"}, ...]}

`tables_preserved` names the repo-keyed tables the adopted candidate's
VERSION never had (issue #56): the migration recreates them empty, so the
store's rows for them are preserved rather than deleted-and-not-recopied,
and the report says which instead of implying a clean replace.

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
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from zaxbygraph.db import (
    CURRENT_USER_VERSION,
    _TABLE_INTRODUCED_VERSION,
    _quote_ident,
    _table_exists,
    connect,
    current_column_rules,
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


#: Every table whose rows the adoption reads or writes (issue #56): the
#: copied tables plus the wholesale `actors` copy and the named
#: `sync_state` insert. The schema-wide storage-class contract covers
#: exactly these — a table the adoption never touches has no contract to
#: violate.
_ADOPTED_TABLES = frozenset(
    [t for t, _pk in _COPY_TABLES] + ["actors", "sync_state"]
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
    unusable instead.

    There is deliberately NO size bound. A size ceiling was tried first and
    removed: it refuses a legitimate crash-damaged candidate whose
    un-checkpointed `-wal` exceeds the ceiling — the exact base defect this
    module exists to fix (frozen check C1 caught the multiple-of-main-file
    form doing it; cubic PR #54 round 2 caught the absolute form doing it
    above 1 GiB). A runaway writer is already refused by the stat-stability
    check in `_faithful_copy`, which sees a growing source and refuses after
    bounded retries; a LARGE BUT STABLE log is a real crash-damaged
    candidate, not a runaway one.

    `stat` is called FIRST and only `FileNotFoundError` means "absent",
    because `is_symlink()`/`exists()`/`is_file()` are not stable oracles
    (issue #59). On 3.11-3.13 those three are pathlib's own implementation,
    which re-raises only an OSError outside `_ignore_error`'s swallow set
    (`ENOENT`, `ENOTDIR`, `EBADF`, `ELOOP`/`WSAELOOP`, plus Windows winerror
    21/123/1921) and answers `False` for the rest; on 3.14 they delegate to
    `os.path.islink`/`os.path.exists`/`os.path.isfile`, which swallow every
    `OSError`. So the old shape classified an uninspectable sidecar as absent
    on 3.14 for every fault class, and on 3.11-3.13 for the swallowed classes
    that are not a genuine absence (`ENOTDIR`, `EBADF`, `ELOOP` — `ENOENT` is
    correctly "absent" and by design indistinguishable from it). The 3.11-3.13
    row is measured per-errno on this box; the 3.14 row is read from CPython's
    `pathlib`/`genericpath` source, because 3.14's `os.path.*` are C builtins
    that bypass a patched `os.stat`. Either way an exists()-first shape drops
    an uninspectable sidecar from the copy and lets `_faithful_copy` adopt a
    truncated image — its own documented worst case. `stat` is called first
    and only `FileNotFoundError` means absent, so every other fault class
    refuses on every supported interpreter.
    Any other OSError makes the candidate unusable, exactly as
    `snapshot.py`'s `_post_install_check` already treats a destination
    sidecar it cannot inspect.
    """
    sidecars = []
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        # `follow_symlinks=False` is `lstat`: it answers for the link
        # itself, so a symlinked sidecar — including a dangling one — is
        # refused rather than quietly dropped, and the classification
        # never follows the link to its target.
        try:
            st = sidecar.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return None
        if not stat.S_ISREG(st.st_mode):
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
            # Defence in depth: `_sidecars` now fails closed by returning
            # None, so this guard covers a future enumeration that raises
            # rather than today's. One unreadable candidate must never
            # abort the scan (PRR-003).
            sidecars = _sidecars(path)
        except OSError:
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
            # No `if p.exists()` guard (issue #59): the guard swallowed a
            # stat fault into a missing dict entry on 3.14, so a source that
            # could not be stat'd dropped out of the comparison instead of
            # failing the attempt. An OSError here now reaches the
            # retry-then-refuse path below, which is where an unusable
            # candidate belongs.
            before = {
                p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                for p in sources
            }
            for src in sources:
                shutil.copy2(src, Path(str(copy) + src.name[len(path.name):]))
            after_sidecars = _sidecars(path)
            if after_sidecars is None:
                continue  # a sidecar appeared that is not a log: retry
            # Same stat-first shape as `before` above (issue #59).
            after = {
                p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                for p in after_sidecars + [path]
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
#: The storage-class checks the migration-time fold needs BEFORE
#: init_schema runs (issue #56): (table, column, allowed typeof values).
#: The fold's closes-group merge reads edges.evidence as text, the per-item
#: revisions read calls int() on revisions.number, and the grouping and
#: aggregations read sync_state's key and TEXT columns — all reachable
#: from a pre-migration schema, so each check is gated on the column
#: existing. A NULL evidence is legal (no provenance recorded); a NULL
#: number is not (the DDL declares it NOT NULL).
_RAW_STORAGE_CLASS_CHECKS = (
    ("sync_state", "repo", ("text",)),
    ("sync_state", "issues_since", ("text", "null")),
    ("sync_state", "last_full_sync_at", ("text", "null")),
    ("sync_state", "last_incr_sync_at", ("text", "null")),
    ("sync_state", "last_error", ("text", "null")),
    ("sync_state", "include_patches", ("integer",)),
    ("revisions", "number", ("integer",)),
    ("edges", "evidence", ("text", "null")),
    # The migration's v8->v9 text backfill reads these three in Python
    # (db.py _backfill_text_refs: parse_path_refs / branch_number_tokens),
    # and the v6->v7 rebuild joins `pr_files.path` into `items.paths_text`
    # (db.py migrate_v6_to_v7), so a BLOB in any of them aborts the whole
    # command with an uncaught TypeError that no sqlite3.DatabaseError
    # handler converts — the round-6 reviewer's finding 2, the same class
    # as edges.evidence one column family over, and the final critic's
    # finding 1 (a v6 candidate with a BLOB pr_files.path killed the whole
    # doctor run, healthy sibling included, while its current-version twin
    # was refused cleanly). `path` is declared NOT NULL, so a NULL is a
    # fabricated state too.
    ("items", "head_ref", ("text", "null")),
    ("comments", "body", ("text", "null")),
    ("reviews", "body", ("text", "null")),
    ("pr_files", "path", ("text",)),
)


def _require_adoptable_schema(conn: sqlite3.Connection, path: Path,
                           repo: str) -> None:
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
    # candidate carrying EXTRA columns in a copied table fails the copy's
    # INSERT and is already converted to a refusal; extras in the
    # non-copied tables (sync_state, actors, revisions) are named
    # column-by-column and silently ignored — a same-version extra column
    # is recorded as a considered-not-filed boundary in the PR body.
    expected = current_table_columns()
    for table in [t for t, _pk in _COPY_TABLES] + ["actors"]:
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = expected.get(table, set()) - have
        if missing:
            raise _UnreadableCandidate(
                path,
                f"the candidate's {table} table lacks the"
                f" {', '.join(sorted(missing))} column(s)")
    # ONE schema-derived rule for every column the adoption writes into the
    # store (issue #56): the DDL declares each column's type and
    # NULLability, so a divergent storage class is refused — TEXT holding a
    # BLOB or INTEGER, INTEGER holding text (items.number,
    # sync_state.rate_limit_remaining), and a NULL in a NOT NULL column
    # (revisions.valid_from, items.title), which the store's own DDL would
    # reject at the copy with an uncaught IntegrityError that aborts the
    # whole command (swarm-pr-review N-01/N-18). One derived rule replaces
    # the enumerated per-column checks that preceded it, three of which it
    # had made unreachable (N-14).
    # The required-column presence check runs BEFORE the derived rule: the
    # rule reads `sync_state`'s columns through PRAGMA-derived names, so a
    # candidate missing one of them would otherwise be refused by the rule's
    # generic "divergent storage class" message instead of by the specific
    # "sync_state lacks the X column" one (cubic review, PR #58). Presence
    # is the cheaper and more diagnosable fact, so it is asked first.
    state_cols = {row[1] for row in conn.execute("PRAGMA table_info(sync_state)")}
    for column in _REQUIRED_SYNC_STATE_COLUMNS:
        if column not in state_cols:
            raise _UnreadableCandidate(
                path, f"the candidate's sync_state lacks the {column} column")
    rules = current_column_rules()
    for table in sorted(rules):
        if table not in _ADOPTED_TABLES:
            continue
        for column, (declared, not_null) in sorted(rules[table].items()):
            if declared == "TEXT":
                # A NOT NULL TEXT column refuses NULL; a nullable one
                # admits it. `sync_state.repo` is a TEXT PRIMARY KEY whose
                # own DDL omits NOT NULL, and it needs no special case
                # here: the scope is `lower(repo) = ?`, which a NULL repo
                # can never satisfy, so such a row is outside the count
                # this rule makes — the fold's own `_text_value`
                # fail-closed is what refuses it (see the NOTE below).
                # Probe-verified inert by the round-2 final critic: with
                # the branch deleted the full suite is green.
                allowed = ("text",) if not_null else ("text", "null")
            elif declared == "INTEGER":
                allowed = ("integer",) if not_null else ("integer", "null")
            else:
                continue  # no declared type: the column carries anything
            # Scope the count to the rows the adoption actually copies:
            # `WHERE lower(repo) = ?`, exactly `_copy_table`'s predicate. A
            # table-wide count would refuse the whole candidate for
            # corruption in a repo the user is not consolidating — a scope
            # the adoption never reads (swarm-pr-review N-09). `actors` has
            # no repo column and IS copied wholesale, so it stays
            # whole-table.
            scope = "lower(repo) = ? AND " if table != "actors" else ""
            params = (repo,) if table != "actors" else ()
            bad = conn.execute(
                f"SELECT COUNT(*) FROM {_quote_ident(table)}"
                f" WHERE {scope}typeof({_quote_ident(column)}) NOT IN"
                f" ({', '.join(repr(a) for a in allowed)})",
                params).fetchone()[0]
            if bad:
                raise _UnreadableCandidate(
                    path,
                    f"the candidate's {table}.{column} holds a divergent"
                    f" storage class in {bad} row(s): refused, not adopted")
    # NOTE: TWO of the three guards that used to sit here are subsumed by
    # the derived rule above (swarm-pr-review N-14) — the flag-column guard
    # (both flag columns are INTEGER NOT NULL) and the TEXT-column guard
    # (the TEXT branch honours `not_null` for every column). Keeping them
    # would leave two unreachable enumerations of one column family to
    # drift apart.
    # The THIRD, the repo NULL/non-text guard, is NOT subsumed and is not
    # redundant either: this rule's scope is `lower(repo) = ?`, which a
    # NULL or non-text repo can never satisfy, so those rows are outside
    # the count it makes and no column-level `allowed` set can reach them.
    # Its backstop is separate — the fold's own `_text_value` fail-closed
    # (db.py), which the winner's consolidation runs through. That is also
    # why `sync_state.repo` needs no special case in the rule: the round-2
    # final critic proved such a branch inert and it was deleted. Disclosed
    # by the final critic's round-1 finding F2, and corrected again after
    # cubic's re-review (PR #58), which caught this NOTE still claiming all
    # three were subsumed.
    # `actors.login` is the global table's key: a TEXT PRIMARY KEY on a
    # rowid table permits NULL in SQLite, and the adoption's upsert never
    # matches one, so every re-adoption of such a candidate would append
    # another NULL-login row and grow the store's global actors table
    # without bound (swarm-pr-review N-06). The table has no repo column,
    # so the count is whole-table — exactly the scope the copy reads.
    null_login = conn.execute(
        "SELECT COUNT(*) FROM actors WHERE login IS NULL"
        " OR typeof(login) <> 'text'").fetchone()[0]
    if null_login:
        raise _UnreadableCandidate(
            path,
            f"the candidate's actors table holds {null_login} row(s) with a"
            f" NULL or non-text login: a fabricated actor key is refused,"
            " not adopted")


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

    def __init__(self, path: Path, repo: str) -> None:
        self._td = tempfile.mkdtemp(prefix="zaxbygraph-doctor-")
        self.repo = repo
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
            if version > CURRENT_USER_VERSION:
                # A candidate newer than this build cannot be adopted
                # faithfully: its schema carries facts this build does not
                # know, and the adoption would silently drop them (the
                # sync_state INSERT names the store's columns, so an extra
                # column is lost; _COPY_TABLES members fail their INSERT,
                # but actors/revisions extras are ignored). Refuse it the
                # way `snapshot` refuses a newer store (store_too_new) —
                # the policy existed at one surface and not its sibling
                # (issue #56, the claim-2 prior-art reconciliation).
                raise _UnreadableCandidate(
                    path,
                    f"the candidate's schema (user_version {version}) is newer"
                    f" than this build supports ({CURRENT_USER_VERSION})")
            self.migrated = version < CURRENT_USER_VERSION
            self.version = version
            # The candidate's OWN table set, captured before any migration
            # (issue #56, round-6 reviewer finding 1): `init_schema` recreates
            # every table the candidate's version predates, so after the
            # migration the adoption can no longer tell "the candidate never
            # had item_events" from "the candidate has item_events and they
            # are empty". The delete loop must know the difference: deleting
            # the store's rows for a table the candidate never had copies
            # zero rows back, which is the truncated-image outcome AC3
            # refuses — the same class the per-item revisions scoping
            # preserves (issue #40).
            self.raw_tables: frozenset[str] = frozenset()
            if self.migrated:
                # A pre-migration storage-class check for the shapes the
                # migration-time fold reads (issue #56): the fold runs
                # inside init_schema's migrate_v1_to_v2, which the
                # post-migration preflight cannot precede, so a divergent
                # value there aborts the whole command. The raw candidate's
                # schema is pre-migration, so every check is gated on the
                # table and column existing — a genuine v1 store carries
                # none of the divergent shapes.
                raw = sqlite3.connect(str(self.path))
                try:
                    self.raw_tables = frozenset(
                        r[0] for r in raw.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                            " AND name NOT LIKE 'sqlite_%'"
                        )
                    )
                    for table, column, allowed in _RAW_STORAGE_CLASS_CHECKS:
                        if not _table_exists(raw, table):
                            continue
                        raw_cols = {
                            row[1] for row in raw.execute(
                                f"PRAGMA table_info({table})")
                        }
                        if column not in raw_cols:
                            continue
                        bad = raw.execute(
                            f"SELECT COUNT(*) FROM {table}"
                            f" WHERE typeof({column}) NOT IN"
                            f" ({', '.join(repr(a) for a in allowed)})"
                        ).fetchone()[0]
                        if bad:
                            raise _UnreadableCandidate(
                                path,
                                f"the candidate's {table}.{column} holds a"
                                f" divergent storage class in {bad} row(s):"
                                " refused, not adopted")
                finally:
                    raw.close()
            conn = connect(self.path)
            try:
                if self.migrated:
                    init_schema(conn)
                # One structural preflight for everything the adoption reads
                # from the candidate (issue #56): an ordinary older
                # candidate was just migrated to the current shape, so this
                # refuses only genuinely divergent schemas — before any
                # store write and before the fold sees them.
                _require_adoptable_schema(conn, path, self.repo)
                # Scope the fold's `sync_state` half to the consolidated
                # identity, so a malformed row for a repo this adoption
                # never copies cannot refuse the candidate — the same
                # isolation the repo-scoped storage-class count gives
                # (swarm-pr-review N-09; cubic review, PR #58).
                fold_repo_casings(conn, self.repo)
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
def _migrated_copy(path: Path, repo: str):
    """Context manager yielding the migrated temp copy (issue #56).

    Yields the `_MigratedCopy` itself, not just its path: the adoption
    needs the copy's pre-migration facts (`raw_tables`, `migrated`), which
    the migration itself destroys. `repo` scopes the preflight's
    storage-class counts to the rows the adoption copies."""
    copy = _MigratedCopy(path, repo)
    try:
        yield copy
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


def consolidate(store_db: Path, path: Path, repo: str) -> tuple[int, int, list[str]]:
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
    (adopted_rows, preserved_rows, preserved_tables): adopted = rows copied
    from the candidate; preserved = stored rows for numbers the candidate
    holds no revision rows for (a payload-identical covered number is a
    no-op — its rows are neither adopted nor preserved); preserved_tables =
    the repo-keyed tables the candidate's version never had, whose store
    rows are kept rather than replaced.

    The same preservation rule covers every other table the candidate's
    VERSION never had (issue #56, round-6 reviewer finding 1):
    `_MigratedCopy` migrates a legacy candidate, which recreates the
    tables that version predates — and BACKFILLS some of them from the
    candidate's own payload (v6→v7 `assignees`/`issue_field_values` from
    `raw_json`, v8→v9 `item_path_refs`/`item_signatures` from text) — so
    the post-migration preflight cannot see the absence and the
    unconditional delete would remove the store's rows for them while the
    copy brought the backfilled rows back: the truncated-image outcome
    AC3 refuses, on the tool's primary input class (legacy DBs).
    `_MigratedCopy.raw_tables` records the candidate's own table set before
    the migration; a table outside it is preserved — neither deleted nor
    copied (round-7 reviewer finding 1: the migration's backfill populates
    it, so copying it would collide with the store's preserved rows and
    refuse a genuine candidate) — and named in the envelope's
    `tables_preserved`, so the report says what happened instead of claiming
    a clean replace.
    """
    repo_l = repo.lower()
    preserved_tables: list[str] = []
    with _migrated_copy(path, repo_l) as copy:
        src = open_existing(copy.path)
        dst = connect(store_db)
        init_schema(dst)  # the store may not exist yet; doctor may create it
        try:
            dst.execute("BEGIN IMMEDIATE")
            try:
                for table, autoinc_pk in _COPY_TABLES:
                    if table == "revisions":
                        continue  # per-item scoping below (issue #40)
                    # A version-0 stamp means "before the v1 schema", which
                    # the migration chain treats as v1 (its first target is
                    # 2); clamp so a v1-shaped store stamped 0 is not read as
                    # predating every table.
                    stamped = max(copy.version, 1)
                    never_had = (table not in copy.raw_tables and copy.migrated)
                    stamp_predates = (
                        stamped < _TABLE_INTRODUCED_VERSION.get(table, 1))
                    if never_had or stamp_predates:
                        # Either the candidate's schema never carried the
                        # table (the migration recreated it), or its version
                        # predates the table's slot while the schema DOES
                        # carry it — an interrupted-upgrade remnant, because
                        # init_schema commits the current schema DDL and only
                        # then runs the migration chain, so a crash between
                        # them leaves the newer tables present under the old
                        # stamp. Deleting the store's rows for either shape
                        # and copying nothing back is the truncated image
                        # AC19 refuses (round-9 reviewer finding 1) — BUT the
                        # migration BACKFILLS several of these tables from the
                        # candidate's own payload (v6→v7 assignees /
                        # issue_field_values from raw_json, v8→v9
                        # item_path_refs / item_signatures from the bodies),
                        # so a candidate that DID make an observation must
                        # have it adopted rather than silently dropped
                        # (swarm-pr-review N-02/N-05). Preserve the store's
                        # rows only when the candidate holds none for this
                        # repo — the interrupted-upgrade signature, since the
                        # migration that would have populated the table never
                        # ran — and otherwise adopt the candidate's rows
                        # normally.
                        cand_rows = src.execute(
                            f"SELECT COUNT(*) FROM {_quote_ident(table)}"
                            " WHERE lower(repo) = ?", (repo_l,)).fetchone()[0]
                        if cand_rows:
                            # fall through to the replace below
                            pass
                        else:
                            preserved_tables.append(table)
                            continue
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
                    try:
                        dst.executemany(
                            insert_sql,
                            [tuple(r) + (repo_l, number) for r in cand_rows],
                        )
                    except sqlite3.DatabaseError as exc:
                        # The candidate's revisions rows can carry a value
                        # the STORE's DDL rejects — a NULL in a NOT NULL
                        # column such as valid_from/valid_to, which the
                        # schema-wide preflight cannot see because the
                        # candidate's own divergent DDL declares the column
                        # nullable. IntegrityError is a sqlite3.DatabaseError
                        # but NOT one the outer `except BaseException`
                        # converts: it would escape consolidate() and
                        # doctor() and lose the whole scan, healthy sibling
                        # included (swarm-pr-review N-01). The adoption is one
                        # transaction, so nothing has been written; report the
                        # candidate unusable, exactly as _copy_table does.
                        raise _UnreadableCandidate(
                            path,
                            f"the candidate's revisions rows cannot be written"
                            f" into the store: {exc}",
                        ) from exc
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
                    if table in preserved_tables:
                        # The same set the delete loop preserved (issue #56,
                        # round-7 reviewer finding 1): the candidate holds no
                        # rows for this repo in a table its version never
                        # populated, so there is nothing to copy and the
                        # store's rows stand. A table the candidate DID
                        # observe — including the rows the migration
                        # backfilled from its own payload — is not in the
                        # set and is adopted here (swarm-pr-review N-02).
                        continue
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
    return (adopted_rows, preserved_rows, preserved_tables)


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
            rev_adopted, rev_preserved, tables_preserved = consolidate(
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
            rev_adopted, rev_preserved, tables_preserved = 0, 0, []
        else:
            winner["adopted"] = True
            adopted = winner["path"]
    else:
        rev_adopted = 0
        rev_preserved = 0
        tables_preserved = []
    return {
        "ok": True,
        "adopted": adopted,
        "revisions_adopted": rev_adopted,
        "revisions_preserved": rev_preserved,
        "tables_preserved": sorted(tables_preserved),
        "scanned": scanned,
    }
