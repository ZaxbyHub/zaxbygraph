from __future__ import annotations

import argparse
import difflib
import errno
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zaxbygraph import __version__
from zaxbygraph import derive
from zaxbygraph import export
from zaxbygraph.db import connect, connect_readonly_query, init_schema, open_existing
from zaxbygraph.doctor import doctor as doctor_run
from zaxbygraph.snapshot import SnapshotError, publish as snapshot_publish, pull as snapshot_pull
# The default sync source is the GraphQL bulk source (issue #5); it is bound
# under the name the test suite patches (`zaxbygraph.cli.GhApiSource`), and
# the REST fallback stays reachable as GhRestSource for `--source rest`.
from zaxbygraph.github import GhApiSource as GhRestSource
from zaxbygraph.graphql import GraphQLSource as GhApiSource
from zaxbygraph.paths import (
    default_jsonl_dir,
    git_common_root,
    legacy_db_paths,
    resolve_db,
    store_db_path,
)
from zaxbygraph.query import (
    apply_item_bounds,
    assert_read_sql,
    churn,
    _clamp_limit,
    export_graph,
    file_history,
    item,
    item_as_of,
    item_history,
    open_items,
    path_between,
    pr_overlap,
    related,
    run_sql,
    search,
    sql_deadline_from_env,
    sql_max_bytes_from_env,
    SqlTimeoutError,
    status,
    store_tables_hint,
    what_closed,
)
from zaxbygraph.repo import DEFAULT_HOST, RepoError, remote_info, validate_slug
from zaxbygraph.schema_notes import describe_schema
from zaxbygraph.sync import (
    SyncError,
    SyncLock,
    acquire_sync_lock,
    read_lock_observer,
    sync_repo,
)


