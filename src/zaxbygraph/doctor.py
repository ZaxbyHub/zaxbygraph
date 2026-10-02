"""Issue #2: discovery and consolidation for scattered legacy DBs.

`doctor` reports every legacy per-checkout DB holding the resolved slug;
`doctor --consolidate` copies the freshest COMPLETE corpus into the
user-level store. Originals are never modified or deleted: inspection and
adoption always work on temp copies (a mode=ro open of a WAL-mode DB would
still materialize -shm/-wal beside the original, so even inspection goes
through a copy).

Report shape (--format json):
    {"ok": true, "adopted": <path|null>,
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
from pathlib import Path

from zaxbygraph.db import CURRENT_USER_VERSION, connect, init_schema, open_existing
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

#: tables copied on adoption -> the autoincrement pk column to omit (None =
#: keep every column; GitHub-assigned ids in items/releases/reviews are kept).
_COPY_TABLES = (
    ("items", None),
    ("labels", None),
    ("comments", "pk"),
    ("reviews", None),
    ("pr_files", None),
    ("releases", None),
    ("edges", "id"),
    ("fetch_log", "id"),
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


def inspect(path: Path, repo: str) -> dict | None:
    """Per-DB stats for one slug, read from a temp copy. None = unreadable."""
    repo_l = repo.lower()
    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / path.name
        try:
            shutil.copy2(path, copy)
        except OSError:
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
                state = conn.execute(
                    "SELECT issues_since FROM sync_state WHERE lower(repo) = ?", (repo_l,)
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
                        f"SELECT ({complete_expr}) AS c FROM sync_state WHERE lower(repo) = ?",
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


class _MigratedCopy:
    """Temp copy of a legacy DB, migrated to the current schema if needed.

    connect+init_schema runs the MIGRATIONS framework on the copy (case
    folds, adds full_sync_pending); the original is never opened for write.
    The temp directory is removed on close (PRR-006: every --consolidate
    used to leak a full DB copy into the system temp dir).
    """

    def __init__(self, path: Path) -> None:
        self._td = tempfile.mkdtemp(prefix="zaxbygraph-doctor-")
        self.path = Path(self._td) / path.name
        shutil.copy2(path, self.path)
        conn = sqlite3.connect(str(self.path))
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.DatabaseError:
            self.migrated = False
            conn.close()
            return
        conn.close()
        self.migrated = version < CURRENT_USER_VERSION
        if self.migrated:
            conn = connect(self.path)
            init_schema(conn)
            conn.close()

    def close(self) -> None:
        shutil.rmtree(self._td, ignore_errors=True)


from contextlib import contextmanager


@contextmanager
def _migrated_copy(path: Path):
    """Context manager yielding the migrated temp copy path."""
    copy = _MigratedCopy(path)
    try:
        yield copy.path
    finally:
        copy.close()


def consolidate(store_db: Path, path: Path, repo: str) -> None:
    """Adopt one candidate into the store: REPLACE-THE-SLUG.

    One transaction: delete every store row whose repo is the folded slug,
    then insert the candidate's rows with repo lowered - omitting
    AUTOINCREMENT pk columns (comments.pk, edges.id, fetch_log.id) so they
    re-assign, keeping GitHub-assigned ids (items.id, releases.id,
    reviews.id). sync_state is replaced and counts recomputed. Plain
    INSERTs only: post-delete there is nothing to conflict with, and
    INSERT OR REPLACE's double-unique semantics could silently drop rows.
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
                    dst.execute(f"DELETE FROM {table} WHERE repo = ?", (repo_l,))
                dst.execute("DELETE FROM sync_state WHERE repo = ?", (repo_l,))

                for table, autoinc_pk in _COPY_TABLES:
                    cols = [
                        row[1] for row in src.execute(f"PRAGMA table_info({table})")
                    ]
                    if autoinc_pk is not None:
                        cols = [c for c in cols if c != autoinc_pk]
                    if not cols:
                        continue
                    if "repo" in cols:
                        select_cols = ", ".join(
                            "lower(repo)" if c == "repo" else c for c in cols
                        )
                    else:
                        select_cols = ", ".join(cols)
                    placeholders = ", ".join("?" for _ in cols)
                    rows = src.execute(
                        f"SELECT {select_cols} FROM {table}"
                        + (" WHERE lower(repo) = ?" if "repo" in cols else ""),
                        (repo_l,) if "repo" in cols else (),
                    ).fetchall()
                    insert_sql = (
                        f"INSERT INTO {table} ({', '.join(cols)}) "
                        f"VALUES ({placeholders})"
                    )
                    dst.executemany(insert_sql, [tuple(r) for r in rows])

                state = src.execute(
                    "SELECT * FROM sync_state WHERE lower(repo) = ?", (repo_l,)
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
            continue
        entry["adopted"] = False
        scanned.append(entry)
        if entry["complete"] and entry["items"] > 0:
            key = entry["issues_since"] or ""
            if best is None or key > best[0]:
                best = (key, entry)
    if consolidate_flag and best is not None:
        winner = best[1]
        consolidate(store_db, Path(winner["path"]), repo_l)
        winner["adopted"] = True
        adopted = winner["path"]
    return {"ok": True, "adopted": adopted, "scanned": scanned}
