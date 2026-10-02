from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
from pathlib import Path
from typing import TextIO

from zaxbygraph.extract import item_kind
from zaxbygraph.github import GitHubError, GitHubSource
from zaxbygraph.repo import validate_slug
from zaxbygraph.store import (
    ingest_item,
    log_fetch,
    mark_sync_finished,
    recount,
    replace_releases,
    set_last_error,
    utcnow,
)


class SyncError(RuntimeError):
    pass


def _error_text(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text[:2000]


def _write_jsonl(handle: TextIO | None, resource: str, payload: object) -> None:
    if handle is None:
        return
    handle.write(json.dumps({"resource": resource, "payload": payload}, ensure_ascii=False))
    handle.write("\n")
    handle.flush()


def _ensure_state_row(conn: sqlite3.Connection, repo: str, include_patches: bool) -> None:
    conn.execute(
        """
        INSERT INTO sync_state(repo, include_patches)
        VALUES (?, ?)
        ON CONFLICT(repo) DO UPDATE SET include_patches = excluded.include_patches
        """,
        (repo, 1 if include_patches else 0),
    )
    conn.commit()


def sync_repo(
    conn: sqlite3.Connection,
    source: GitHubSource,
    repo: str,
    *,
    force: bool = False,
    include_patches: bool = False,
    jsonl_path: Path | None = None,
) -> dict:
    """Incremental sync. Each item is one IMMEDIATE transaction."""
    # Everything that touches the database lives inside the try: the recorder
    # must see prologue failures too (force reset, state-row ensure, pending
    # marker) — a lock or disk fault landing there is still a failed sync and
    # may not escape with state looking clean. validate_slug is pure and stays
    # out; set_last_error upserts the state row, so recording works even when
    # the row was never created.
    repo = validate_slug(repo)  # canonical lowercase identity for every key
    ingested = 0
    last_number: int | None = None
    jsonl_handle: TextIO | None = None
    try:
        if force:
            # The force reset runs first so the pending marker below is
            # computed from the watermark this run will actually use.
            conn.execute(
                "UPDATE sync_state SET issues_since = NULL, last_error = NULL WHERE repo = ?",
                (repo,),
            )
            conn.commit()
        _ensure_state_row(conn, repo, include_patches)

        row = conn.execute(
            "SELECT issues_since, full_sync_pending FROM sync_state WHERE repo = ?", (repo,)
        ).fetchone()
        since = None if row is None else row["issues_since"]
        pending_at_start = bool(row is not None and row["full_sync_pending"])
        if since is None:
            # Beginning a full sync (fresh repo or --force): mark it pending
            # so a crash mid-run leaves complete=false, and the resuming run
            # can stamp last_full_sync_at when it drains the listing.
            conn.execute(
                "UPDATE sync_state SET full_sync_pending = 1 WHERE repo = ?", (repo,)
            )
            conn.commit()
            pending_at_start = True
        # "full" in the result means: this run completes a full sync (it
        # started one, or it resumed an interrupted one to a clean finish).
        full = since is None or pending_at_start

        # Sidecar setup is a failure path too (e.g. the target path is a
        # regular file). The finally guard tolerates a half-failed setup
        # because jsonl_handle is None until it opens.
        if jsonl_path is not None:
            jsonl_path.mkdir(parents=True, exist_ok=True)
            jsonl_handle = (jsonl_path / "events.jsonl").open("a", encoding="utf-8")
        for list_raw in source.list_issues(since):
            if not isinstance(list_raw, dict) or "number" not in list_raw:
                continue
            number = int(list_raw["number"])
            kind = item_kind(list_raw)
            pull_raw: dict | None = None
            issue_comments: list[dict] = []
            review_comments: list[dict] = []
            reviews: list[dict] = []
            files: list[dict] = []
            try:
                comment_count = list_raw.get("comments")
                if comment_count:
                    issue_comments = source.list_issue_comments(number)
                if kind == "pr":
                    pull_raw = source.get_pull(number)
                    reviews = source.list_reviews(number)
                    review_comments = source.list_review_comments(number)
                    changed = (pull_raw or {}).get("changed_files")
                    if changed:
                        files = source.list_pr_files(number)
                    elif changed is None:
                        files = source.list_pr_files(number)
            except GitHubError:
                raise

            conn.execute("BEGIN IMMEDIATE")
            try:
                ingest_item(
                    conn,
                    repo,
                    list_raw,
                    pull_raw=pull_raw,
                    issue_comments=issue_comments,
                    review_comments=review_comments,
                    reviews=reviews,
                    files=files,
                    include_patches=include_patches,
                )
                if len(files) >= 3000:
                    log_fetch(
                        conn,
                        repo,
                        "pr_files",
                        str(number),
                        note="truncated at GitHub 3000-file cap",
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            ingested += 1
            last_number = number
            _write_jsonl(jsonl_handle, "item", list_raw)
            if pull_raw:
                _write_jsonl(jsonl_handle, "pull", pull_raw)
            for rec in issue_comments:
                _write_jsonl(jsonl_handle, "issue_comment", rec)
            for rec in review_comments:
                _write_jsonl(jsonl_handle, "review_comment", rec)
            for rec in reviews:
                _write_jsonl(jsonl_handle, "review", rec)
            for rec in files:
                _write_jsonl(jsonl_handle, "pr_file", rec)

        try:
            releases = source.list_releases()
        except GitHubError as exc:
            conn.execute("BEGIN IMMEDIATE")
            set_last_error(conn, repo, str(exc))
            conn.commit()
            raise SyncError(str(exc)) from exc

        conn.execute("BEGIN IMMEDIATE")
        try:
            replace_releases(conn, repo, releases)
            mark_sync_finished(conn, repo)
            recount(conn, repo)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        for rec in releases:
            _write_jsonl(jsonl_handle, "release", rec)
    except BaseException as exc:
        # Every failure leaves a truthful trail. A BaseException (decode
        # error, AttributeError on a dead reader thread, KeyboardInterrupt)
        # can escape between BEGIN IMMEDIATE and commit, so roll any open
        # transaction back before recording, or the recorder itself would
        # hit "cannot start a transaction within a transaction".
        if conn.in_transaction:
            conn.rollback()
        try:
            conn.execute("BEGIN IMMEDIATE")
            set_last_error(conn, repo, _error_text(exc))
            conn.commit()
        except sqlite3.Error:
            pass  # a dying database cannot record; propagate the original
        if isinstance(exc, (GitHubError, sqlite3.Error)):
            raise SyncError(str(exc)) from exc
        raise
    finally:
        if jsonl_handle is not None:
            # Every record is flushed as it is written, so a failing close
            # loses nothing; and an exception raised in finally is not caught
            # by the sibling except, so it must not escape unrecorded.
            try:
                jsonl_handle.close()
            except OSError:
                pass

    state = conn.execute(
        "SELECT * FROM sync_state WHERE repo = ?", (repo,)
    ).fetchone()
    return {
        "repo": repo,
        "ingested": ingested,
        "last_number": last_number,
        "full": full,
        "finished_at": utcnow(),
        "issues_since": None if state is None else state["issues_since"],
        "item_count": None if state is None else state["item_count"],
        "comment_count": None if state is None else state["comment_count"],
        "edge_count": None if state is None else state["edge_count"],
        "last_error": None if state is None else state["last_error"],
    }


# --- whole-run sync lock (issue #2 AC5) -------------------------------------
#
# Layout: byte 0 is a sentinel kept under an OS byte-range lock; the JSON
# payload {"pid", "host", "started_at"} lives at offset >= 1. Windows range
# locks are MANDATORY, so per-reader rules differ: the acquirer reads through
# its own locked descriptor (exempt from the lock) including byte 0 and
# parses from the first "{", which also tolerates hand-written files whose
# payload starts at 0; unlocked observers (e.g. `where`) read from offset 1
# best-effort and degrade when no "{" is found. The file is never unlinked
# by a process that does not own it: takeover rewrites the payload in place
# while holding the range lock, which makes concurrent stale-recovery atomic.


def lock_path_for(db_path: Path) -> Path:
    # Resolve first (PRR-002): raw --db spellings of the same file (relative
    # forms, trailing dots, symlink aliases) must share one lock.
    return Path(str(Path(db_path).resolve()) + ".sync.lock")


def _lock_range(fd: int) -> None:
    """Exclusive non-blocking lock on byte 0; raises OSError when held."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_range(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _write_payload_locked(fd: int, payload: dict) -> None:
    """Truncate-then-write at the sentinel layout (byte 0 = NUL sentinel,
    payload at offset 1). Truncation matters: an in-place shorter write over
    a longer hand-written payload would leave tail residue and a stale `{`
    at byte 0 that breaks parse-from-first-`{` for every later reader."""
    blob = b"\x00" + json.dumps(payload).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, blob)


def _read_payload_locked(fd: int) -> dict | None:
    """Acquirer-side read through the locked descriptor: from byte 0, parse
    from the first '{' (covers both the sentinel layout and hand-written
    files). None = no payload (free lock)."""
    os.lseek(fd, 0, os.SEEK_SET)
    data = os.read(fd, 65536)
    idx = data.find(b"{")
    if idx < 0:
        return None
    try:
        payload = json.loads(data[idx:].decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def read_lock_observer(db_path: Path) -> dict | str | None:
    """Observer-side read (`where`): offset 1, best-effort, never raises.
    Returns the holder dict, None (free), or the documented degradation."""
    path = lock_path_for(db_path)
    try:
        with open(path, "rb") as fh:
            fh.seek(1)
            data = fh.read(65536)
    except OSError:
        return None
    idx = data.find(b"{")
    if idx < 0:
        if data:
            return "held (holder unreadable)"
        return None
    try:
        payload = json.loads(data[idx:].decode("utf-8", errors="replace"))
    except ValueError:
        return "held (holder unreadable)"
    return payload if isinstance(payload, dict) else "held (holder unreadable)"


def _pid_alive(pid: int) -> bool:
    """Liveness of a recorded lock pid. Windows never uses os.kill(pid, 0)
    for this (it can terminate the process); OpenProcess failure with
    ERROR_ACCESS_DENIED means ALIVE - fail closed, never steal."""
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 0x103
        ERROR_ACCESS_DENIED = 5
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ctypes.GetLastError() == ERROR_ACCESS_DENIED
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        if not ok:
            return False
        return code.value == STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SyncLock:
    """A held whole-run lock. `release_owned` only on the acquired path;
    `abandon` on join/held paths so a joining sync never wipes the holder's
    payload."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self.fd = fd

    def release_owned(self) -> None:
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            os.ftruncate(self.fd, 0)
        finally:
            self._abandon()

    def abandon(self) -> None:
        """Unlock + close without touching the payload. The join/held paths
        in acquire_sync_lock call this; only the owner may truncate."""
        self._abandon()

    def _abandon(self) -> None:
        try:
            _unlock_range(self.fd)
        except OSError:
            pass
        finally:
            os.close(self.fd)
            self.fd = -1


def acquire_sync_lock(
    db_path: Path, *, wait: bool = False, poll_s: float = 0.2
) -> SyncLock | None:
    """Acquire the whole-run lock for db_path. None = held (join).

    Same-host holders with a dead pid are taken over atomically under the
    range lock; foreign-host holders are never stolen. wait=True polls until
    the lock frees instead of returning None.
    """
    path = lock_path_for(db_path)
    while True:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            _lock_range(fd)
        except OSError:
            os.close(fd)
            # Distinguish contention from an environmental fault (PRR-008):
            # when the observer can see a holder, another process owns the
            # lock; when it sees nothing, the range-lock failure was NOT
            # contention and must surface as an error, not a silent join.
            if read_lock_observer(path) is None:
                raise
            if not wait:
                return None
            time.sleep(poll_s)
            continue
        payload = _read_payload_locked(fd)
        if payload is None:
            _write_payload_locked(
                fd, {"pid": os.getpid(), "host": socket.gethostname(), "started_at": utcnow()}
            )
            return SyncLock(path, fd)
        holder_host = str(payload.get("host", ""))
        try:
            holder_pid = int(payload.get("pid", -1))
        except (TypeError, ValueError):
            holder_pid = -1
        if holder_host == socket.gethostname() and not _pid_alive(holder_pid):
            _write_payload_locked(
                fd, {"pid": os.getpid(), "host": socket.gethostname(), "started_at": utcnow()}
            )
            return SyncLock(path, fd)
        SyncLock(path, fd).abandon()
        if not wait:
            return None
        time.sleep(poll_s)