def _force_utf8_streams() -> None:
    """Pin stdout/stderr to UTF-8 regardless of the ambient locale.

    On Windows without UTF-8 overrides, a piped stdout is cp1252 and
    `json.dump(..., ensure_ascii=False)` aborts mid-encode on the first
    emoji, leaving truncated JSON on a zero-exit-looking pipe. `replace`
    keeps a lone surrogate in legacy garbled rows from crashing output.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            # stdin included: the MCP server decodes protocol frames from
            # it, and MCP stdio frames are UTF-8 by spec — a piped stdin
            # left on the Windows locale encoding would mojibake (or
            # crash decoding) non-ASCII frames.
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _format_of(args: argparse.Namespace) -> str:
    fmt = getattr(args, "format", None)
    if fmt in ("json", "compact", "jsonl", "text"):
        return fmt
    return "text" if sys.stdout.isatty() else "json"


def _fields_of(args: argparse.Namespace) -> list[str]:
    raw = getattr(args, "fields", None)
    if not raw:
        return []
    return [f.strip() for f in str(raw).split(",") if f.strip()]


def _freshness(conn: sqlite3.Connection | None, slug: str | None) -> dict:
    """The envelope's freshness block (issue #3 AC2).

    NULL-safe by construction: `mark_sync_finished` stamps exactly one of
    `last_full_sync_at`/`last_incr_sync_at` per run, so after a first full
    sync the other column is NULL — never max() the raw pair.
    """
    out: dict[str, Any] = {"synced_at": None, "age_s": None, "complete": False}
    if conn is None or not slug:
        return out
    try:
        row = conn.execute(
            "SELECT last_full_sync_at, last_incr_sync_at, full_sync_pending,"
            " last_error FROM sync_state WHERE lower(repo) = ?",
            (slug,),
        ).fetchone()
    except sqlite3.Error:
        return out
    if row is None:
        return out
    stamps = [s for s in (row["last_full_sync_at"], row["last_incr_sync_at"]) if s]
    if stamps:
        synced_at = max(stamps)
        out["synced_at"] = synced_at
        try:
            parsed = datetime.fromisoformat(synced_at.replace("Z", "+00:00"))
            out["age_s"] = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
        except ValueError:
            out["age_s"] = None
    out["complete"] = not row["full_sync_pending"] and row["last_error"] is None
    return out


def _item_count(conn: sqlite3.Connection | None, slug: str | None) -> int:
    if conn is None or not slug:
        return 0
    try:
        row = conn.execute(
            "SELECT item_count FROM sync_state WHERE lower(repo) = ?", (slug,)
        ).fetchone()
    except sqlite3.Error:
        return 0
    if row is None or row["item_count"] is None:
        return 0
    return int(row["item_count"])


def corpus_state(
    conn: sqlite3.Connection | None, repo: str | None
) -> tuple[int, str | None]:
    """(live repo-scoped item count, sync_state.last_full_sync_at) — the ONE
    shared no-corpus decision input (issue #72 C10 / plan CF-E).

    The count is a live COUNT(*) over items, never the cached
    sync_state.item_count finish-time snapshot: a run that committed items
    but never finished can sit at item_count=0 while real rows exist, and
    such a store must NOT be rejected as no-corpus. A missing items table
    counts 0. The decision rule (both adapters, CLI `_open_for_read` and
    the MCP resolution path, call this same helper so neither drifts):
    no corpus iff the live count is 0 AND last_full_sync_at IS NULL —
    zero items whose first full sync never completed.
    """
    count = 0
    last_full: str | None = None
    if conn is None or not repo:
        return count, last_full
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM items WHERE lower(repo) = ?", (repo,)
        ).fetchone()
        count = int(row[0]) if row is not None else 0
    except sqlite3.Error:
        # A store predating the items table (or unreadable now) has no
        # corpus this decision can see.
        count = 0
    try:
        row = conn.execute(
            "SELECT last_full_sync_at FROM sync_state WHERE lower(repo) = ?",
            (repo,),
        ).fetchone()
        if row is not None:
            last_full = row[0]
    except sqlite3.Error:
        last_full = None
    return count, last_full


def _identity_line(
    conn: sqlite3.Connection | None, slug: str | None, db_path: Path | None
) -> str | None:
    """The one stderr identity line every successful read prints (AC7).

    `synced` is the whole-second age (digits only — 0 means "no recorded
    sync age", always accompanied by complete=no); never the ISO timestamp.
    """
    if not slug:
        return None
    fresh = _freshness(conn, slug)
    age = fresh["age_s"]
    return (
        f"# db={db_path} repo={slug} items={_item_count(conn, slug)}"
        f" synced={age if age is not None else 0}"
        f" complete={'yes' if fresh['complete'] else 'no'}"
    )


_ERROR_CODES = {3: "no_corpus", 2: "bad_request", 1: "runtime"}


def _error_for(code_key: str, message: str, hint: str | None = None) -> dict:
    err: dict[str, Any] = {"code": code_key, "message": message}
    if hint:
        err["hint"] = hint
    return err


def _failure(code: int, message: str, hint: str | None = None) -> dict:
    return _error_for(_ERROR_CODES.get(code, "runtime"), message, hint)


def _project(row: Any, fields: list[str]) -> Any:
    if not fields or not isinstance(row, dict):
        return row
    out = {k: row[k] for k in fields if k in row}
    if row.get("not_found") is True:
        # The reserved per-row marker (multi-number item, issue #72 C22)
        # survives projection: it is the row's honest "this number is not
        # stored" flag, not a data field a caller narrows away.
        out["not_found"] = True
    return out


#: The per-command list selectors (issue #72 C13 / plan CF-F): the data key
#: whose list is that command's row stream for --fields projection and
#: --format jsonl. Commands whose data is already a bare list (open, churn,
#: multi-number item) need no selector; payloads without an entry here keep
#: their existing paths (single-number item stays a byte-identical object,
#: sql keeps its {columns, rows} projection).
_LIST_KEY_BY_COMMAND = {
    "search": "items",
    "related": "nodes",
    "similar": "candidates",
    "triage": "rows",
    "bundle": "bundles",
}


def _selected_rows(payload: Any, list_key: str | None) -> list[Any] | None:
    """The row stream a --fields/--format jsonl caller targets: a bare list
    payload, or the selector-named list inside a dict payload. None when
    this payload has no row stream."""
    if isinstance(payload, list):
        return payload
    if (
        list_key is not None
        and isinstance(payload, dict)
        and isinstance(payload.get(list_key), list)
    ):
        return payload[list_key]
    return None


def build_envelope(
    data: Any,
    *,
    conn: sqlite3.Connection | None = None,
    slug: str | None = None,
    db_path: Path | str | None = None,
    error: dict | None = None,
    truncated: bool = False,
    freshness: dict | None = None,
) -> dict:
    """The one envelope shape (issue #3 AC2): ok, db, repo, freshness,
    data, truncated — plus error on failures."""
    if freshness is None:
        freshness = _freshness(conn, slug)
    env: dict[str, Any] = {
        "ok": error is None,
        "db": str(db_path) if db_path else None,
        "repo": slug,
        "freshness": freshness,
        "data": None if error is not None else data,
        "truncated": bool(truncated),
    }
    if error is not None:
        env["error"] = error
    return env


def _render_json(env: dict, indent: int | None) -> None:
    json.dump(env, sys.stdout, indent=indent, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def emit_result(
    args: argparse.Namespace,
    data: Any,
    *,
    conn: sqlite3.Connection | None = None,
    slug: str | None = None,
    db_path: Path | str | None = None,
    error: dict | None = None,
    truncated: bool = False,
    freshness: dict | None = None,
    identity_line: str | None = None,
    identity: bool = True,
    echo: bool = True,
) -> None:
    """The single output owner (issue #3): envelope assembly, all formats,
    --fields projection, the stderr identity line on text-mode success
    (machine formats stay silent on stderr; the json/compact envelope keeps
    db, repo and freshness - not the line's items count - while jsonl list
    payloads print rows only with no envelope at all - issue #17; a dict
    payload's selected row list streams the same way, issue #72 C13), and the
    one-line stderr error echo on
    failure (the human contract the error-channel tests pin; the structured
    envelope goes to stdout)."""
    # --max-age refreshed the store on this invocation (issue #31 AC5): the
    # flag is stamped by _refresh_if_stale and merged into every dispatcher's
    # freshness block here, the one shared envelope builder.
    if getattr(args, "_refreshed", False):
        if freshness is None:
            freshness = _freshness(conn, slug)
        if freshness is not None:
            freshness = {**freshness, "refreshed": True}
    fields = _fields_of(args)
    list_key = _LIST_KEY_BY_COMMAND.get(getattr(args, "cmd", None))
    payload = data
    columns: list[str] | None = None
    if fields and error is None:
        if isinstance(data, list):
            payload = [_project(row, fields) for row in data]
        elif (
            list_key is not None
            and isinstance(data, dict)
            and isinstance(data.get(list_key), list)
        ):
            # The command's named row list (issue #72 C13): --fields narrows
            # the dict payload to that stream with its rows projected — the
            # projection is observable even when the stream is empty, and
            # the envelope still carries db/repo/freshness for identity.
            payload = {list_key: [_project(row, fields) for row in data[list_key]]}
        elif isinstance(data, dict) and isinstance(data.get("rows"), list):
            # sql's rows project despite dict-shaped data (plan item 5).
            # Objects mode projects keys; array mode projects positions so
            # columns and rows stay aligned (PRR-002).
            cols = data.get("columns")
            payload = dict(data)
            if (
                data["rows"]
                and isinstance(data["rows"][0], list)
                and isinstance(cols, list)
            ):
                idxs = [i for i, c in enumerate(cols) if c in fields]
                payload["rows"] = [
                    [row[i] for i in idxs if i < len(row)] for row in data["rows"]
                ]
                payload["columns"] = [cols[i] for i in idxs]
            else:
                payload["rows"] = [_project(row, fields) for row in data["rows"]]
                if isinstance(cols, list):
                    payload["columns"] = [c for c in cols if c in fields]
    env = build_envelope(
        payload,
        conn=conn,
        slug=slug,
        db_path=db_path,
        error=error,
        truncated=truncated,
        freshness=freshness,
    )
    if error is not None and echo:
        print(f"error: {error.get('message', '')}", file=sys.stderr)
    fmt = _format_of(args)
    if fmt == "text":
        # Failures answer through the stderr echo; rendering the null payload
        # would print a literal "None" to stdout (4.5 review finding 2).
        if error is None:
            _emit_text(payload)
    elif fmt == "jsonl":
        stream = None if error is not None else _selected_rows(payload, list_key)
        if stream is not None:
            for row in stream:
                sys.stdout.write(
                    json.dumps(row, ensure_ascii=False, default=str) + "\n"
                )
        else:
            _render_json(env, indent=None)
    elif fmt == "compact":
        _render_json(env, indent=None)
    else:
        _render_json(env, indent=2)
    if identity_line is None and identity and error is None and slug:
        identity_line = _identity_line(conn, slug, db_path)
    if identity_line and fmt == "text":
        # Text mode only (issue #17): machine formats must stay strict-parse
        # safe under merged-stream captures. The json/compact envelope still
        # carries db, repo and freshness (not the items count the line
        # printed); jsonl list payloads print rows only, so on that surface
        # the identity data is simply absent.
        print(identity_line, file=sys.stderr)


def _emit_text(data: Any) -> None:
    """The historical text-mode rendering: bare data, never envelope keys."""
    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, (dict, list)):
                print(f"{key}:")
                print(json.dumps(val, indent=2, ensure_ascii=False, default=str))
            else:
                print(f"{key}: {val}")
        return
    if isinstance(data, list):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return
    print(data)


def emit_failure(
    args: argparse.Namespace,
    code: int,
    message: str,
    *,
    slug: str | None = None,
    db_path: Path | str | None = None,
    code_key: str | None = None,
    hint: str | None = None,
) -> int:
    """Post-parse failure: structured error envelope on stdout, the one-line
    human echo on stderr, exit code unchanged."""
    err = (
        _error_for(code_key, message, hint)
        if code_key
        else _failure(code, message, hint)
    )
    emit_result(
        args,
        None,
        slug=slug,
        db_path=db_path,
        error=err,
        identity=False,
    )
    return code


class _ReadFailure(Exception):
    """A read that must not proceed: carries its exit code and message."""

    def __init__(self, code: int, message: str, code_key: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.code_key = code_key


class _UnknownPathError(Exception):
    """file-history's ghost-input refusal (issue #72 C11): the normalized
    path matches no stored path exactly. Carries the message and a
    suggestion hint (case-insensitive/basename near-misses, never a disk
    resolve)."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def _resolved_repo(args: argparse.Namespace) -> tuple[str | None, str]:
    """(repo, host) for a command. An explicitly empty --repo means
    "no filter" (repo None). A bare --repo slug carries no host: it keys
    the store under github.com (README documents the boundary)."""
    repo_flag = getattr(args, "repo", None)
    if repo_flag is None:
        host, slug = remote_info()
        return slug, host
    if repo_flag == "":
        return None, DEFAULT_HOST
    return validate_slug(repo_flag), DEFAULT_HOST


def _parse_max_age(value: str) -> int:
    """--max-age D: integer seconds or <n><s|m|h|d> (issue #31 AC5).

    A negative or malformed value raises ArgumentTypeError, which argparse
    turns into the standard usage error (exit 2)."""
    text = str(value).lower()
    if not text:
        raise argparse.ArgumentTypeError("empty --max-age value")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text[-1] in units and text[:-1]:
        num, unit = text[:-1], text[-1]
    else:
        num, unit = text, "s"
    # ASCII digits only: int() also accepts "1_0", "+5" and non-ASCII
    # numerics, silently reinterpreting mistyped budgets (PR review).
    if re.fullmatch(r"[0-9]+", num) is None:
        raise argparse.ArgumentTypeError(
            f"invalid --max-age value: {value!r}"
        )
    return int(num) * units[unit]


def _parse_comment_cap(value: str) -> int:
    """--max-comments K: ASCII non-negative integer, rejected at the parser
    so the answer does not depend on the item existing (PR review)."""
    text = str(value).strip()
    if re.fullmatch(r"[0-9]+", text) is None:
        raise argparse.ArgumentTypeError(
            f"invalid --max-comments value: {value!r}"
        )
    return int(text)


def _parse_offset(value: str) -> int:
    """--offset N (search paging, issue #72 C14): ASCII non-negative integer;
    negative values are rejected at the parser, never silently clamped."""
    text = str(value).strip()
    if re.fullmatch(r"[0-9]+", text) is None:
        raise argparse.ArgumentTypeError(
            f"invalid --offset value: {value!r}"
        )
    return int(text)


def _sync_core(
    conn: sqlite3.Connection,
    slug: str,
    *,
    force: bool,
    include_patches: bool,
    jsonl_path: Path | None,
    numbers: list[int] | None,
    source_name: str,
    stamp_source: str | None = None,
) -> dict:
    """One quiet sync_repo call against an open writable connection (issue #31).

    cmd_sync and the --max-age refresh share this so both paths issue exactly
    one sync_repo call. Neither path acquires the lock here — the caller owns
    the lock and maps failures to its own reporting channel (cmd_sync to its
    result envelope, the refresh path to a coded _ReadFailure). source_name
    constructs the fetch source (graphql default); stamp_source is what the
    run records as its requested profile — None (an unstated source) leaves
    any recorded value untouched (issue #48, cubic re-review)."""
    if source_name == "rest":
        source: object = GhRestSource(*slug.split("/", 1))
    else:
        source = GhApiSource(*slug.split("/", 1))
    return sync_repo(
        conn,
        source,
        slug,
        force=force,
        include_patches=include_patches,
        jsonl_path=jsonl_path,
        numbers=numbers,
        fetch_source=stamp_source,
        source_name=source_name,
    )


class _EnvironmentGuard(RuntimeError):
    """A non-retryable environment fault (schema newer than this build,
    SQLite floor) raised out of the refresh seam; the caller maps it to the
    coded environment_guard envelope, exit 2 — matching the sync path —
    while every other refresh failure stays refresh_failed, exit 1 (the
    frozen C11 contract)."""


def _run_refresh_sync(
    conn: sqlite3.Connection,
    slug: str,
    *,
    include_patches: bool = False,
    source_name: str | None = None,
) -> int:
    """The --max-age refresh seam (issue #31 AC5): one quiet incremental sync
    against the open writable connection, returning 0 on success and raising
    on failure (the caller reports the error envelope — never a stale answer
    with a false refreshed bit). Checks stub this module-level symbol to
    exercise the refresh offline. include_patches carries the store's own
    retention so a refresh never downgrades a patch-synced store (PR review).
    source_name carries the store's recorded fetch_source (issue #48) —
    NULL/unset keeps the documented graphql default; a store deliberately
    kept on `--source rest` refreshes over REST instead of profile-drifting."""
    try:
        init_schema(conn)
    except RuntimeError as exc:
        # store_too_new / floor: environment, not a sync failure.
        raise _EnvironmentGuard(str(exc)) from exc
    _sync_core(
        conn,
        slug,
        force=False,
        include_patches=include_patches,
        jsonl_path=None,
        numbers=None,
        source_name="graphql" if not source_name else source_name,
        stamp_source=source_name,
    )
    return 0


def _refresh_if_stale(args: argparse.Namespace, db_path: Path, slug: str | None) -> bool:
    """--max-age D: refresh the resolved store first when it is older than D
    seconds (issue #31 AC5).

    The staleness decision is made on a READ-ONLY open (a --max-age read
    never touches a store it does not have to: foreign SQLite files, legacy
    candidates and read-only media behave exactly as without the flag). The
    writable connection is opened only under the sync lock, after the
    under-lock re-check confirms a refresh is still needed. The refresh runs
    with the store's own include_patches retention. On failure the error is
    re-raised as a coded _ReadFailure: the read never answers a stale
    payload after a failed refresh. Without the flag this is a no-op and
    reads never write (AC12). The wait on a contended lock is unbounded by
    design, matching `sync --wait`."""
    budget = getattr(args, "max_age", None)
    if budget is None or slug is None:
        return False
    if not db_path.exists():
        return False
    try:
        ro = open_existing(db_path)
    except RuntimeError as exc:
        raise _ReadFailure(2, str(exc), code_key="environment_guard")
    except sqlite3.DatabaseError as exc:
        # Newer SQLite raises from the first prepared statement on a corrupt
        # file; the read path answers the same store_unreadable envelope.
        raise _ReadFailure(
            1, f"{db_path} is not a zaxbygraph database: {exc}",
            code_key="store_unreadable",
        )
    try:
        try:
            row = ro.execute(
                "SELECT repo FROM sync_state WHERE lower(repo) = ?",
                (slug,),
            )
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                # A valid SQLite file that is not this repo's corpus store:
                # refresh never seeds one; the read path's own guards answer.
                return False
            # A transient storage fault (e.g. a locked database): fail the
            # read rather than silently serving a stale payload.
            raise _ReadFailure(
                1, f"refresh failed: {exc}", code_key="refresh_failed"
            )
        row = row.fetchone()
        if row is None:
            return False
        # The pre-lock query is a store-identity probe only (repo row
        # exists); the refresh's actual configuration is read under the
        # lock below, never from this snapshot (review PRR-020).
        age = _freshness(ro, slug).get("age_s")
        if age is not None and age <= budget:
            return False
        try:
            lock = acquire_sync_lock(db_path, wait=True)
        except OSError as exc:
            raise _ReadFailure(
                1, f"refresh failed: {exc}", code_key="refresh_failed"
            )
        if lock is None:
            raise _ReadFailure(
                1,
                "could not acquire the sync lock for the --max-age refresh"
                " (another zaxbygraph process holds it)",
                code_key="refresh_failed",
            )
        try:
            # Re-check under the lock: another writer may have refreshed
            # while we waited, so this invocation never double-syncs. (The
            # boundary is intentionally approximate to +-1s of the budget:
            # age is second-truncated wall-clock time, re-read here.)
            row = ro.execute(
                "SELECT repo FROM sync_state WHERE lower(repo) = ?", (slug,)
            ).fetchone()
            if row is None:
                return False
            age = _freshness(ro, slug).get("age_s")
            if age is None or age > budget:
                try:
                    conn = connect(db_path)
                except RuntimeError as exc:
                    raise _ReadFailure(2, str(exc), code_key="environment_guard")
                try:
                    # Issue #48 AC2: the refresh configuration is re-read
                    # UNDER the lock, on the writable connection, AFTER
                    # init_schema has migrated it to v11 — a concurrent
                    # writer that changed fetch_source or include_patches
                    # (or migrated a v10 store) while this read waited on
                    # the lock must never be reverted by the waiting
                    # refresh. The pre-lock query is a v10-compatible
                    # store-identity probe only; nothing about the refresh
                    # is decided from it (review PRR-020).
                    try:
                        init_schema(conn)
                    except RuntimeError as exc:
                        raise _EnvironmentGuard(str(exc)) from exc
                    cfg = conn.execute(
                        "SELECT include_patches, fetch_source FROM sync_state"
                        " WHERE lower(repo) = ?",
                        (slug,),
                    ).fetchone()
                    if cfg is None:
                        return False
                    refresh_patches = bool(cfg["include_patches"])
                    raw_source = cfg["fetch_source"]
                    # Out-of-domain stored values (a tampered or foreign
                    # candidate-adopted row) heal to the documented graphql
                    # default instead of silently persisting through the
                    # refresh's re-stamp (review PRR-021). The refresh
                    # always re-stamps the concrete value it honored, so a
                    # profile only ever moves to a value a source actually
                    # fetched with.
                    refresh_source = (
                        raw_source if raw_source in ("graphql", "rest") else "graphql"
                    )
                    _run_refresh_sync(
                        conn,
                        slug,
                        include_patches=refresh_patches,
                        source_name=refresh_source,
                    )
                except _EnvironmentGuard as exc:
                    # Non-retryable environment guards (schema too new,
                    # SQLite floor) exit 2 like the sync path (PR review);
                    # other failures keep the frozen C11 exit-1 contract.
                    raise _ReadFailure(2, str(exc), code_key="environment_guard")
                except Exception as exc:
                    raise _ReadFailure(
                        1,
                        f"refresh failed: {exc}",
                        code_key="refresh_failed",
                    )
                finally:
                    conn.close()
            return True
        finally:
            lock.release_owned()
    except sqlite3.OperationalError as exc:
        # A transient storage fault under the lock (e.g. database locked):
        # fail the read loudly, but do not call a healthy store corrupt.
        raise _ReadFailure(
            1, f"refresh failed: {exc}", code_key="refresh_failed"
        )
    except sqlite3.DatabaseError as exc:
        # A corrupt/non-store file: the same store_unreadable envelope the
        # read path itself will answer.
        raise _ReadFailure(
            1, f"{db_path} is not a zaxbygraph database: {exc}",
            code_key="store_unreadable",
        )
    finally:
        ro.close()

def _open_for_read(args: argparse.Namespace) -> tuple[sqlite3.Connection, str | None, Path]:
    """Resolution + corpus guards for every read command (issue #2 AC3/AC4).

    Returns (readonly connection, repo-or-None, resolved path). Never
    creates a file or directory. Raises _ReadFailure with exit 3 (no corpus
    for the resolved repo: DB missing or empty) or exit 2 (DB holds other
    repos but not this one) or exit 1 (file is not a zaxbygraph database).
    """
    try:
        repo, host = _resolved_repo(args)
    except RepoError as exc:
        raise _ReadFailure(2, f"could not determine repo ({exc}); pass --repo OWNER/REPO")
    explicit = getattr(args, "db", None)
    try:
        db_path, _chain = resolve_db(repo, explicit=explicit, host=host)
    except RepoError as exc:
        raise _ReadFailure(2, str(exc))
    if repo is not None and getattr(args, "max_age", None) is not None:
        # --max-age D (issue #31 AC5): refresh a stale store before reading.
        # Scoped to repo-resolved reads; the no-filter branch below skips it
        # because a sync needs a slug to scope to.
        setattr(args, "_refreshed", _refresh_if_stale(args, db_path, repo))
    if repo is None:
        # Explicit no-filter: open whatever file was named, guards skipped.
        try:
            return open_existing(db_path), None, db_path
        except FileNotFoundError:
            raise _ReadFailure(
                3,
                f"no database at {db_path}; run: zaxbygraph sync --repo OWNER/REPO",
            )
        except RuntimeError as exc:
            # Environment guards (SQLite floor, FTS5 probe, issue #31 AC2)
            # can never succeed on retry: coded bad_request, matching the
            # sync path's exit-2 contract.
            raise _ReadFailure(2, str(exc), code_key="environment_guard")
        except sqlite3.DatabaseError as exc:
            # A corrupt/non-store file raises from the first page-touching
            # statement (now the FTS5 probe): keep the store_unreadable
            # contract wherever the first failing statement sits.
            raise _ReadFailure(
                1, f"no database at {db_path}: {exc}",
                code_key="store_unreadable",
            )
    slug = repo
    try:
        conn = open_existing(db_path)
    except FileNotFoundError:
        raise _ReadFailure(
            3,
            f"no graph for {slug}; resolved database: {db_path}; "
            f"run: zaxbygraph sync --repo {slug}",
        )
    except RuntimeError as exc:
        # Environment guards (SQLite floor, FTS5 probe, issue #31 AC2) can
        # never succeed on retry: coded bad_request, matching the sync
        # path's exit-2 contract.
        raise _ReadFailure(2, str(exc), code_key="environment_guard")
    except sqlite3.DatabaseError as exc:
        # A corrupt/non-store file raises from the first page-touching
        # statement (now the FTS5 probe): keep the store_unreadable
        # contract wherever the first failing statement sits.
        raise _ReadFailure(
            1, f"{db_path} is not a zaxbygraph database: {exc}",
            code_key="store_unreadable",
        )
    try:
        try:
            # Case-fold the guard: a pre-#9 legacy DB stores the slug with
            # user-typed casing. Telling the caller it holds "another repo"
            # when the row IS the resolved repo would be the exact
            # identity-ambiguity defect this issue removes.
            row = conn.execute(
                "SELECT repo FROM sync_state WHERE lower(repo) = ?", (slug,)
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise _ReadFailure(
                1, f"{db_path} is not a zaxbygraph database: {exc}",
                code_key="store_unreadable",
            )
        if row is not None:
            if row["repo"] != slug:
                raise _ReadFailure(
                    2,
                    f"no corpus for {slug} in {db_path}; this database holds "
                    f"{row['repo']} - the same repo with pre-fold casing; "
                    f"run: zaxbygraph doctor --consolidate --repo {slug} to adopt it "
                    f"(or zaxbygraph sync --repo {slug} to rebuild)",
                )
            # The no-corpus gate (issue #72 C10 / plan CF-E): a sync_state
            # row alone is not a corpus. Zero LIVE items AND no completed
            # full sync (the store a FAILED first sync leaves) is exit-3
            # no_corpus on every read — decided by the shared corpus_state
            # helper, never the lagging item_count snapshot.
            live_items, last_full_sync_at = corpus_state(conn, slug)
            if live_items == 0 and last_full_sync_at is None:
                raise _ReadFailure(
                    3,
                    f"no graph for {slug}; resolved database: {db_path}; "
                    f"run: zaxbygraph sync --repo {slug}",
                )
        else:
            others = [
                r["repo"] for r in conn.execute("SELECT repo FROM sync_state ORDER BY repo")
            ]
            if others:
                raise _ReadFailure(
                    2,
                    f"no corpus for {slug} in {db_path}; this database holds: "
                    + ", ".join(others),
                )
            raise _ReadFailure(
                3,
                f"no graph for {slug}; resolved database: {db_path}; "
                f"run: zaxbygraph sync --repo {slug}",
            )
    except BaseException:
        conn.close()
        raise
    return conn, slug, db_path


def _run_read(args: argparse.Namespace, query_fn, *extra) -> int:
    repo: str | None = None
    db_path: Path | None = None
    try:
        conn, repo, db_path = _open_for_read(args)
    except _ReadFailure as exc:
        # Pre-open failures still name what was requested: fall back to the
        # caller's --repo/--db so the envelope is not null-blind (4.5 review).
        return emit_failure(
            args,
            exc.code,
            exc.message,
            slug=repo or getattr(args, "repo", None) or None,
            db_path=db_path or getattr(args, "db", None),
            code_key=exc.code_key,
        )
    try:
        data = query_fn(conn, *extra, repo=repo)
        fresh = _freshness(conn, repo)
        line = _identity_line(conn, repo, db_path)
    except LookupError as exc:
        # A ghost input (issue #72 C11): an unstored item number answers
        # the coded not_found envelope, exit 1 — the same mapping the
        # derived and product runners already apply.
        return emit_failure(
            args,
            1,
            str(exc),
            slug=repo,
            db_path=db_path,
            code_key="not_found",
        )
    except ValueError as exc:
        # A caller-supplied row limit outside the driver's range is a
        # malformed request, not a store fault, and is mapped exactly as
        # _run_read_promote_truncated maps it so every read command answers
        # one way (issue #55). The arm is deliberately narrow: an INTERNAL
        # ValueError from the query layer is a different fault and must not
        # be reported as a caller error with a non-retryable exit code.
        if not _LIMIT_RE.match(str(exc)):
            raise
        return emit_failure(
            args,
            2,
            str(exc),
            slug=repo,
            db_path=db_path,
            code_key="bad_request",
        )
    except sqlite3.Error as exc:
        # A store that cannot be read mid-query (a pre-v7 store hitting a
        # v7-only table, a corrupt page, a read-only directory blocking the
        # -shm) is a coded store_unreadable, matching the derived-read
        # runner — never a raw traceback (pr45 review K01).
        return emit_failure(
            args,
            1,
            f"store unreadable: {exc}",
            slug=repo,
            db_path=db_path,
            code_key="store_unreadable",
        )
    finally:
        conn.close()
    emit_result(
        args, data, slug=repo, db_path=db_path, freshness=fresh, identity_line=line
    )
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    numbers: list[int] | None = None
    numbers_raw = getattr(args, "numbers", None)
    if numbers_raw is not None:
        try:
            numbers = [int(x) for x in str(numbers_raw).replace(" ", "").split(",") if x != ""]
        except ValueError:
            return emit_failure(args, 2, f"invalid --numbers value: {numbers_raw!r}")
        if not numbers:
            return emit_failure(args, 2, f"invalid --numbers value: {numbers_raw!r}")
        if getattr(args, "force", False):
            return emit_failure(args, 2, "--numbers cannot be combined with --force")
    try:
        if args.repo:
            slug = validate_slug(args.repo)
            host = DEFAULT_HOST
        else:
            host, slug = remote_info()
    except RepoError as exc:
        return emit_failure(args, 2, str(exc), slug=None)
    db_path, _chain = resolve_db(slug, explicit=args.db, host=host)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(db_path)
        init_schema(conn)
    except RuntimeError as exc:
        # Environment guards (SQLite floor, database newer than this build)
        # can never succeed on retry — README's exit-2 contract.
        if conn is not None:
            conn.close()
        return emit_failure(args, 2, str(exc), slug=slug, db_path=db_path)
    except Exception as exc:
        # A storage fault before the sync starts is reported as the same
        # result object, not a traceback (AGENTS.md: reported, not swallowed).
        if conn is not None:
            conn.close()
        emit_result(
            args,
            None,
            slug=slug,
            db_path=db_path,
            error=_error_for("runtime", str(exc)),
            identity=False,
        )
        return 1
    lock: SyncLock | None = None
    try:
        lock = acquire_sync_lock(db_path, wait=bool(getattr(args, "wait", False)))
    except OSError as exc:
        # A lock-file storage fault (unreadable directory, permissions) is a
        # failed sync, reported as the same result object - never a traceback.
        conn.close()
        emit_result(
            args,
            None,
            slug=slug,
            db_path=db_path,
            error=_error_for("runtime", str(exc)),
            identity=False,
        )
        return 1
    if lock is None:
        fresh = _freshness(conn, slug)
        conn.close()
        emit_result(
            args,
            {"joined": True},
            slug=slug,
            db_path=db_path,
            freshness=fresh,
            identity=False,
        )
        return 0
    jsonl: Path | None = None
    if args.jsonl:
        jsonl = Path(args.jsonl)
    elif args.jsonl_flag:
        jsonl = default_jsonl_dir(db_path)
    try:
        # Profile inheritance (issue #65 AC4): a run WITHOUT --source or
        # --include-patches uses the store's recorded profile, read UNDER
        # the sync lock so a concurrent writer cannot flip it mid-run
        # (the PR #47 review race). An explicit flag still wins, and only
        # an explicitly stated source is forwarded as the stamp (issue
        # #48: an incidental default-source sync never rewrites the
        # store's recorded profile). The default CONSTRUCTION source is
        # the GraphQL bulk source (issue #5); `--source rest` keeps the
        # plain REST source (now page-streaming). The call goes through
        # the shared quiet core (issue #31) so the --max-age refresh path
        # issues exactly the same one sync_repo call.
        stored = conn.execute(
            "SELECT include_patches, fetch_source FROM sync_state WHERE repo = ?",
            (slug,),
        ).fetchone()
        stored_patches = bool(stored["include_patches"]) if stored else False
        stored_source = (
            stored["fetch_source"] if stored and stored["fetch_source"] else None
        ) or "graphql"
        effective_patches = (
            args.include_patches if args.include_patches else stored_patches
        )
        effective_source = getattr(args, "source", None) or stored_source
        result = _sync_core(
            conn,
            slug,
            force=args.force,
            include_patches=effective_patches,
            jsonl_path=jsonl,
            numbers=numbers,
            source_name=effective_source,
            stamp_source=getattr(args, "source", None),
        )
    except SyncError as exc:
        emit_result(
            args,
            None,
            conn=conn,
            slug=slug,
            db_path=db_path,
            error=_error_for("runtime", str(exc)),
            identity=False,
        )
        return 1
    except Exception as exc:  # recorded by sync_repo; reported, not a traceback
        emit_result(
            args,
            None,
            conn=conn,
            slug=slug,
            db_path=db_path,
            error=_error_for("runtime", str(exc)),
            identity=False,
        )
        return 1
    finally:
        lock.release_owned()
        fresh = _freshness(conn, slug)
        conn.close()
    data = {k: v for k, v in result.items() if k not in ("repo", "db")}
    emit_result(
        args,
        data,
        slug=slug,
        db_path=db_path,
        freshness=fresh,
        identity=False,
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    return _run_read(args, status)


def cmd_search(args: argparse.Namespace) -> int:
    # Issue #72 C14: --offset pages the deduplicated strict-first stream and
    # the payload's limit-independent `truncated` (page < matched) is
    # promoted to the envelope; --no-bots (issue #71 C6) filters bot-authored
    # item/comment/review matches out of the SQL, pre-ranking.
    offset = max(0, int(getattr(args, "offset", 0) or 0))
    exclude_bots = bool(getattr(args, "no_bots", False))

    def _query(conn: sqlite3.Connection, repo: str | None):
        return search(
            conn,
            args.query,
            args.limit,
            repo=repo,
            offset=offset,
            exclude_bots=exclude_bots,
        )

    return _run_read_promote_truncated(args, _query)


def _parse_as_of(value: str) -> datetime:
    """Validate a user-supplied --as-of timestamp (issue #25). Delegates to
    the query layer's parser so CLI and read semantics cannot diverge; naive
    and malformed input raise ValueError, which cmd_item turns into a coded
    error rather than a traceback."""
    from zaxbygraph.query import _parse_instant

    return _parse_instant(value)


def _item_not_found_row(number: int) -> dict:
    """The per-row not_found entry a multi-number item call answers for an
    unstored number (issue #72 C22): the reserved `not_found: true` marker
    (which survives --fields projection) plus the coded error object."""
    return {
        "number": number,
        "not_found": True,
        "error": {"code": "not_found", "message": f"item {number} is not stored"},
    }


def cmd_item(args: argparse.Namespace) -> int:
    repo: str | None = None
    db_path: Path | None = None
    try:
        conn, repo, db_path = _open_for_read(args)
    except _ReadFailure as exc:
        # Pre-open failures still name what was requested: fall back to the
        # caller's --repo/--db so the envelope is not null-blind (4.5 review).
        return emit_failure(
            args,
            exc.code,
            exc.message,
            slug=repo or getattr(args, "repo", None) or None,
            db_path=db_path or getattr(args, "db", None),
            code_key=exc.code_key,
        )
    try:
        at: datetime | None = None
        if getattr(args, "as_of", None) is not None and getattr(args, "history", False):
            conn.close()
            return emit_failure(
                args,
                2,
                "--history and --as-of are mutually exclusive",
                slug=repo,
                db_path=db_path,
                code_key="bad_flags",
            )
        if getattr(args, "as_of", None) is not None:
            try:
                at = _parse_as_of(args.as_of)
            except ValueError as exc:
                conn.close()
                return emit_failure(
                    args,
                    1,
                    f"invalid --as-of value: {exc}",
                    slug=repo,
                    db_path=db_path,
                    code_key="bad_as_of",
                )
        numbers = list(getattr(args, "number") or [])
        exclude_bots = bool(getattr(args, "no_bots", False))
        if len(numbers) <= 1:
            # Single number: the historical object payload, byte-identical.
            number = numbers[0] if numbers else None
            data = item(conn, number, repo=repo, exclude_bots=exclude_bots)
            if data is not None:
                if at is not None:
                    data = item_as_of(conn, number, at, repo=repo, exclude_bots=exclude_bots)
                elif getattr(args, "history", False):
                    data = item_history(conn, number, repo=repo, exclude_bots=exclude_bots)
            if data is not None:
                # --max-body-chars keeps its explicit semantics (issue #25
                # AC6); the default bounds (issue #71 C5: body <= 20 KiB with
                # a marker, comments/reviews <= 5, omission counts, envelope
                # truncated on any cut) apply wherever a flag is absent.
                truncated = _truncate_bodies(data, args.max_body_chars)
                truncated = apply_item_bounds(
                    data,
                    repo,
                    max_body_chars=args.max_body_chars,
                    max_comments=getattr(args, "max_comments", None),
                ) or truncated
                fresh = _freshness(conn, repo)
                line = _identity_line(conn, repo, db_path)
        else:
            # Multi-number (issue #72 C22): a BARE LIST of per-number rows in
            # input order — unstored numbers answer per-row not_found entries
            # instead of failing the call (all-missing included), and every
            # row gets the same --max-comments/--max-body-chars treatment.
            rows: list[dict] = []
            truncated = False
            for number in numbers:
                row = item(conn, number, repo=repo, exclude_bots=exclude_bots)
                if row is not None and at is not None:
                    row = item_as_of(conn, number, at, repo=repo, exclude_bots=exclude_bots)
                elif row is not None and getattr(args, "history", False):
                    row = item_history(conn, number, repo=repo, exclude_bots=exclude_bots)
                if row is None:
                    rows.append(_item_not_found_row(number))
                    continue
                row_cut = _truncate_bodies(row, args.max_body_chars)
                row_cut = apply_item_bounds(
                    row,
                    repo,
                    max_body_chars=args.max_body_chars,
                    max_comments=getattr(args, "max_comments", None),
                ) or row_cut
                truncated = truncated or row_cut
                rows.append(row)
            data = rows
            fresh = _freshness(conn, repo)
            line = _identity_line(conn, repo, db_path)
    finally:
        conn.close()
    if data is None:
        return emit_failure(
            args,
            1,
            f"item #{numbers[0]} not found",
            slug=repo,
            db_path=db_path,
            code_key="not_found",
        )
    emit_result(
        args,
        data,
        slug=repo,
        db_path=db_path,
        truncated=truncated,
        freshness=fresh,
        identity_line=line,
    )
    return 0


def _truncate_bodies(data: dict, limit: int) -> bool:
    """AC6: cut item/comment/review bodies to --max-body-chars and mark each
    truncated object `truncated: true`. Returns whether anything was cut.
    Revision history content (--history) truncates the same way (issue #25)."""
    if limit is None or limit <= 0:
        return False
    cut = False
    body = data.get("body")
    if isinstance(body, str) and len(body) > limit:
        data["body"] = body[:limit]
        data["truncated"] = True
        cut = True
    for key in ("comments", "reviews"):
        for obj in data.get(key) or []:
            if not isinstance(obj, dict):
                continue
            text = obj.get("body")
            if isinstance(text, str) and len(text) > limit:
                obj["body"] = text[:limit]
                obj["truncated"] = True
                cut = True
    for rev in data.get("revisions") or []:
        if not isinstance(rev, dict):
            continue
        text = rev.get("content")
        if isinstance(text, str) and len(text) > limit:
            rev["content"] = text[:limit]
            rev["truncated"] = True
            cut = True
    return cut


def cmd_related(args: argparse.Namespace) -> int:
    # Issue #71 C7 / plan CF-D: the bounded expansion reports its own
    # `truncated` data flag; promote it to the envelope like file-history.
    return _run_read_promote_truncated(args, related, args.number, args.depth)


def cmd_churn(args: argparse.Namespace) -> int:
    # churn/open_items are keyword-only after their positional page window
    # (pr45 review K03), so the flags ride a closure instead of _run_read's
    # positional *extra.
    limit = args.limit
    merged_only = bool(getattr(args, "merged_only", False))

    def _query(conn: sqlite3.Connection, repo: str | None):
        return churn(conn, limit, merged_only=merged_only, repo=repo)

    return _run_read(args, _query)


def cmd_open(args: argparse.Namespace) -> int:
    limit = getattr(args, "limit", None)
    offset = getattr(args, "offset", 0)
    kind = getattr(args, "kind", None)
    label = getattr(args, "label", None)

    def _query(conn: sqlite3.Connection, repo: str | None):
        return open_items(
            conn, limit, offset, kind=kind, label=label, repo=repo
        )

    return _run_read(args, _query)


def cmd_path(args: argparse.Namespace) -> int:
    return _run_read(args, path_between, args.a, args.b)


def _run_derived_read(args: argparse.Namespace, query_fn, *extra) -> int:
    """Derived-tier read runner: same resolution and envelope as
    ``_run_read``, over the same ``mode=ro`` connection ``open_existing``
    already provides (temp objects work there; main-db writes are
    impossible). Query-time failures map to coded envelopes, never
    tracebacks: unknown items to ``not_found``, weight-configuration
    problems to ``config_error`` and other bad requests to exit 2,
    store-level failures to ``store_unreadable``; every command routed
    here refuses an unresolved repository with the canonical
    ``repo_required`` code instead of answering from an empty filter
    (pr43 review PRR-014/016)."""
    repo: str | None = None
    db_path: Path | None = None
    try:
        conn, repo, db_path = _open_for_read(args)
    except _ReadFailure as exc:
        return emit_failure(
            args,
            exc.code,
            exc.message,
            slug=repo or getattr(args, "repo", None) or None,
            db_path=db_path or getattr(args, "db", None),
            code_key=exc.code_key,
        )
    if not repo:
        conn.close()
        return emit_failure(
            args,
            2,
            "repo_required: the derived tier needs a resolved repository"
            " (pass --repo, or sync the store so its repo can be detected)",
            slug=getattr(args, "repo", None) or None,
            db_path=db_path or getattr(args, "db", None),
            code_key="repo_required",
        )
    try:
        data = query_fn(conn, *extra, repo=repo, db_path=db_path)
        fresh = _freshness(conn, repo)
        line = _identity_line(conn, repo, db_path)
    except LookupError as exc:
        return emit_failure(
            args, 1, str(exc), slug=repo, db_path=db_path, code_key="not_found"
        )
    except derive.ConfigError as exc:
        message = str(exc) + (f" (config file: {exc.path})" if exc.path else "")
        return emit_failure(
            args, 2, message, slug=repo, db_path=db_path, code_key="config_error"
        )
    except ValueError as exc:
        return emit_failure(args, 2, str(exc), slug=repo, db_path=db_path)
    except sqlite3.Error as exc:
        # A store that cannot be read (corrupt file, or a read-only
        # directory blocking the -shm) is a store_unreadable, distinct from
        # a generic runtime error: the envelope names the actionable cause.
        return emit_failure(
            args,
            1,
            f"store unreadable: {exc}",
            slug=repo,
            db_path=db_path,
            code_key="store_unreadable",
        )
    finally:
        conn.close()
    emit_result(
        args, data, slug=repo, db_path=db_path, freshness=fresh, identity_line=line
    )
    return 0


def _resolve_weight_config(
    db_path: Path | None, explicit: str | None
) -> tuple[dict | None, str | None, str | None]:
    """Weight-config discovery per the plan's precedence chain: defaults,
    then ``<store>/config.toml``, then ``.zaxbygraph.toml`` (cwd), then an
    explicit ``--config`` path (highest). Returns the paths that exist."""
    store_cfg: str | None = None
    if db_path is not None:
        store_candidate = db_path.parent / "config.toml"
        if store_candidate.is_file():
            store_cfg = str(store_candidate)
    cwd_cfg: str | None = None
    candidate = Path(".zaxbygraph.toml")
    if candidate.is_file():
        cwd_cfg = str(candidate)
    return store_cfg, cwd_cfg, explicit


def _git_ls_files() -> tuple[list[str] | None, str | None]:
    """Opt-in ``git ls-files`` of the invoking checkout plus its HEAD sha.
    Degrades to ``(None, None)`` outside a git checkout — the response then
    carries ``ls_files_applied: false``.

    ``--full-name -- :/`` is load-bearing: plain ``git ls-files`` prints
    cwd-relative names for the cwd's subtree only, so from any subdirectory
    the intersection with the store's root-relative vocabulary would drop
    every path while the envelope still claimed ``ls_files_applied: true``
    (pr43 review PRR-002). ``-z`` returns NUL-delimited, never-quoted raw
    names — newline/quote parsing would drop any path git would otherwise
    C-quote even with ``quotePath`` off (names containing ``"`` or control
    characters), and ``splitlines()`` would also break on Unicode
    separators (cubic run-3). ``core.quotePath=false`` is belt-and-braces
    for older gits, and ``core.fsmonitor=false`` keeps a hostile checkout's
    fsmonitor hook out of a read-only listing.
    """
    import subprocess

    try:
        files = subprocess.run(
            [
                "git",
                "-c",
                "core.quotePath=false",
                "-c",
                "core.fsmonitor=false",
                "ls-files",
                "-z",
                "--full-name",
                "--",
                ":/",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        if files.returncode != 0:
            return None, None
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return (
            [line for line in files.stdout.split("\0") if line],
            head.stdout.strip() if head.returncode == 0 else None,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None


def _similar_query(
    conn: sqlite3.Connection,
    number: int,
    limit: int,
    config: str | None,
    ls_files_flag: bool,
    repo: str,
    db_path: Path | None = None,
) -> dict:
    from zaxbygraph import derive as _derive

    store_cfg, cwd_cfg, explicit = _resolve_weight_config(db_path, config)
    weights = _derive.load_weights(store_cfg, cwd_cfg, explicit)
    ls_files: list[str] | None = None
    ls_head: str | None = None
    applied = False
    if ls_files_flag:
        ls_files, ls_head = _git_ls_files()
        applied = ls_files is not None
    out = _derive.similar(
        conn,
        repo,
        number,
        limit=_clamp_limit(limit),
        weights=weights,
        ls_files=ls_files,
    )
    out["ls_files_applied"] = applied
    out["ls_files_head"] = ls_head
    return out


def _explain_query(
    conn: sqlite3.Connection,
    a: int,
    b: int,
    config: str | None,
    ls_files_flag: bool,
    repo: str,
    db_path: Path | None = None,
) -> dict:
    from zaxbygraph import derive as _derive

    store_cfg, cwd_cfg, explicit = _resolve_weight_config(db_path, config)
    weights = _derive.load_weights(store_cfg, cwd_cfg, explicit)
    ls_files: list[str] | None = None
    ls_head: str | None = None
    applied = False
    if ls_files_flag:
        ls_files, ls_head = _git_ls_files()
        applied = ls_files is not None
    out = _derive.explain(
        conn, repo, a, b, weights=weights, ls_files=ls_files
    )
    out["ls_files_applied"] = applied
    out["ls_files_head"] = ls_head
    return out


def _coupling_query(
    conn: sqlite3.Connection,
    path: str,
    limit: int,
    repo: str,
    db_path: Path | None = None,
) -> dict:
    from zaxbygraph import derive as _derive

    # coupling uses no weight config, so db_path is accepted for runner
    # signature uniformity and unused.
    return _derive.coupling(conn, repo, path, limit=_clamp_limit(limit))


def _eval_query(
    conn: sqlite3.Connection,
    mode: str,
    config: str | None,
    repo: str,
    db_path: Path | None = None,
) -> dict:
    from zaxbygraph import derive as _derive, evaluate as _evaluate

    store_cfg, cwd_cfg, explicit = _resolve_weight_config(db_path, config)
    # Always resolved (defaults when no config source exists) so the eval
    # envelope carries the EFFECTIVE weights, the same contract similar and
    # explain honour (pr43 review PRR-021).
    weights = _derive.load_weights(store_cfg, cwd_cfg, explicit)
    return _evaluate.run_eval(conn, repo, mode, weights=weights)


def cmd_similar(args: argparse.Namespace) -> int:
    return _run_derived_read(
        args, _similar_query, args.number, args.limit, args.config, args.ls_files
    )


def cmd_explain(args: argparse.Namespace) -> int:
    return _run_derived_read(
        args, _explain_query, args.a, args.b, args.config, args.ls_files
    )


def cmd_coupling(args: argparse.Namespace) -> int:
    return _run_derived_read(args, _coupling_query, args.path, args.limit)


def cmd_eval(args: argparse.Namespace) -> int:
    return _run_derived_read(args, _eval_query, args.mode, args.config)


def _triage_query(
    conn: sqlite3.Connection,
    limit: int,
    cursor: str | None,
    top_k: int | None,
    include_gated: bool,
    config: str | None,
    repo: str,
    db_path: Path | None = None,
) -> dict:
    from zaxbygraph import derive as _derive

    # The [derive] weights ride the same store/cwd/explicit chain as
    # similar's --config: the stage-2 analog scan must score under the
    # caller's effective table, not silently the defaults (pr45 K05).
    store_cfg, cwd_cfg, explicit = _resolve_weight_config(db_path, config)
    weights = _derive.load_weights(store_cfg, cwd_cfg, explicit)
    return _derive.triage(
        conn,
        repo,
        limit=limit,
        cursor=cursor,
        top_k=top_k,
        include_gated=include_gated,
        config=config,
        weights=weights,
    )


def _bundle_query(
    conn: sqlite3.Connection,
    size: str,
    count: int,
    pool_raw: str | None,
    exclude_raw: str | None,
    allow_shared_files: bool,
    max_files: int | None,
    budget_tokens: int | None,
    config: str | None,
    repo: str,
    db_path: Path | None = None,
) -> dict:
    from zaxbygraph import derive as _derive

    # Parsed INSIDE the derived-read runner's protected try (an invalid
    # --pool/--exclude value is a bad_request envelope at exit 2, never a
    # traceback — the same contract --size already has through _parse_size).
    pool = _csv_numbers(pool_raw, "--pool")
    exclude = _csv_numbers(exclude_raw, "--exclude")
    store_cfg, cwd_cfg, explicit = _resolve_weight_config(db_path, config)
    weights = _derive.load_weights(store_cfg, cwd_cfg, explicit)
    return _derive.bundle(
        conn,
        repo,
        size=size,
        count=count,
        pool=pool,
        exclude=exclude,
        allow_shared_files=allow_shared_files,
        max_files=max_files,
        config=config,
        budget_tokens=budget_tokens,
        weights=weights,
    )


def _brief_query(
    conn: sqlite3.Connection,
    number: int,
    budget_tokens: int,
    live: bool,
    exclude_bots: bool,
    config: str | None,
    repo: str,
    db_path: Path | None = None,
) -> dict:
    from zaxbygraph import derive as _derive

    store_cfg, cwd_cfg, explicit = _resolve_weight_config(db_path, config)
    weights = _derive.load_weights(store_cfg, cwd_cfg, explicit)
    return _derive.brief(
        conn,
        repo,
        number,
        budget_tokens=budget_tokens,
        config=config,
        live=live,
        weights=weights,
        exclude_bots=exclude_bots,
    )


def _csv_numbers(raw: str | None, flag: str) -> list[int] | None:
    """Parse a comma-separated number list. A BLANK value is treated as
    absent (None -> the default pool), so `--pool ""` cannot silently mean
    "an explicit empty pool" (pr45 review CUB#b08069ad)."""
    if raw is None or not str(raw).strip():
        return None
    out: list[int] = []
    for piece in str(raw).replace(" ", "").split(","):
        if not piece:
            continue
        try:
            out.append(int(piece))
        except ValueError:
            raise ValueError(f"invalid {flag} value: {raw!r}") from None
    return out


def cmd_triage(args: argparse.Namespace) -> int:
    return _run_derived_read(
        args,
        _triage_query,
        args.limit,
        args.cursor,
        args.top_k,
        bool(getattr(args, "include_gated", False)),
        args.config,
    )


def cmd_bundle(args: argparse.Namespace) -> int:
    # --pool/--exclude travel as RAW strings: they are parsed inside
    # _bundle_query, within _run_derived_read's protected try, so invalid
    # values answer the coded bad_request envelope instead of a traceback.
    return _run_derived_read(
        args,
        _bundle_query,
        args.size,
        args.count,
        args.pool,
        args.exclude,
        bool(getattr(args, "allow_shared_files", False)),
        args.max_files,
        args.budget_tokens,
        args.config,
    )


def cmd_brief(args: argparse.Namespace) -> int:
    return _run_derived_read(
        args,
        _brief_query,
        args.number,
        args.budget_tokens,
        bool(getattr(args, "live", False)),
        bool(getattr(args, "no_bots", False)),
        args.config,
    )


def _run_read_promote_truncated(
    args: argparse.Namespace, query_fn, *extra
) -> int:
    """_run_read for the product queries (issue #7): a data-level
    `truncated` key (file_history's over-fetch evidence) is promoted to
    the envelope's truncated flag instead of hiding inside data."""
    repo: str | None = None
    db_path: Path | None = None
    try:
        conn, repo, db_path = _open_for_read(args)
    except _ReadFailure as exc:
        return emit_failure(
            args,
            exc.code,
            exc.message,
            slug=repo or getattr(args, "repo", None) or None,
            db_path=db_path or getattr(args, "db", None),
            code_key=exc.code_key,
        )
    try:
        data = query_fn(conn, *extra, repo=repo)
        truncated = False
        if isinstance(data, dict) and "truncated" in data:
            truncated = bool(data.pop("truncated"))
        fresh = _freshness(conn, repo)
        line = _identity_line(conn, repo, db_path)
    except _UnknownPathError as exc:
        # file-history's ghost path (issue #72 C11): unknown_path, exit 1,
        # with the suggestion hint.
        return emit_failure(
            args,
            1,
            exc.message,
            slug=repo,
            db_path=db_path,
            code_key="unknown_path",
            hint=exc.hint,
        )
    except LookupError as exc:
        return emit_failure(
            args,
            1,
            str(exc),
            slug=repo,
            db_path=db_path,
            code_key="not_found",
        )
    except ValueError as exc:
        # The product queries are number-keyed: a no-filter scan would
        # collapse distinct same-numbered items across repos (review F8).
        return emit_failure(
            args,
            2,
            str(exc),
            slug=repo,
            db_path=db_path,
            code_key="bad_request",
        )
    finally:
        conn.close()
    emit_result(
        args,
        data,
        slug=repo,
        db_path=db_path,
        truncated=truncated,
        freshness=fresh,
        identity_line=line,
    )
    return 0


def cmd_overlap(args: argparse.Namespace) -> int:
    if len(args.numbers) < 2:
        return emit_failure(
            args,
            2,
            "overlap needs at least two numbers to form a pair",
            slug=getattr(args, "repo", None) or None,
            code_key="bad_request",
        )
    return _run_read_promote_truncated(args, pr_overlap, args.numbers)


def _file_history_suggestions(normalized: str, stored: list[str]) -> list[str]:
    """Up to 5 stored paths that look like `normalized` (issue #72 C11):
    equal case-insensitively, or sharing the basename. Pure string
    comparison over the stored vocabulary — nothing is resolved on disk."""
    norm_lower = normalized.lower()
    base = normalized.rsplit("/", 1)[-1].lower()
    hits = sorted(
        p
        for p in stored
        if p.lower() == norm_lower or p.rsplit("/", 1)[-1].lower() == base
    )
    return hits[:5]


def cmd_file_history(args: argparse.Namespace) -> int:
    raw_path = str(getattr(args, "path", ""))

    def _query(conn: sqlite3.Connection, repo: str | None):
        if not repo:
            # The no-filter branch keeps the query layer's own guard (a
            # repo is required) and its existing bad_request mapping.
            return file_history(conn, raw_path, args.limit, repo=repo)
        # C11: backslashes normalize, then the lookup is EXACT-CASE against
        # the stored pr_files vocabulary. A miss answers unknown_path with
        # suggestions — never a silent case-fold, never a disk resolve.
        normalized = raw_path.replace("\\", "/")
        stored = sorted(
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT path FROM pr_files WHERE repo = ?", (repo,)
            )
        )
        if normalized not in set(stored):
            suggestions = _file_history_suggestions(normalized, stored)
            hint = (
                "stored paths that look similar: " + ", ".join(suggestions)
                if suggestions
                else "no stored path looks similar; churn or"
                " `sql \"SELECT DISTINCT path FROM pr_files\"` lists the"
                " stored vocabulary"
            )
            raise _UnknownPathError(f"unknown path: {normalized}", hint)
        return file_history(conn, normalized, args.limit, repo=repo)

    return _run_read_promote_truncated(args, _query)


def cmd_what_closed(args: argparse.Namespace) -> int:
    return _run_read_promote_truncated(args, what_closed, args.number)


def cmd_mcp(args: argparse.Namespace) -> int:
    # Deferred import: mcp_server imports this module's helpers, so the
    # dependency only ever points one way at import time.
    from zaxbygraph.mcp_server import serve

    try:
        # MCP stdio frames are LF-delimited; a Windows pipe would
        # otherwise translate every frame's \n to \r\n.
        sys.stdout.reconfigure(newline="")
    except (ValueError, OSError, AttributeError):
        pass
    serve(
        sys.stdin,
        sys.stdout,
        stale_after_s=args.stale_seconds,
        repo=getattr(args, "repo", None) or None,
        db=getattr(args, "db", None) or None,
    )
    return 0


_NO_SUCH_RE = re.compile(r"^(no such column|no such table)", re.IGNORECASE)
_TABLE_RE = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
# The row-limit refusal raised by query._clamp_fetch_limit / _clamp_limit. A
# caller-supplied limit outside the driver's range is a malformed request, so
# it must be classified like every other guard refusal (bad_request / exit 2)
# rather than as a retryable runtime fault (issue #55).
_LIMIT_RE = re.compile(r"^row limit .+ exceeds the maximum of", re.IGNORECASE)


def _hint_table(sql_text: str) -> str | None:
    """First FROM/JOIN table in CODE spans only (PRR-006): a plain regex
    over raw SQL matches the words inside string literals and comments and
    mis-targets the hint. query._sql_tokens already classifies spans, so
    scan only its 'code' output."""
    from zaxbygraph.query import _sql_tokens

    code = " ".join(text for kind, text in _sql_tokens(sql_text) if kind == "code")
    match = _TABLE_RE.search(code)
    if match is None:
        return None
    return match.group(1)


def _column_hint(conn: sqlite3.Connection, sql_text: str) -> str | None:
    """Real column names for the first FROM/JOIN table (issue #3 AC1).

    PRAGMA-free by design: the hint probe is a plain `SELECT * FROM <token>
    LIMIT 0` on the already-open authorizer-guarded connection, read through
    cursor.description — no path through the `sql` subcommand ever issues a
    PRAGMA (AGENTS.md escape-hatch invariant). The token is identifier-
    charset-only so it cannot break out of the SELECT. Any probe failure
    (missing table, CTE names) omits the hint; the envelope still emits.
    """
    token = _hint_table(sql_text)
    if token is None:
        return None
    try:
        probe = conn.execute(f"SELECT * FROM {token} LIMIT 0")
        cols = [d[0] for d in probe.description or []]
    except sqlite3.Error:
        return None
    if not cols:
        return None
    return f"columns of {token}: " + ", ".join(cols)


#: Hint for the first-keyword/PRAGMA refusal (issue #71 AC11): a rejected
#: PRAGMA or INSERT must point at the read contract and the schema command.
_SQL_GUARD_HINT = (
    "the sql subcommand runs ONE read-only SELECT/WITH/EXPLAIN statement; "
    "see `zaxbygraph schema [TABLE]` for the store's tables and columns"
)

#: Hint for a deadline expiry (issue #71 AC1): the error is coded `timeout`
#: (never `runtime`), so the fix is narrowing the statement or widening the
#: env budget — not retrying the identical call.
_SQL_TIMEOUT_HINT = (
    "narrow the statement (LIMIT, tighter predicates) or raise "
    "ZAXBYGRAPH_SQL_TIMEOUT_S (clamped to (0, 600])"
)


def cmd_sql(args: argparse.Namespace) -> int:
    # Write rejection comes FIRST: the read-only contract is about the
    # statement, and it must keep its exit 2 before any DB access.
    try:
        assert_read_sql(args.statement)
    except ValueError as exc:
        return emit_failure(
            args,
            2,
            str(exc),
            slug=getattr(args, "repo", None) or None,
            db_path=getattr(args, "db", None),
            code_key="bad_sql",
            hint=_SQL_GUARD_HINT,
        )
    repo: str | None = None
    db_path: Path | None = None
    try:
        conn, repo, db_path = _open_for_read(args)
    except _ReadFailure as exc:
        # Pre-open failures still name what was requested: fall back to the
        # caller's --repo/--db so the envelope is not null-blind (4.5 review).
        return emit_failure(
            args,
            exc.code,
            exc.message,
            slug=repo or getattr(args, "repo", None) or None,
            db_path=db_path or getattr(args, "db", None),
            code_key=exc.code_key,
        )
    conn.close()
    try:
        ro_conn = connect_readonly_query(db_path)
    except (ValueError, FileNotFoundError) as exc:
        code = 3 if isinstance(exc, FileNotFoundError) else 2
        return emit_failure(args, code, str(exc), slug=repo, db_path=db_path)
    except RuntimeError as exc:
        # Environment guards (SQLite floor, FTS5 probe, issue #31 AC2) on the
        # sql command's second connection surface as coded bad_request like
        # every other read, never a traceback.
        return emit_failure(
            args, 2, str(exc), slug=repo, db_path=db_path,
            code_key="environment_guard",
        )
    except sqlite3.Error as exc:
        # A corrupt/non-store file raises from the first page-touching
        # statement: the store_unreadable envelope, never a traceback.
        return emit_failure(
            args, 1, f"store unreadable: {exc}", slug=repo,
            db_path=db_path, code_key="store_unreadable",
        )
    try:
        try:
            data = run_sql(
                ro_conn,
                args.statement,
                limit=args.limit,
                repo=repo,
                deadline=sql_deadline_from_env(),
                max_bytes=sql_max_bytes_from_env(),
            )
        except SqlTimeoutError as exc:
            # A deadline expiry is its own coded failure (issue #71 AC1):
            # `timeout`, exit 1 — retrying the identical statement cannot
            # succeed, but it is not a bad_request either.
            emit_result(
                args,
                None,
                conn=ro_conn,
                slug=repo,
                db_path=db_path,
                error=_error_for("timeout", str(exc), _SQL_TIMEOUT_HINT),
                identity=False,
            )
            return 1
        except ValueError as exc:
            message = str(exc)
            match = _NO_SUCH_RE.match(message)
            if match is not None:
                code_key = match.group(1).lower().replace(" ", "_")
                if code_key == "no_such_table":
                    # Issue #71 AC11: a missing table gets the store's real
                    # table list (PRAGMA-free, from sqlite_master) — the
                    # per-column probe cannot know a table's columns when
                    # the table itself does not exist.
                    hint = store_tables_hint(ro_conn)
                else:
                    hint = _column_hint(ro_conn, args.statement)
            elif _LIMIT_RE.match(message):
                # A caller-supplied row limit outside the driver's range is a
                # malformed request, not an internal fault: `file-history`
                # reports the identical refusal as bad_request / exit 2, and
                # README reserves exit 1 for a runtime failure that may succeed
                # on retry. Reporting `runtime` here told an agent to retry a
                # request that can never succeed unchanged (issue #55).
                code_key, hint = "bad_request", None
            else:
                code_key, hint = "runtime", None
            emit_result(
                args,
                None,
                conn=ro_conn,
                slug=repo,
                db_path=db_path,
                error=_error_for(code_key, message, hint),
                identity=False,
            )
            # Exit 2 for the guard refusal so both read commands agree on the
            # documented caller-error contract; every other ValueError keeps 1.
            return 2 if code_key == "bad_request" else 1
        fresh = _freshness(ro_conn, repo)
        line = _identity_line(ro_conn, repo, db_path)
    finally:
        ro_conn.close()
    truncated = bool(data.get("truncated"))
    cols = data["columns"]
    rows = data["rows"]
    if args.rows_mode == "objects":
        # A SELECT can yield duplicate column names (joins of tables sharing
        # columns, `SELECT a, a`); a plain dict(zip) would silently drop the
        # later positions. Repeats get suffixed keys, generated collision-
        # safe against every ORIGINAL name (so a real `n_2` column keeps its
        # own key and the generated duplicate moves to `n_3`), so every
        # value survives under a deterministic key; `columns` keeps the true
        # names and `--rows array` preserves positions exactly.
        reserved = set(cols)
        used: set[str] = set()
        counts: dict[str, int] = {}
        keys: list[str] = []
        for c in cols:
            counts[c] = counts.get(c, 0) + 1
            if counts[c] == 1 and c not in used:
                keys.append(c)
                used.add(c)
                continue
            i = 2
            while f"{c}_{i}" in used or f"{c}_{i}" in reserved:
                i += 1
            keys.append(f"{c}_{i}")
            used.add(f"{c}_{i}")
        rows = [dict(zip(keys, row)) for row in rows]
    payload: dict = {"columns": cols, "rows": rows}
    if data.get("warnings"):
        # Issue #71 AC10: out-of-vocabulary warnings ride at the payload
        # top level so they survive --fields projection (which only touches
        # rows/columns).
        payload["warnings"] = data["warnings"]
    emit_result(
        args,
        payload,
        slug=repo,
        db_path=db_path,
        truncated=truncated,
        freshness=fresh,
        identity_line=line,
    )
    return 0


def cmd_where(args: argparse.Namespace) -> int:
    try:
        repo, host = _resolved_repo(args)
    except RepoError as exc:
        return emit_failure(args, 2, str(exc))
    try:
        serving, _chain = resolve_db(repo, explicit=getattr(args, "db", None), host=host)
        store_path = store_db_path(host, repo) if repo else serving
    except RepoError as exc:
        # e.g. `where --repo ''` with no --db: no slug, no store to resolve.
        return emit_failure(args, 2, str(exc), slug=repo)
    cwd = Path.cwd()
    common = git_common_root(cwd)
    exists = False
    items = 0
    watermark = None
    complete = False
    store_error = None
    fresh = None
    line = None
    if repo and store_path.exists():
        try:
            conn = open_existing(store_path)
            try:
                row = conn.execute(
                    "SELECT issues_since, last_error, full_sync_pending FROM sync_state"
                    " WHERE lower(repo) = ?",
                    (repo,),
                ).fetchone()
                items = int(
                    conn.execute(
                        "SELECT COUNT(*) AS c FROM items WHERE lower(repo) = ?", (repo,)
                    ).fetchone()["c"]
                )
                fresh = _freshness(conn, repo)
                line = _identity_line(conn, repo, serving)
            finally:
                conn.close()
            if row is not None:
                watermark = row["issues_since"]
                complete = not row["full_sync_pending"] and row["last_error"] is None
                exists = True
        except sqlite3.DatabaseError as exc:
            # A corrupt/unreadable store must stay distinguishable from a
            # never-created one (PRR-007) without crashing the diagnostic.
            store_error = str(exc)
            fresh = None
            line = None
        except RuntimeError as exc:
            # Environment guards (SQLite floor, FTS5 probe) are reported in
            # the payload like any other store fault — where is the
            # diagnostic a broken environment needs most (PR #47 review).
            store_error = str(exc)
            fresh = None
            line = None
    elif not repo and store_path.exists():
        # Slug-less where: report file-scoped facts. items is the UNFILTERED
        # count (matching --repo '' = no-filter); watermark/complete are
        # per-slug concepts and stay null/false rather than claiming a row.
        exists = True
        fresh = None
        line = None
        try:
            conn = open_existing(store_path)
            try:
                items = int(
                    conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
                )
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            store_error = str(exc)
        except RuntimeError as exc:
            store_error = str(exc)
    data = {
        "cwd": str(cwd),
        "git_common_dir": str(common) if common is not None else None,
        "slug": repo,
        "db": str(store_path),
        "exists": exists,
        "items": items,
        "watermark": watermark,
        "complete": complete,
        "legacy": [str(p) for p in legacy_db_paths(cwd)],
        "serving": str(serving),
        "sync_lock": read_lock_observer(serving),
        "store_error": store_error,
    }
    emit_result(
        args,
        data,
        slug=repo,
        db_path=serving,
        freshness=fresh,
        identity_line=line,
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        if args.repo:
            repo, host = validate_slug(args.repo), DEFAULT_HOST
        else:
            host, repo = remote_info()
    except RepoError as exc:
        return emit_failure(args, 2, str(exc), slug=None)
    store_db = store_db_path(host, repo)
    if args.db:
        store_db = Path(args.db)
    scans = [Path(s) for s in args.scan]
    try:
        data = doctor_run(
            Path.cwd(),
            repo,
            store_db,
            extra_scans=scans,
            consolidate_flag=args.consolidate,
        )
    except Exception as exc:
        # A storage fault during report/consolidation is a result object,
        # never a traceback (AGENTS.md: reported, not swallowed; PRR-010).
        emit_result(
            args,
            None,
            slug=repo,
            db_path=store_db,
            error=_error_for("runtime", str(exc)),
            identity=False,
        )
        return 1
    data.pop("store", None)
    # doctor's own ok/True is dead weight under the envelope (root ok is the
    # contract); dropping it keeps one source of truth for success.
    data.pop("ok", None)
    emit_result(args, data, slug=repo, db_path=store_db, identity=False)
    return 0


def _snapshot_freshness(db_path: Path | str | None, slug: str | None) -> dict:
    """The store's REAL freshness for a snapshot envelope (issue #72 C16 /
    plan CF-H): opened READ-ONLY through the same guard connection the sql
    path uses, so stamping the envelope never writes to (or creates) the
    store. A store that cannot be opened -- absent, busy, unreadable, or
    rejected by the environment guard -- answers the null freshness block
    (synced_at null, complete false), which is itself the truth about a
    store this operation could not read."""
    if not db_path or not slug:
        return _freshness(None, None)
    try:
        conn = connect_readonly_query(Path(db_path))
    except (FileNotFoundError, RuntimeError, OSError, sqlite3.Error):
        return _freshness(None, slug)
    try:
        return _freshness(conn, slug)
    finally:
        conn.close()


def _emit_snapshot_failure(
    args: argparse.Namespace,
    code: int,
    message: str,
    *,
    slug: str | None,
    db_path: Path | str | None,
    code_key: str,
    hint: str | None = None,
    freshness: dict | None = None,
) -> int:
    """emit_failure + the store's real freshness: a snapshot refusal must
    still say WHEN the store last synced (issue #72 C16), which the plain
    failure path cannot do (it opens no connection)."""
    emit_result(
        args,
        None,
        slug=slug,
        db_path=db_path,
        error=_error_for(code_key, message, hint),
        identity=False,
        freshness=freshness,
    )
    return code


def cmd_snapshot_publish(args: argparse.Namespace) -> int:
    try:
        if args.repo:
            repo, host = validate_slug(args.repo), DEFAULT_HOST
        else:
            host, repo = remote_info()
    except RepoError as exc:
        return emit_failure(args, 2, str(exc), slug=None)
    # the snapshot commands resolve the store exactly like sync and every
    # read command: --db > ZAXBYGRAPH_DB > existing store > legacy in-repo
    # DB > the would-be store path (paths.resolve_db)
    store_db, _chain = resolve_db(repo, explicit=args.db, host=host)
    # The store's real freshness rides EVERY publish envelope (issue #72
    # C16): stamped BEFORE the operation from a read-only connection, so a
    # refusal (and a success) both say what the store actually holds rather
    # than the null block the connection-less envelope defaulted to.
    pre_fresh = _snapshot_freshness(store_db, repo)
    try:
        result = snapshot_publish(store_db, args.target, args.publisher,
                               expected_repo=repo)
    except SnapshotError as exc:
        # The refusal's own exit code, honoured here as in cmd_snapshot_pull.
        # The exit-1 raises are the per-attempt inspection refusal
        # (dest_uninspectable: a stat/open fault on the destination or store
        # path). Every publish-reachable raise -- including git_timeout,
        # whose hint says to retry -- uses the default 2. That is a policy,
        # not a claim about causes: dest_open can be a busy destination and
        # dest_sidecar_stale's first leg is an uninspectable -wal, and
        # git_timeout fires from the transport before the destination is
        # inspected at all. So this is consistency, not a behaviour change.
        return _emit_snapshot_failure(
            args, exc.exit_code, exc.message, slug=repo, db_path=store_db,
            code_key=exc.code, hint=exc.hint, freshness=pre_fresh)
    except (OSError, sqlite3.Error) as exc:
        # a storage fault is a result object, never a traceback
        return _emit_snapshot_failure(
            args, 1, str(exc), slug=repo, db_path=store_db,
            code_key="runtime", freshness=pre_fresh)
    emit_result(args, result, slug=repo, db_path=store_db, identity=False,
                freshness=pre_fresh)
    return 0


def cmd_snapshot_pull(args: argparse.Namespace) -> int:
    try:
        if args.repo:
            repo, host = validate_slug(args.repo), DEFAULT_HOST
        else:
            host, repo = remote_info()
    except RepoError as exc:
        return emit_failure(args, 2, str(exc), slug=None)
    store_db, _chain = resolve_db(repo, explicit=args.db, host=host)
    # Freshness BEFORE the operation (issue #72 C16): every failure envelope
    # carries what the store held when the pull started -- pull may replace
    # the whole file, so the pre-operation stamp is the only honest one a
    # refusal can report.
    pre_fresh = _snapshot_freshness(store_db, repo)
    try:
        result = snapshot_pull(store_db, args.source, force=args.force,
                               verify_sample=max(0, int(args.verify_sample)),
                               expected_repo=repo, expected_host=host)
    except SnapshotError as exc:
        # The refusal's own exit code. The default is 2; the exit-1 raises
        # are the per-attempt refusals -- dest_uninspectable (a stat or open
        # fault on the destination or store path) and source_unreachable (a
        # git transport/auth failure against the snapshot SOURCE that exited
        # nonzero without the absent-ref signature; issue #72 C16). A hint
        # that says "re-pull" does NOT by itself mean 1: git_timeout,
        # dest_open, dest_sidecar_stale and corpus_mismatch all say that and
        # stay 2. That is a policy, not a claim about their causes --
        # dest_open can be a busy destination and dest_sidecar_stale's first
        # leg is an uninspectable -wal. Three of the four fire once the
        # install is underway or has landed; git_timeout fires earlier
        # still, from the transport before the destination is inspected, and
        # stays 2 as transport-policy continuity rather than because of
        # where it fires (README's exit-code table).
        return _emit_snapshot_failure(
            args, exc.exit_code, exc.message, slug=repo, db_path=store_db,
            code_key=exc.code, hint=exc.hint, freshness=pre_fresh)
    except (OSError, sqlite3.Error) as exc:
        # a storage fault is a result object, never a traceback
        return _emit_snapshot_failure(
            args, 1, str(exc), slug=repo, db_path=store_db,
            code_key="runtime", freshness=pre_fresh)
    # The store was REPLACED: re-open read-only and stamp the SUCCESS
    # envelope with the installed store's own freshness, never the stale
    # pre-pull block (issue #72 C16).
    emit_result(args, result, slug=repo, db_path=store_db, identity=False,
                freshness=_snapshot_freshness(store_db, repo))
    return 0


def cmd_export_corpus(args: argparse.Namespace) -> int:
    repo: str | None = None
    db_path: Path | None = None
    try:
        conn, repo, db_path = _open_for_read(args)
    except _ReadFailure as exc:
        return emit_failure(
            args,
            exc.code,
            exc.message,
            slug=repo or getattr(args, "repo", None) or None,
            db_path=db_path or getattr(args, "db", None),
            code_key=exc.code_key,
        )
    if repo is None:
        # Explicit --repo "" skips every corpus guard in _open_for_read; the
        # corpus is defined per repo, so refuse rather than guess a filter.
        if conn is not None:
            conn.close()
        return emit_failure(
            args,
            2,
            "export-corpus requires a resolved repository; --repo '' is not"
            " supported here",
            slug=None,
            db_path=db_path,
            code_key="repo_required",
        )
    include = frozenset(
        item.strip() for item in (args.include or "").split(",") if item.strip()
    )
    unknown = sorted(include - set(export.INCLUDE_CHOICES))
    if unknown:
        conn.close()
        return emit_failure(
            args,
            2,
            f"unknown --include value(s): {', '.join(unknown)}",
            slug=repo,
            db_path=db_path,
            code_key="bad_include",
            hint=f"--include accepts: {', '.join(export.INCLUDE_CHOICES)}",
        )
    out_path = getattr(args, "out", None)
    if out_path == "":
        conn.close()
        return emit_failure(
            args,
            2,
            "--out requires a non-empty path",
            slug=repo,
            db_path=db_path,
            code_key="bad_flags",
            hint="pass --out F with a destination file, or omit --out for stdout",
        )
    ok = False
    try:
        target = None if out_path else sys.stdout.buffer
        export.write_corpus(
            conn,
            repo,
            target,
            out_path,
            include,
            bool(getattr(args, "require_complete", False)),
            str(db_path),
        )
        ok = True
    except export.ExportError as exc:
        return emit_failure(
            args,
            exc.exit_code,
            exc.message,
            slug=repo,
            db_path=db_path,
            code_key=exc.code,
            hint=exc.hint,
        )
    except (OSError, sqlite3.Error) as exc:
        # Storage faults answer with the envelope like every other refusal
        # (AGENTS: reported, not swallowed and not a traceback).
        return emit_failure(
            args,
            1,
            f"export failed: {exc}",
            slug=repo,
            db_path=db_path,
        )
    finally:
        if ok and _format_of(args) == "text":
            # The same stderr trust summary every read prints in text mode
            # (issue #17 contract); machine formats stay silent so piped
            # captures parse.
            line = _identity_line(conn, repo, db_path)
            if line:
                print(line, file=sys.stderr)
        try:
            conn.close()
        except sqlite3.Error:
            pass
        if not out_path and ok:
            try:
                sys.stdout.flush()
            except OSError:
                pass
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    repo: str | None = None
    db_path: Path | None = None
    try:
        conn, repo, db_path = _open_for_read(args)
    except _ReadFailure as exc:
        # Pre-open failures still name what was requested: fall back to the
        # caller's --repo/--db so the envelope is not null-blind (4.5 review).
        return emit_failure(
            args,
            exc.code,
            exc.message,
            slug=repo or getattr(args, "repo", None) or None,
            db_path=db_path or getattr(args, "db", None),
            code_key=exc.code_key,
        )
    try:
        data = export_graph(conn, repo=repo)
        fresh = _freshness(conn, repo)
        line = _identity_line(conn, repo, db_path)
    finally:
        conn.close()
    emit_result(
        args, data, slug=repo, db_path=db_path, freshness=fresh, identity_line=line
    )
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    output = getattr(args, "output", None)
    if output:
        return _schema_output_shape(args, output)
    repo: str | None = None
    db_path: Path | None = None
    try:
        conn, repo, db_path = _open_for_read(args)
    except _ReadFailure as exc:
        # Pre-open failures still name what was requested: fall back to the
        # caller's --repo/--db so the envelope is not null-blind (4.5 review).
        return emit_failure(
            args,
            exc.code,
            exc.message,
            slug=repo or getattr(args, "repo", None) or None,
            db_path=db_path or getattr(args, "db", None),
            code_key=exc.code_key,
        )
    try:
        try:
            data = describe_schema(conn, args.table)
        except LookupError as exc:
            return emit_failure(
                args,
                1,
                f"no such table: {exc.args[0]}",
                slug=repo,
                db_path=db_path,
                code_key="not_found",
            )
        fresh = _freshness(conn, repo)
        line = _identity_line(conn, repo, db_path)
    finally:
        conn.close()
    emit_result(
        args, data, slug=repo, db_path=db_path, freshness=fresh, identity_line=line
    )
    return 0


def _schema_output_shape(args: argparse.Namespace, command: str) -> int:
    """`schema --output COMMAND` (issue #72 C23 / plan CF-L): print one read
    command's declared result shape — the SAME shape the shape registry in
    mcp_server.py declares for that command's MCP twin (single source of
    truth: tools/list outputSchemas, this command and the SKILL.md shape
    table all read it). The shape is static, so no store is opened and no
    corpus guard applies; an unknown or non-read command name is a
    bad_request naming the commands that do declare shapes."""
    # Lazy import: mcp_server imports this module at scope, so the CLI only
    # pulls the registry (and its heavier imports) when --output runs.
    from zaxbygraph.mcp_server import CLI_OUTPUT_SHAPES

    entry = CLI_OUTPUT_SHAPES.get(command)
    if entry is None:
        known = ", ".join(sorted(CLI_OUTPUT_SHAPES))
        return emit_failure(
            args,
            2,
            f"unknown or non-read command {command!r}: --output declares the"
            " shape of a read command's result payload",
            slug=getattr(args, "repo", None) or None,
            db_path=getattr(args, "db", None),
            code_key="bad_request",
            hint=f"read commands with declared shapes: {known}",
        )
    emit_result(
        args,
        {
            "command": command,
            "data_keys": list(entry["data_keys"]),
            "data_kind": entry.get("data_kind", "object"),
            "description": entry["description"],
            "mcp_tool": entry.get("mcp_tool"),
        },
        slug=getattr(args, "repo", None) or None,
        db_path=getattr(args, "db", None),
        identity=False,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="zaxbygraph",
        description="Local incremental GitHub issue/PR knowledge graph.",
    )
    p.add_argument("--version", action="version", version=f"zaxbygraph {__version__}")
    # Dual placement (issue #72 AC12): --db/--repo/--format are accepted
    # BEFORE the subcommand too. The subparser copies (added in add_common)
    # use default=SUPPRESS so an absent child flag never overwrites the
    # parent-parsed value, while a child-parsed value still lands.
    p.add_argument("--db", help="SQLite path (default: user-level store for the repo)")
    p.add_argument(
        "--format",
        choices=("json", "compact", "jsonl", "text"),
        default=None,
        help="json when stdout is not a TTY, text when it is; jsonl/compact for streams",
    )
    p.add_argument("--repo", help="OWNER/REPO (default: git origin)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(
        sp: argparse.ArgumentParser, *, repo: bool = True, max_age: bool = False
    ) -> None:
        sp.add_argument("--db", default=argparse.SUPPRESS,
                        help="SQLite path (default: user-level store for the repo)")
        sp.add_argument(
            "--format",
            choices=("json", "compact", "jsonl", "text"),
            default=argparse.SUPPRESS,
            help="json when stdout is not a TTY, text when it is; jsonl/compact for streams",
        )
        sp.add_argument(
            "--fields",
            default=None,
            help="Comma-separated keys to project result rows to (e.g. repo,number)",
        )
        if repo:
            sp.add_argument("--repo", default=argparse.SUPPRESS,
                            help="OWNER/REPO (default: git origin)")
        if max_age:
            # --max-age D (issue #31 AC5): reads may bound staleness and pay
            # one locked incremental sync for freshness. Off by default —
            # without the flag reads never write (AC12). Deliberately absent
            # from `sql` and the export/doctor writers: the sql escape hatch
            # must never write, and the writers manage their own freshness.
            sp.add_argument(
                "--max-age",
                dest="max_age",
                type=_parse_max_age,
                default=None,
                metavar="D",
                help="Refresh the store first when older than D seconds"
                " (or Ns/Nm/Nh/Nd) via a locked incremental sync; reports"
                " freshness.refreshed; without the flag reads never write",
            )

    sp = sub.add_parser("sync", help="Fetch issues/PRs into the local graph")
    add_common(sp)
    sp.add_argument("--force", action="store_true", help="Ignore watermark; full pull")
    sp.add_argument(
        "--numbers",
        default=None,
        metavar="N,M",
        help="Refresh exactly these items; the watermark is untouched",
    )
    sp.add_argument("--include-patches", action="store_true", help="Store pull file patches")
    sp.add_argument(
        "--source",
        choices=("graphql", "rest"),
        default=None,
        help="Fetch source: graphql bulk pages (default) or plain REST"
        " pages. Only an explicitly stated source re-stamps the store's"
        " recorded fetch profile (issue #48)",
    )
    sp.add_argument(
        "--jsonl",
        nargs="?",
        const=True,
        default=False,
        dest="jsonl_raw",
        help="Append JSONL sidecar (optional DIR; default sibling jsonl/)",
    )
    sp.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the sync lock instead of returning joined:true",
    )
    sp.set_defaults(func=_sync_entry)

    sp = sub.add_parser(
        "status",
        help="Counts, watermark and corpus coverage (no bodies)",
    )
    add_common(sp, max_age=True)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser(
        "search",
        help="relevance-ranked FTS search over titles, bodies, labels, comments, review bodies, and PR touched paths",
    )
    add_common(sp, max_age=True)
    sp.add_argument("query")
    sp.add_argument(
        "--limit",
        type=int,
        default=20,
        help="page size; also the AND-to-OR fallback threshold",
    )
    sp.add_argument(
        "--offset",
        type=_parse_offset,
        default=0,
        metavar="N",
        help="Skip the first N rows of the deduplicated strict-first result"
        " stream (strict hits first, then broadened non-duplicates)",
    )
    sp.add_argument(
        "--no-bots",
        dest="no_bots",
        action="store_true",
        help="Omit bot-authored matches (actors.is_bot=1): bot-authored items,"
        " comments and reviews are filtered out of the match SQL before"
        " ranking, counts and snippets; reports bots_omitted (excluded"
        " bot-authored matches)",
    )
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("item", help="One issue/PR with comments, files, edges")
    add_common(sp, max_age=True)
    sp.add_argument(
        "number",
        type=int,
        nargs="+",
        metavar="NUMBER",
        help="One or more item numbers; a single number answers the item"
        " object, several answer one row per number in order (unstored"
        " numbers answer per-row not_found, issue #72 C22)",
    )
    sp.add_argument(
        "--max-body-chars",
        type=int,
        default=None,
        metavar="N",
        help="Truncate the item/comment/review bodies to N chars (marks truncated)",
    )
    sp.add_argument(
        "--max-comments",
        dest="max_comments",
        type=_parse_comment_cap,
        default=None,
        metavar="K",
        help="Cap the comment list to K entries (first, last and maintainer"
        " comments preferred; reports comments_omitted); without the flag the"
        " default bound is 5 comments and 5 reviews (comments_omitted/"
        " reviews_omitted report the cut, and the body is bounded to 20 KiB"
        " with a truncation marker)",
    )
    sp.add_argument(
        "--no-bots",
        dest="no_bots",
        action="store_true",
        help="Omit bot-authored comments and reviews (actors.is_bot=1),"
        " filtered in the query before omission counting; reports"
        " bots_omitted (excluded comment+review rows)",
    )
    sp.add_argument(
        "--history",
        action="store_true",
        help="List the item's stored revision pre-images (issue #25)",
    )
    sp.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        metavar="T",
        help="Reconstruct the item's text entities as of GitHub time T (ISO-8601)",
    )
    sp.set_defaults(func=cmd_item)

    sp = sub.add_parser("related", help="1-hop neighborhood")
    add_common(sp, max_age=True)
    sp.add_argument("number", type=int)
    sp.add_argument("--depth", type=int, default=1)
    sp.set_defaults(func=cmd_related)

    sp = sub.add_parser("churn", help="Files ranked by PR touch count")
    add_common(sp, max_age=True)
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument(
        "--merged-only",
        dest="merged_only",
        action="store_true",
        help="Count only PRs that actually merged (drop closed-unmerged work)",
    )
    sp.set_defaults(func=cmd_churn)

    sp = sub.add_parser("open", help="Open issues and pull requests")
    add_common(sp, max_age=True)
    sp.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Page size (default: every open item)",
    )
    sp.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Skip the first N rows (applies with or without --limit)",
    )
    sp.add_argument(
        "--kind",
        choices=("issue", "pr"),
        default=None,
        help="Restrict to issues or pull requests",
    )
    sp.add_argument(
        "--label",
        default=None,
        help="Only items carrying this exact label name",
    )
    sp.set_defaults(func=cmd_open)

    sp = sub.add_parser("path", help="Undirected path between two item numbers or file paths")
    add_common(sp, max_age=True)
    sp.add_argument("a")
    sp.add_argument("b")
    sp.set_defaults(func=cmd_path)

    sp = sub.add_parser(
        "similar", help="Related items with per-signal evidence (derived, read-only)"
    )
    add_common(sp, max_age=True)
    sp.add_argument("number", type=int)
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument(
        "--ls-files",
        action="store_true",
        help="Intersect resolved paths with this checkout's git ls-files",
    )
    sp.add_argument("--config", help="Explicit [derive] weights TOML (highest precedence)")
    sp.set_defaults(func=cmd_similar)

    sp = sub.add_parser(
        "explain", help="Full per-signal breakdown for one pair (derived, read-only)"
    )
    add_common(sp, max_age=True)
    sp.add_argument("a", type=int)
    sp.add_argument("b", type=int)
    sp.add_argument("--ls-files", action="store_true")
    sp.add_argument("--config", help="Explicit [derive] weights TOML (highest precedence)")
    sp.set_defaults(func=cmd_explain)

    sp = sub.add_parser(
        "coupling",
        help="Files that change together with PATH (merged PRs, support/confidence/lift)",
    )
    add_common(sp, max_age=True)
    sp.add_argument("path")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_coupling)

    sp = sub.add_parser(
        "eval",
        help="Measure retrieval quality against GitHub-reported ground truth",
    )
    add_common(sp, max_age=True)
    sp.add_argument("mode", choices=["similar", "localize", "bundle"])
    sp.add_argument("--config", help="Explicit [derive] weights TOML (highest precedence)")
    sp.set_defaults(func=cmd_eval)

    sp = sub.add_parser(
        "triage",
        help="Rank open issues by demand/leverage/risk/priority (derived, read-only)",
    )
    add_common(sp, max_age=True)
    sp.add_argument("--limit", type=int, default=20, help="Page size (default 20)")
    sp.add_argument("--cursor", default=None, metavar="T:N", help="Page cursor from a previous payload")
    sp.add_argument(
        "--top-k", dest="top_k", type=int, default=None,
        help="Stage-2 fan over the top-K ungated rows (default: [triage] top_k)",
    )
    sp.add_argument(
        "--include-gated", dest="include_gated", action="store_true",
        help="Keep gated rows in the response (ranked: false, gates named)",
    )
    sp.add_argument("--config", help="Explicit [triage] TOML (highest precedence)")
    sp.set_defaults(func=cmd_triage)

    sp = sub.add_parser(
        "bundle",
        help="Greedy work bundles over the triage pool (derived, read-only)",
    )
    add_common(sp, max_age=True)
    sp.add_argument("--size", default="2-3", metavar="N-M", help="Bundle size range (default 2-3)")
    sp.add_argument("--count", type=int, default=5, help="Maximum bundles (default 5)")
    sp.add_argument("--pool", default=None, metavar="N,M", help="Explicit pool member numbers")
    sp.add_argument(
        "--exclude", default=None, metavar="N,M",
        help="Remove these numbers from the pool entirely",
    )
    sp.add_argument(
        "--allow-shared-files", dest="allow_shared_files", action="store_true",
        help="Permit bundles to share files across bundles",
    )
    sp.add_argument("--max-files", dest="max_files", type=int, default=None,
                    help="Bundle file-set cap (default: [triage] max_files)")
    sp.add_argument("--budget-tokens", dest="budget_tokens", type=int, default=None,
                    help="Cap the serialized payload at N tokens (chars/4; positive integer)")
    sp.add_argument("--config", help="Explicit [triage] TOML (highest precedence)")
    sp.set_defaults(func=cmd_bundle)

    sp = sub.add_parser(
        "brief",
        help="One issue's work packet: features, body, comments, files, analog (derived, read-only)",
    )
    add_common(sp, max_age=True)
    sp.add_argument("number", type=int)
    sp.add_argument(
        "--budget-tokens", dest="budget_tokens", type=int, default=4000,
        help="Packet budget in tokens (chars/4; positive integer, default 4000)",
    )
    sp.add_argument(
        "--live", action="store_true",
        help="Also read linked-PR CI state/reviews via gh (never stored; failures degrade)",
    )
    sp.add_argument(
        "--no-bots",
        dest="no_bots",
        action="store_true",
        help="Omit bot-authored content: bot comments are filtered out of the"
        " comment ranking and bot-authored items (linked PRs, the analog)"
        " out of the packet; reports bots_omitted (filtered rows)",
    )
    sp.add_argument("--config", help="Explicit [triage] TOML (highest precedence)")
    sp.set_defaults(func=cmd_brief)

    sp = sub.add_parser("sql", help="Read-only SQL (SELECT/WITH/EXPLAIN)")
    add_common(sp)
    sp.add_argument("statement")
    sp.add_argument(
        "--rows",
        choices=("objects", "array"),
        default="objects",
        dest="rows_mode",
        help="Row shape: objects keyed by column (default) or positional arrays",
    )
    sp.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum rows to return (default 200)",
    )
    sp.set_defaults(func=cmd_sql)

    sp = sub.add_parser("schema", help="Table DDL and per-column notes from the live DB")
    add_common(sp, max_age=True)
    sp.add_argument(
        "table",
        nargs="?",
        default=None,
        help="One table name (default: every table)",
    )
    sp.add_argument(
        "--output",
        default=None,
        metavar="COMMAND",
        help="Print one read command's declared result shape"
             " (data keys; the same shape registry the MCP outputSchemas"
             " derive from) instead of the live DDL",
    )
    sp.set_defaults(func=cmd_schema)

    sp = sub.add_parser("export-graph", help="Graphify-shaped {nodes, edges} JSON")
    add_common(sp)
    sp.set_defaults(func=cmd_export)
    sp = sub.add_parser(
        "export-corpus",
        help="Deterministic hash-stamped corpus snapshot (JSONL, zaxbygraph.corpus.v1)",
    )
    add_common(sp)
    sp.add_argument("--out", default=None, metavar="F", help="Write to file F (default: stdout, binary)")
    sp.add_argument(
        "--include",
        default="",
        metavar="ITEMS",
        help="Comma-separated extras: raw,patches,revisions",
    )
    sp.add_argument(
        "--require-complete",
        action="store_true",
        help="Refuse (exit 1) unless the store is complete and error-free",
    )
    sp.set_defaults(func=cmd_export_corpus)

    sp = sub.add_parser(
        "snapshot",
        help="Verified snapshot transport (publish|pull; issue #34)",
    )
    snap_sub = sp.add_subparsers(dest="snapshot_command", required=True)
    sp_pub = snap_sub.add_parser("publish", help="Push a verified store snapshot as one orphan commit")
    add_common(sp_pub)
    sp_pub.add_argument("target", help="Target git repository (path or URL)")
    sp_pub.add_argument("--publisher", default=None, help="Publisher name recorded in the manifest")
    sp_pub.set_defaults(func=cmd_snapshot_publish)
    sp_pull = snap_sub.add_parser("pull", help="Verify and install a snapshot")
    add_common(sp_pull)
    sp_pull.add_argument("source", help="Source git repository (path or URL)")
    sp_pull.add_argument("--force", action="store_true",
                         help="Overwrite a newer local store")
    sp_pull.add_argument("--verify-sample", dest="verify_sample", type=int,
                         default=0, metavar="N",
                         help="Re-fetch N random items from the live source"
                              " and compare their stored fields (title/body/"
                              "state/timestamps/author/labels/pull-marker)"
                              " for drift (issue #34)")
    sp_pull.set_defaults(func=cmd_snapshot_pull)

    sp = sub.add_parser("where", help="Print the resolution chain for this checkout")
    add_common(sp)
    sp.set_defaults(func=cmd_where)

    sp = sub.add_parser("doctor", help="Report (and optionally consolidate) legacy DBs")
    add_common(sp)
    sp.add_argument(
        "--scan",
        action="append",
        default=[],
        metavar="DIR",
        help="Extra directory to scan for history.db files (repeatable)",
    )
    sp.add_argument(
        "--consolidate",
        action="store_true",
        help="Adopt the freshest complete corpus into the store (copies, never deletes)",
    )
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser(
        "overlap",
        help="Shared files for each pair of PRs (the collision check)",
    )
    add_common(sp, max_age=True)
    sp.add_argument("numbers", type=int, nargs="+", metavar="NUMBER")
    sp.set_defaults(func=cmd_overlap)

    sp = sub.add_parser(
        "file-history",
        help="PRs that touched a path, newest first, with the issues they closed",
    )
    add_common(sp, max_age=True)
    sp.add_argument("path")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_file_history)

    sp = sub.add_parser(
        "what-closed",
        help="Closing PRs and commits for an issue, with edge provenance",
    )
    add_common(sp, max_age=True)
    sp.add_argument("number", type=int)
    sp.set_defaults(func=cmd_what_closed)

    sp = sub.add_parser(
        "mcp", help="Run the stdio MCP server (typed tools over JSON-RPC 2.0)"
    )
    add_common(sp)
    sp.add_argument(
        "--stale-seconds",
        type=int,
        default=None,
        dest="stale_seconds",
        help=(
            "Freshness age that triggers a background refresh on reads "
            "(default 900; env ZAXBYGRAPH_MCP_STALE_SECONDS)"
        ),
    )
    sp.set_defaults(func=cmd_mcp)
    return p


def _sync_entry(args: argparse.Namespace) -> int:
    raw = getattr(args, "jsonl_raw", False)
    args.jsonl_flag = bool(raw)
    args.jsonl = None if raw is True or raw is False else str(raw)
    return cmd_sync(args)


class UsageError(Exception):
    """A failed command-line parse (issue #72 AC12): argparse's own message
    plus the nearest valid flag, raised by the parser base instead of
    printing usage, so a machine-format caller gets an envelope."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def _nearest_flag_hint(message: str, parser: argparse.ArgumentParser) -> str | None:
    """The nearest valid flag for an unknown-flag/invalid-value message
    (difflib over the parser's own option strings); for messages without a
    flag token (missing positional), point at the parser's --help."""
    known: list[str] = []
    actions = list(parser._actions)  # noqa: SLF001 - argparse introspection
    for action in parser._actions:  # noqa: SLF001 - argparse introspection
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            # Union the subparsers: "unrecognized arguments" is detected at
            # the top level, after the subparser consumed the rest.
            for sp in choices.values():
                actions.extend(sp._actions)  # noqa: SLF001
    for action in actions:
        known.extend(a for a in action.option_strings if a not in known)
    tokens = re.findall(r"--?[A-Za-z][\w-]*", message)
    for token in tokens:
        match = difflib.get_close_matches(token, known, n=1, cutoff=0.55)
        if match:
            return f"did you mean {match[0]}? (see `zaxbygraph {parser.prog} --help`)"
    prog = parser.prog if " " in parser.prog else "COMMAND"
    return f"see `zaxbygraph {prog} --help` for this command's arguments"


class _Parser(argparse.ArgumentParser):
    """Parser base whose error() raises UsageError instead of printing
    usage (issue #72 AC12): main() decides the output channel. Help and
    --version still exit through their own SystemExit(0) paths."""

    def error(self, message: str):  # type: ignore[override]
        raise UsageError(message, _nearest_flag_hint(message, self))


def _usage_envelope_exit(
    exc: UsageError, argv: list[str] | None
) -> int:
    """Answer a usage failure: under a machine format (explicit json/
    compact/jsonl in argv, or stdout not a TTY) a bad_request envelope goes
    to stdout with argparse's message and the nearest-flag hint (exit 2);
    text mode on a TTY keeps the human one-liner on stderr. The db/repo
    identity is best-effort pre-scanned from argv (parsing already failed)."""
    fmt = None
    db = repo = None
    if argv:
        for i, tok in enumerate(argv):
            if tok == "--format" and i + 1 < len(argv):
                fmt = argv[i + 1]
            elif tok == "--db" and i + 1 < len(argv):
                db = argv[i + 1]
            elif tok == "--repo" and i + 1 < len(argv):
                repo = argv[i + 1]
    if fmt is None:
        fmt = "json" if not sys.stdout.isatty() else "text"
    error = {"code": "bad_request", "message": exc.message}
    if exc.hint:
        error["hint"] = exc.hint
    if fmt in ("json", "compact", "jsonl"):
        env = build_envelope(
            None, conn=None, slug=repo, db_path=db, error=error
        )
        _render_json(env, indent=None)
    else:
        print(f"error: {exc.message}", file=sys.stderr)
    return 2


def _silence_broken_stdout() -> int:
    """Give the dying process a valid stdout: point fd 1 at os.devnull so the
    interpreter's own shutdown flush (the buffered tail of whatever was
    already written) succeeds quietly instead of raising the unraisable
    'Exception ignored in' noise and a nonzero shutdown exit (issue #71,
    check C24). Narrowly scoped: only reached from a broken-stdout shape."""
    try:
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        pass
    try:
        fd = sys.stdout.fileno()
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, fd)
        finally:
            os.close(devnull)
    except (BrokenPipeError, OSError, ValueError):
        pass
    return 0


def _is_broken_stdout_flush(exc: OSError) -> bool:
    """Windows reports the dead pipe at flush time as a plain OSError with
    errno EINVAL (22), not BrokenPipeError; POSIX raises EPIPE. Only a
    failure flushing sys.stdout is this shape — the guard that consults this
    predicate wraps the stdout flush alone, never the dispatch, so unrelated
    storage/runtime OSErrors keep propagating."""
    return exc.errno in (errno.EINVAL, errno.EPIPE)


def main(argv: list[str] | None = None) -> int:
    # Pin the streams before argparse runs: its usage/error text is the first
    # user-facing output and can carry non-ASCII from bad arguments.
    _force_utf8_streams()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except UsageError as exc:
        # A failed parse answers the structured envelope under a machine
        # format (issue #72 AC12); text mode on a TTY keeps the human line.
        return _usage_envelope_exit(exc, argv)
    if getattr(args, "repo", None):
        try:
            args.repo = validate_slug(args.repo)
        except RepoError as exc:
            # Post-parse validation: still answers with the structured
            # envelope on stdout (argparse's own usage errors stay on stderr).
            return emit_failure(args, 2, str(exc))
    try:
        code = int(args.func(args))
    except BrokenPipeError:
        # The reader went away (`| head` exited): the quiet success exit.
        return _silence_broken_stdout()
    try:
        sys.stdout.flush()
    except (BrokenPipeError, OSError) as exc:
        # The shutdown-time shape: the output sat buffered past dispatch and
        # the interpreter's exit flush hit the dead pipe (Windows EINVAL).
        if isinstance(exc, BrokenPipeError) or _is_broken_stdout_flush(exc):
            return _silence_broken_stdout()
        raise
    return code
