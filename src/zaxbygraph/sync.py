from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from zaxbygraph.extract import item_kind
from zaxbygraph.github import GitHubError, GitHubSource
from zaxbygraph.repo import validate_slug
from zaxbygraph.store import (
    ISO_Z,
    bump_watermark,
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


#: Rate-limit windows slept through per fetch-layer call before giving up
#: (issue #5). Five is the ceiling; each sleep is additionally clamped so a
#: hostile or far-future reset can never park the sync (and its whole-run
#: lock) indefinitely.
_MAX_RATE_WINDOWS = 5

#: Upper bound on a single sleep-until-reset, in seconds: one hour, the
#: longest real GitHub window. A hostile or far-future reset can therefore
#: park the sync (and its whole-run lock) for at most one hour per window
#: instead of indefinitely; a legitimate window always sleeps in full.
_MAX_WINDOW_SLEEP_S = 60 * 60

#: Nested child sections a bulk item payload may carry inline, mapped to the
#: per-item REST fallback that completes them (issue #5 AC3).
_CHILD_FALLBACK = {
    "issue_comments": "list_issue_comments",
    "review_comments": "list_review_comments",
    "reviews": "list_reviews",
    "files": "list_pr_files",
}


def _batch(payload: object) -> list[dict]:
    """Normalize one yielded listing payload: an item dict is a one-item
    batch, a page list is itself, anything else is skipped."""
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [raw for raw in payload if isinstance(raw, dict)]
    return []


def _rate_attrs(exc: BaseException) -> tuple[int, str] | None:
    remaining = getattr(exc, "rate_limit_remaining", None)
    reset = getattr(exc, "rate_limit_reset", None)
    if remaining is None or reset is None:
        return None
    return (remaining, reset)


def _rate_reset_epoch(reset) -> float:
    """Parse an ISO-Z (or ISO) reset instant; an offset-less value is UTC.
    Anything unparseable raises so the caller can fall back to the original
    error."""
    if not isinstance(reset, str):
        raise ValueError(f"rate_limit_reset is not a string: {reset!r}")
    try:
        return datetime.strptime(reset, ISO_Z).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        parsed = datetime.fromisoformat(reset.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()


def _rate_sleep(exc: BaseException) -> None:
    """Sleep until the reported reset on the sync module's `time` (the
    injectable clock tests patch), clamped to `_MAX_WINDOW_SLEEP_S`. The
    reset string is consumed verbatim; an unparseable one re-raises the
    original rate-limit error rather than a parse failure."""
    reset = getattr(exc, "rate_limit_reset")
    try:
        reset_epoch = _rate_reset_epoch(reset)
    except (ValueError, TypeError, OSError, OverflowError):
        raise exc
    delay = min(_MAX_WINDOW_SLEEP_S, max(0.0, reset_epoch - time.time()))
    time.sleep(delay)


def _call_with_rate_retry(fn, *args):
    """Run one fetch-layer call under the bounded sleep-until-reset loop
    (issue #5 AC4). Raises the last error once `_MAX_RATE_WINDOWS` windows
    have been slept through."""
    for attempt in range(_MAX_RATE_WINDOWS + 1):
        try:
            return fn(*args)
        except GitHubError as exc:
            if _rate_attrs(exc) is None or attempt >= _MAX_RATE_WINDOWS:
                raise
            _rate_sleep(exc)
    raise AssertionError("unreachable")


class _BudgetWindow:
    """Carrier for a source-observed budget floor so `_rate_sleep` can
    consume it exactly like an enriched error."""

    def __init__(self, remaining: int, reset: str) -> None:
        self.rate_limit_remaining = remaining
        self.rate_limit_reset = reset


def _source_budget_floor(source) -> tuple[int, str] | None:
    """(remaining, reset_at) when the source tracks a live budget
    (GraphQLSource, from each page query's rateLimit selection) and that
    budget has reached the floor; None otherwise."""
    remaining = getattr(source, "rate_limit_remaining", None)
    reset_at = getattr(source, "rate_limit_reset_at", None)
    if isinstance(remaining, int) and isinstance(reset_at, str) and remaining <= 0:
        return (remaining, reset_at)
    return None


def _resolve_children(
    source: GitHubSource,
    list_raw: dict,
    kind: str,
    include_patches: bool,
    provided: dict | None = None,
) -> tuple[dict | None, list[dict], list[dict], list[dict], list[dict]]:
    """Children for one item: inline or bulk-provided payload where present
    and complete, per-item REST fallback where flagged incomplete or absent
    (issue #5). `provided` is what the source's fetch_children bulk call
    returned for this item; inline payload keys take precedence.

    The absent-everywhere path keeps the legacy rules (comments only when the
    count is nonzero, pull details only for PRs, files via changed_files),
    which is what plain REST sources hit. `include_patches` forces the REST
    files fetch even when a payload carried files: GraphQL cannot serve
    patches.
    """
    number = int(list_raw["number"])
    pull_raw = list_raw.get("pull")
    if pull_raw is None and provided is not None:
        pull_raw = provided.get("pull")
    if pull_raw is None and kind == "pr" and "pull" not in list_raw and not (
        provided is not None and "pull" in provided
    ):
        pull_raw = source.get_pull(number)

    sections: dict[str, list[dict]] = {}
    for name, fallback_name in _CHILD_FALLBACK.items():
        provided_section = provided.get(name) if provided is not None else None
        provided_flagged = bool(provided.get(f"{name}_incomplete")) if provided is not None else False
        if name in list_raw:
            if include_patches and name == "files":
                sections[name] = list(source.list_pr_files(number))
            elif list_raw.get(f"{name}_incomplete"):
                sections[name] = list(getattr(source, fallback_name)(number))
            else:
                sections[name] = list(list_raw[name] or [])
        elif provided_section is not None or provided_flagged:
            if include_patches and name == "files":
                sections[name] = list(source.list_pr_files(number))
            elif provided_flagged:
                sections[name] = list(getattr(source, fallback_name)(number))
            else:
                sections[name] = list(provided_section or [])
        elif kind == "pr" or name == "issue_comments":
            if name == "issue_comments":
                if list_raw.get("comments"):
                    sections[name] = list(source.list_issue_comments(number))
                else:
                    sections[name] = []
            elif name == "files":
                changed = (pull_raw or {}).get("changed_files")
                if include_patches and name == "files":
                    sections[name] = list(source.list_pr_files(number))
                elif changed or changed is None:
                    sections[name] = list(source.list_pr_files(number))
                else:
                    sections[name] = []
            else:
                sections[name] = list(getattr(source, fallback_name)(number))
        else:
            sections[name] = []
    return (
        pull_raw,
        sections["issue_comments"],
        sections["review_comments"],
        sections["reviews"],
        sections["files"],
    )


def _mark_gone(
    conn: sqlite3.Connection, repo: str, number: int, verdict: str, note: str
) -> None:
    """Record a deletion verdict in place (issue #5 AC5): items.state changes,
    the row stays (edges pointing at it survive), and a fetch_log trail names
    the verdict with a null http_status (no fetch happened on this row)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE items SET state = ?, state_reason = ? "
            "WHERE repo = ? AND number = ?",
            (verdict, f"marked {verdict}: {note}", repo, number),
        )
        log_fetch(conn, repo, "item", str(number), note=f"marked {verdict}: {note}", status=None)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _deletion_pass(
    conn: sqlite3.Connection, source: GitHubSource, repo: str, seen_numbers: set[int]
) -> None:
    """Consult the source's deletion oracle after a drained full listing.

    Only a run that started at since=None may mark: absence from an
    incremental listing means "not updated", never "gone". The candidate set
    is stored, unmarked items minus everything seen across the whole run
    (including rate-limit restarts). The probe runs under the bounded
    rate-limit retry so a window cannot fail an otherwise-complete run."""
    checker = getattr(source, "check_deleted", None)
    if not callable(checker):
        return
    rows = conn.execute(
        "SELECT number FROM items WHERE repo = ? AND state NOT IN ('deleted','transferred')",
        (repo,),
    ).fetchall()
    candidates = sorted(
        {int(r["number"]) for r in rows} - set(seen_numbers)
    )
    if not candidates:
        return
    verdicts = _call_with_rate_retry(checker, candidates) or {}
    if not isinstance(verdicts, dict):
        return
    allowed = set(candidates)
    clean: dict[int, str] = {}
    for n, v in verdicts.items():
        # Source-supplied keys/values are untrusted: malformed rows are
        # skipped (never crash a completed run), unknown verdicts are
        # dropped silently by design.
        try:
            number = int(n)
            text = str(v)
        except (TypeError, ValueError):
            continue
        if number in allowed and text in ("deleted", "transferred"):
            clean[number] = text
    for number, verdict in sorted(clean.items()):
        _mark_gone(
            conn, repo, number, verdict,
            "reported by source deletion check after a completed full listing",
        )


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
        started_full = since is None
        seen_numbers: set[int] = set()
        rate_window: tuple[int, str] | None = None

        # Sidecar setup is a failure path too (e.g. the target path is a
        # regular file). The finally guard tolerates a half-failed setup
        # because jsonl_handle is None until it opens.
        if jsonl_path is not None:
            jsonl_path.mkdir(parents=True, exist_ok=True)
            jsonl_handle = (jsonl_path / "events.jsonl").open("a", encoding="utf-8")
        # The listing is consumed in pages and each item commits with its
        # own watermark bump, so everything delivered before a failure is
        # durable (issue #5 AC1). A rate-limit window is slept out on the
        # injectable clock and the listing restarts from a fresh generator;
        # re-ingesting committed items is a no-op upsert and the watermark
        # never regresses. Attr-bearing errors only: anything else fails
        # fast exactly as before. A source that exposes its live budget
        # (GraphQLSource) is also checked proactively at the top of each
        # attempt, so a budget already at the floor sleeps BEFORE burning
        # the attempt (issue #5 AC4's "reaches the floor" clause).
        attempts = 0
        while True:
            try:
                # Proactive floor check (issue #5 AC4 "reaches the floor"):
                # a source tracking its live budget sleeps BEFORE the attempt
                # instead of burning it on a guaranteed 429. Each floor sleep
                # counts toward the window cap, so this is bounded.
                floor = _source_budget_floor(source)
                if floor is not None and attempts < _MAX_RATE_WINDOWS:
                    rate_window = floor
                    attempts += 1
                    _rate_sleep(_BudgetWindow(*floor))
                for payload in source.list_issues(since):
                    batch = _batch(payload)
                    # One bulk children call per listing page when the source
                    # offers it (issue #5 AC2); items the source did not
                    # answer fall through to the per-item REST path.
                    provided_map: dict[int, dict] = {}
                    fetch = getattr(source, "fetch_children", None)
                    if callable(fetch) and batch:
                        provided_map = fetch(batch) or {}
                    for list_raw in batch:
                        if not isinstance(list_raw, dict) or "number" not in list_raw:
                            continue
                        try:
                            number = int(list_raw["number"])
                        except (TypeError, ValueError):
                            conn.execute("BEGIN IMMEDIATE")
                            try:
                                log_fetch(
                                    conn, repo, "item", None,
                                    note=f"skipped malformed listing number: "
                                    f"{str(list_raw['number'])[:100]!r}",
                                )
                                conn.commit()
                            except Exception:
                                conn.rollback()
                                raise
                            continue
                        seen_numbers.add(number)
                        kind = item_kind(list_raw)
                        try:
                            pull_raw, issue_comments, review_comments, reviews, files = _resolve_children(
                                source, list_raw, kind, include_patches,
                                provided=provided_map.get(number),
                            )
                        except GitHubError as exc:
                            if exc.status in (404, 410):
                                # A first-sighting 404 stores the listing
                                # payload so the marking is truthful and the
                                # watermark advances past it. An item that was
                                # ALREADY stored keeps its children and edges:
                                # ingest_item replaces child rows and rebuilt
                                # edges from scratch, so re-ingesting with
                                # empty children would destroy the last local
                                # copy of a deleted item's discussion. This
                                # matches the oracle path, which marks without
                                # touching children.
                                exists = conn.execute(
                                    "SELECT 1 FROM items WHERE repo = ? AND number = ?",
                                    (repo, number),
                                ).fetchone() is not None
                                if not exists:
                                    conn.execute("BEGIN IMMEDIATE")
                                    try:
                                        ingest_item(
                                            conn, repo, list_raw,
                                            pull_raw=None,
                                            issue_comments=[], review_comments=[],
                                            reviews=[], files=[],
                                            include_patches=include_patches,
                                        )
                                        conn.commit()
                                    except Exception:
                                        conn.rollback()
                                        raise
                                    ingested += 1
                                    last_number = number
                                    _write_jsonl(jsonl_handle, "item", list_raw)
                                else:
                                    # Advance the watermark past the marked
                                    # item without re-ingesting: otherwise
                                    # every later run re-lists it until its
                                    # updated_at ages past the watermark.
                                    conn.execute("BEGIN IMMEDIATE")
                                    try:
                                        bump_watermark(conn, repo, list_raw.get("updated_at"))
                                        conn.commit()
                                    except Exception:
                                        conn.rollback()
                                        raise
                                _mark_gone(
                                    conn, repo, number, "deleted",
                                    f"GitHub returned HTTP {exc.status} while fetching item data",
                                )
                                continue
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
                break
            except GitHubError as exc:
                window = _rate_attrs(exc)
                if window is None or attempts >= _MAX_RATE_WINDOWS:
                    raise
                rate_window = window
                attempts += 1
                _rate_sleep(exc)

        # Deletion oracle: only a drained full listing may mark items gone
        # (issue #5 AC5). Incremental absence marks nothing. Runs under the
        # bounded retry so a window cannot fail an otherwise-complete run.
        if started_full:
            _deletion_pass(conn, source, repo, seen_numbers)

        try:
            releases = _call_with_rate_retry(source.list_releases)
        except GitHubError as exc:
            conn.execute("BEGIN IMMEDIATE")
            set_last_error(conn, repo, str(exc))
            conn.commit()
            raise SyncError(str(exc)) from exc

        if rate_window is None:
            # A clean run that slept no window still reports the last budget
            # the source observed (GraphQLSource tracks it per page query).
            rate_window = _source_budget_floor(source)

        conn.execute("BEGIN IMMEDIATE")
        try:
            replace_releases(conn, repo, releases)
            mark_sync_finished(
                conn,
                repo,
                rate_limit_remaining=None if rate_window is None else rate_window[0],
                rate_limit_reset_at=None if rate_window is None else rate_window[1],
            )
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
        "rate_limit_remaining": None if rate_window is None else rate_window[0],
        "rate_limit_reset_at": None if rate_window is None else rate_window[1],
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


def _observe_lock_file(lock_path: Path) -> dict | str | None:
    """Read an ALREADY-DERIVED lock file path (offset 1, best-effort)."""
    try:
        with open(lock_path, "rb") as fh:
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


def read_lock_observer(db_path: Path) -> dict | str | None:
    """Observer-side read (`where`): derives the lock path from the db."""
    return _observe_lock_file(lock_path_for(db_path))


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


_FAULT_REPROBE_PAUSE = 0.02  # bounded re-probe pause (PRR-008)


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
        except OSError as lock_fault:
            os.close(fd)
            # Distinguish contention from an environmental fault (PRR-008).
            # The first instant is ambiguous: a genuine holder may hold the
            # range lock with its payload not yet (re)written, while a
            # transient environmental fault fails identically. Bounded
            # re-probe (reviewer prescription): after one short pause,
            # a holder still locks the file AND its payload is observable;
            # a persistent fault keeps failing with nothing to observe -
            # report it (a silent joined:true here was the original
            # PRR-008 silent false-success).
            time.sleep(_FAULT_REPROBE_PAUSE)
            fd2 = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                _lock_range(fd2)
            except OSError:
                os.close(fd2)
                holder = _observe_lock_file(path)
                if holder is None:
                    raise lock_fault
                if not wait:
                    return None
                time.sleep(poll_s)
                continue
            # The re-probe ACQUIRED the range lock: the earlier failure was
            # transient (and any prior holder died within the window, where
            # our payload legitimately replaces theirs). Take ownership.
            _write_payload_locked(
                fd2, {"pid": os.getpid(), "host": socket.gethostname(), "started_at": utcnow()}
            )
            return SyncLock(path, fd2)
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
