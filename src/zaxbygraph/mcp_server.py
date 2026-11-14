"""stdio MCP (Model Context Protocol) server for zaxbygraph — issue #7.

A thin adapter over query.py / paths.py / cli.py helpers with no query
logic of its own: every tool resolves a repo the way the CLI does, opens
the same read-only connections, and answers with the same Workstream A
JSON envelope `{ok, db, repo, freshness, data, truncated}` (plus
`error{code, message, hint}` on failure).

Framing: newline-delimited JSON-RPC 2.0 over TEXT streams — one UTF-8
JSON object per line. stdout carries ONLY protocol frames; every log line
goes to stderr. serve() returns at stdin EOF without joining in-flight
background jobs (daemon threads; the whole-run sync lock's same-host
dead-PID takeover recovers abandoned locks).

Disclosed boundaries:
- Requests are serialized; messages that arrive while the server awaits
  its own `roots/list` reply are buffered and processed after it, so
  every identified request still gets exactly one response.
- Repo resolution asks the client for roots (one `roots/list` round-trip
  per resolution) only when the client DECLARED the roots capability in
  `initialize`; an undeclared client never receives a server-initiated
  request — resolution falls through to the server --repo pin, then the
  server cwd's git origin, and a repo-less call that resolves nothing
  answers bad_request naming the repo argument.
- A failed staleness refresh is ISOLATED: the store's sync_state row is
  snapshot-restored after any failed run (a background failure never
  poisons a previously-complete store), and re-arming is backed off for
  ZAXBYGRAPH_REFRESH_BACKOFF_S seconds (default 300) — a suppressed read
  reports freshness.refresh_failed instead of refreshing. The explicit
  `sync` tool keeps normal error recording (issue #71 C3).
"""

from __future__ import annotations

import itertools
import json
import errno
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

from zaxbygraph import __version__
from zaxbygraph.cli import (
    _brief_query,
    _bundle_query,
    _freshness,
    _item_count,
    _similar_query,
    _triage_query,
    _truncate_bodies,
    build_envelope,
    corpus_state,
)
from zaxbygraph.db import connect, connect_readonly_query, init_schema, open_existing
from zaxbygraph.derive import ConfigError as DeriveConfigError
from zaxbygraph.github import GhApiSource as GhRestSource
from zaxbygraph.graphql import GraphQLSource as GhApiSource
from zaxbygraph.paths import resolve_db
from zaxbygraph.query import (
    apply_item_bounds,
    _clamp_limit,
    assert_read_sql,
    churn,
    file_history,
    item,
    open_items,
    page_truncated,
    path_between,
    pr_overlap,
    related,
    resolve_file_history_path,
    run_sql,
    search,
    sql_deadline_from_env,
    sql_max_bytes_from_env,
    SqlTimeoutError,
    status,
    store_tables_hint,
    UnknownFilePathError,
    what_closed,
)
from zaxbygraph.repo import DEFAULT_HOST, RepoError, remote_info, validate_slug
from zaxbygraph.schema_notes import describe_schema
from zaxbygraph.sync import acquire_sync_lock, read_lock_observer, sync_repo

#: Protocol revisions this server can speak. initialize echoes the
#: client's requested version when we serve it, else the latest one.
#: 2026-07-28 is NOT negotiated here — it is a per-request `_meta` revision
#: (see _MODERN_PROTOCOL_VERSION); a modern-marked `initialize` request is
#: still answered with the latest legacy revision.
_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_LATEST_PROTOCOL_VERSION = _PROTOCOL_VERSIONS[0]

#: The modern, stateless revision: the protocol version and client
#: capabilities ride EVERY request's `_meta` (namespaced keys per the
#: official 2026-07-28 RequestMetaObject); there is no initialize handshake
#: and no ping on this era.
_MODERN_PROTOCOL_VERSION = "2026-07-28"
_META_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
#: Sentinel for "namespaced key absent" in _era_of: key-present-with-null
#: is malformed modern metadata (-32602), never a silent legacy downgrade
#: (fix-round probe P14).
_MODERN_ABSENT = object()
_META_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"

#: Cache hints (the official CacheableResult fields) for results that are
#: cheap to recompute but stable within a process: the tool registry is
#: static per server, per-resource reads are private to the caller.
_LISTING_TTL_MS = 300000
_RESOURCE_READ_TTL_MS = 60000

#: Failure-backoff window for staleness refreshes (issue #71 C3 / plan
#: CF-B): after a FAILED background refresh, re-arming for that
#: (db_path, slug) is suppressed for this many seconds. Read from the
#: environment AT CALL TIME so operators (and tests) can widen or shrink
#: it per run; a missing or non-numeric value falls back to the default.
_REFRESH_BACKOFF_ENV = "ZAXBYGRAPH_REFRESH_BACKOFF_S"
_DEFAULT_REFRESH_BACKOFF_S = 300.0

#: Staleness threshold default: the issue's proposed 15 minutes.
_DEFAULT_STALE_AFTER_S = 900

#: Ceilings for client-controlled work on the serialized server: a single
#: tool call must never scale its fetch or expansion with an unbounded
#: argument (review round-2, finding F1/PRR-001).
_MAX_PAGE = 500
_MAX_CURSOR = 10000
_MAX_DEPTH = 5
_MAX_TOP_K = 50
_MAX_NUMBERS = 64
_STALE_ENV = "ZAXBYGRAPH_MCP_STALE_SECONDS"

_SCHEMA_RESOURCE_URI = "zaxbygraph://schema"

_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL_ERROR = -32603


def _log(message: str) -> None:
    """Protocol discipline: logs go to stderr, never to stdout."""
    print(f"zaxbygraph-mcp: {message}", file=sys.stderr, flush=True)


class _InputRequired(Exception):
    """A modern (2026-07-28) call cannot resolve a repository and must
    answer an InputRequiredResult naming `repo` instead of round-tripping
    roots. Raised by _resolve_target on the modern path only; the legacy
    path keeps the roots handshake (AC12)."""

    REQUEST_STATE = "zaxbygraph:repo-required"

    def hint_text(self, *, resources_channel: bool = False) -> str:
        # resources/read has NO arguments channel (params are uri + _meta),
        # so "pass repo" is unactionable there — name the real remedies
        # instead (pr49 review PRR-009).
        if resources_channel:
            return (
                "repo required: start the server with --repo/--db, or use the "
                "tools (which accept a repo argument)"
            )
        return (
            "repo required: pass repo as OWNER/REPO (no explicit repo argument "
            "and no server --repo pin resolved one)"
        )


def _era_of(params: dict) -> tuple[str, str | None]:
    """(era, declared) for this request's era classification.

    "modern" — the namespaced protocolVersion key carries "2026-07-28";
    "unsupported" — it carries another string (declared but not served on
    the per-request channel; -32022); "malformed" — present but not a
    string (the 2026-07-28 table types it string/required, so this is
    malformed modern metadata: -32602, never a silent legacy downgrade —
    pr49 review PRR-004); "legacy" — no namespaced declaration at all
    (including any legacy _meta shape)."""
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return "legacy", None
    declared = meta.get(_META_VERSION_KEY, _MODERN_ABSENT)
    if declared is _MODERN_ABSENT:
        return "legacy", None
    if not isinstance(declared, str):
        return "malformed", None
    if declared != _MODERN_PROTOCOL_VERSION:
        return "unsupported", declared
    return "modern", declared


def _modern_capabilities(params):
    """The clientCapabilities object of a modern request (checked by the
    caller to have declared a served modern version first)."""
    meta = (params or {}).get("_meta")
    if not isinstance(meta, dict):
        return None
    return meta.get(_META_CAPABILITIES_KEY)


class ToolError(Exception):
    """A tool-shaped failure that must become the envelope error object.

    `code_key` uses the Workstream A error vocabulary; `hint` is always
    non-empty in practice — an error that does not say what to try next
    is the exact 'failures look like success' defect this surface removes.
    `slug`/`db_path` carry the resolution identity when it is known, so
    the error envelope is not null-blind (mirrors the CLI's 4.5 review
    fix)."""

    def __init__(
        self,
        code_key: str,
        message: str,
        hint: str | None = None,
        *,
        slug: str | None = None,
        db_path=None,
    ) -> None:
        super().__init__(message)
        self.code_key = code_key
        self.message = message
        self.hint = hint
        self.slug = slug
        self.db_path = db_path


# ---------------------------------------------------------------------------
# Background sync jobs (first threading in the package — see 05-fix-plan)

_JOBS_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_JOB_SEQ = itertools.count(1)


def _default_sync_runner(
    db_path, repo: str, *, force: bool = False, include_patches: bool = False,
    numbers: list[int] | None = None, source: str | None = None,
) -> None:
    """The real background sync: the cmd_sync core without CLI output.

    The source and patch profile come from the store's own recording
    (issue #65 AC5): a rest-stamped store is refreshed over REST — the
    hardcoded GraphQL construction used to flip it on every background
    refresh — and a patch-enabled store keeps its patches instead of
    silently losing them. An explicit argument overrides. The injected
    seam is pinned to (db_path, repo)."""
    conn = connect(db_path)
    try:
        init_schema(conn)
        row = conn.execute(
            "SELECT include_patches, fetch_source FROM sync_state WHERE repo = ?",
            (repo,),
        ).fetchone()
        stored_patches = bool(row["include_patches"]) if row else False
        stored_source = (
            row["fetch_source"] if row and row["fetch_source"] else None
        ) or "graphql"
        effective_patches = include_patches or stored_patches
        effective_source = source or stored_source
        if effective_source == "rest":
            src = GhRestSource(*repo.split("/", 1))
        else:
            src = GhApiSource(*repo.split("/", 1))
        sync_repo(conn, src, repo, force=force, include_patches=effective_patches,
                  numbers=numbers,
                  # The envelope reports the source this run actually used;
                  # an EXPLICIT source argument re-stamps the stored profile
                  # exactly as the CLI's --source does (an omitted argument
                  # inherits and never rewrites — issue #48).
                  source_name=effective_source,
                  fetch_source=source)
    finally:
        conn.close()


def _start_job(db_path: Path, slug: str, sync_runner) -> tuple[str, bool]:
    """Start one background sync for (db_path, slug); never blocks.

    Single-flight is enforced twice, in this order: an atomic registry
    check keyed by the exact db_path string (covers injected runners that
    hold no OS lock), then the whole-run sync lock (covers other
    processes). The thread body clears its registry state in its
    `finally` BEFORE releasing the lock, so a stale read can never see a
    free lock behind a still-running registry entry. Returns
    (job_id, started); started is False when an equivalent job is already
    in flight or the lock is held elsewhere (reported as joined).
    """
    key = str(db_path)
    with _JOBS_LOCK:
        for job_id, job in _JOBS.items():
            if job["db_path"] == key and job["state"] == "running":
                return job_id, False
        finished = [k for k, v in _JOBS.items() if v["state"] != "running"]
        while len(finished) >= 50:  # settled bound: after insert at most 50
            # finished entries remain
            _JOBS.pop(finished.pop(0), None)
        job_id = f"sync-{next(_JOB_SEQ)}"
        _JOBS[job_id] = {
            "id": job_id,
            "db_path": key,
            "repo": slug,
            "state": "running",
            "error": None,
        }
    try:
        lock = acquire_sync_lock(db_path)
    except Exception as exc:
        # Any failure here (not just OSError) must roll the entry back,
        # or every later read joins a phantom forever (review F9).
        with _JOBS_LOCK:
            _JOBS.pop(job_id, None)
        raise
    if lock is None:
        # Another process owns the run lock: our caller joins it. Roll
        # the entry back — no thread exists to transition it, and a
        # stuck 'running' row would wedge single-flight forever
        # (review round-3). The external sync stays visible through
        # read_lock_observer.
        with _JOBS_LOCK:
            _JOBS.pop(job_id, None)
        return "external", False

    def _body() -> None:
        try:
            sync_runner(db_path, slug)
            with _JOBS_LOCK:
                _JOBS[job_id]["state"] = "done"
        except BaseException as exc:  # contained: job marked failed, logged
            _log(f"background sync {job_id} for {slug} failed: {exc}")
            with _JOBS_LOCK:
                _JOBS[job_id]["state"] = "failed"
                _JOBS[job_id]["error"] = str(exc)
        finally:
            # Registry state first, then the lock (plan-pinned order).
            lock.release_owned()

    try:
        threading.Thread(target=_body, name=f"zaxbygraph-{job_id}", daemon=True).start()
    except BaseException as exc:
        # Roll everything back: the lock is ours, the registry entry is
        # ours, and no thread exists to release either (review F9).
        with _JOBS_LOCK:
            _JOBS.pop(job_id, None)
        lock.release_owned()
        raise
    return job_id, True


def _jobs_snapshot(db_key: str) -> list[dict]:
    with _JOBS_LOCK:
        return [dict(job) for job in _JOBS.values() if job["db_path"] == db_key]


#: (db_path string, slug) -> time.monotonic() of the last FAILED
#: staleness refresh. Written by the isolated refresh wrapper's failure
#: path, read by _read_envelope before re-arming.
_BACKOFF_LOCK = threading.Lock()
_REFRESH_BACKOFF: dict[tuple[str, str], float] = {}


def _refresh_backoff_s() -> float:
    """The configured backoff window, read at call time (the env seam:
    operators and tests change it between reads, never at import)."""
    raw = os.environ.get(_REFRESH_BACKOFF_ENV)
    if raw is None:
        return _DEFAULT_REFRESH_BACKOFF_S
    try:
        value = float(raw)
    except ValueError:
        _log(f"ignoring non-numeric {_REFRESH_BACKOFF_ENV}={raw!r}")
        return _DEFAULT_REFRESH_BACKOFF_S
    return value if value > 0 else 0.0


#: Sentinel _snapshot_sync_state returns when the pre-run row could not be
#: READ (open/transport fault) — distinct from None, which means the row
#: genuinely does not exist. Containment must never touch a store whose
#: pre-run state it could not observe (08b review Q4): a DELETE issued on
#: an unread snapshot can erase a sync_state row it never saw.
_SNAPSHOT_READ_FAILED = object()


def _snapshot_sync_state(db_path, slug: str):
    """The repo's FULL sync_state row before an isolated refresh run
    (every column — error/pending flags, sync stamps, watermark fields —
    so a restore is byte-faithful): the row dict, None when no row exists
    yet, or the _SNAPSHOT_READ_FAILED sentinel when the read itself failed
    (a restore then cannot know what to restore and must not run).

    Read-only: a failed refresh must be isolated FROM the store, so the
    snapshot itself must never write (connect_readonly_query)."""
    try:
        conn = connect_readonly_query(db_path)
    except (FileNotFoundError, RuntimeError, sqlite3.Error, OSError):
        return _SNAPSHOT_READ_FAILED
    try:
        row = conn.execute(
            "SELECT * FROM sync_state WHERE repo = ?", (slug,)
        ).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}
    except sqlite3.Error:
        return _SNAPSHOT_READ_FAILED
    finally:
        conn.close()


def _restore_sync_state(db_path, slug: str, snapshot: dict | None) -> None:
    """Put the snapshotted sync_state row back in a FRESH write
    connection (plan CF-B: cover every failure write site inside sync by
    restoration, not flag-threading). A None snapshot restores the
    no-row state (the failed run may have created one)."""
    conn = connect(db_path)
    try:
        if snapshot is None:
            conn.execute("DELETE FROM sync_state WHERE repo = ?", (slug,))
        else:
            key = snapshot.get("repo", slug)
            columns = [c for c in snapshot if c != "repo"]
            assignments = ", ".join(
                f'"{c.replace(chr(34), chr(34) * 2)}" = ?' for c in columns
            )
            conn.execute(
                f"UPDATE sync_state SET {assignments} WHERE repo = ?",
                [snapshot[c] for c in columns] + [key],
            )
        conn.commit()
    finally:
        conn.close()


def _contain_refresh_failure(db_path, slug: str, snapshot) -> None:
    """The isolated wrapper's failure path: restore the pre-run
    sync_state snapshot, then arm the failure backoff. Restore faults are
    logged, never raised over the refresh's own exception.

    A _SNAPSHOT_READ_FAILED sentinel means the pre-run row could not be
    read (08b review Q4): containment REFUSES to touch the store — a
    blind DELETE/UPDATE against an unobserved row is exactly the poison
    the isolation exists to prevent — logs, and still arms the backoff so
    the failure stays reported and suppressed; the original failure is
    re-raised by the caller either way."""
    # The backoff arms FIRST (FB-023): _log writes stderr bare, and a dead
    # stderr stream raising out of a log must not leave the failure
    # unsuppressed — the storm is the harm, the line is a nicety.
    with _BACKOFF_LOCK:
        _REFRESH_BACKOFF[(str(db_path), slug)] = time.monotonic()
    if snapshot is _SNAPSHOT_READ_FAILED:
        try:
            _log(
                f"staleness refresh containment for {slug} on {db_path}"
                " skipped: the pre-run sync_state snapshot could not be"
                " read; the store is left as the failed run left it"
            )
        except (OSError, ValueError):
            # ValueError too (cubic round 2): a CLOSED stream raises
            # ValueError ('I/O operation on closed file'), not OSError.
            pass
    else:
        try:
            _restore_sync_state(db_path, slug, snapshot)
        except Exception as exc:  # restoration is best-effort containment
            try:
                _log(f"staleness refresh state restore for {slug} failed: {exc}")
            except (OSError, ValueError):
                pass


def _isolated_refresh_runner(inner):
    """Wrap one (db_path, slug) runner with refresh isolation (CF-B).

    Snapshot the store's sync_state row before the run; on ANY failure
    propagating out of the inner runner, restore that snapshot and arm
    the backoff, then re-raise so the job registry still records the
    failure (the honesty reporting reads that recorded error). The
    two-argument runner seam is preserved: the wrapper is called with
    exactly (db_path, slug) and calls the inner runner the same way."""

    def _runner(db_path, slug) -> None:
        snapshot = _snapshot_sync_state(db_path, slug)
        try:
            inner(db_path, slug)
        except BaseException:
            _contain_refresh_failure(db_path, slug, snapshot)
            raise
        # A successful refresh ends the failure streak: the backoff
        # tracks consecutive failures, not this store's history.
        with _BACKOFF_LOCK:
            _REFRESH_BACKOFF.pop((str(db_path), slug), None)

    return _runner


#: The production staleness-kick runner: the real background sync under
#: refresh isolation. _read_envelope installs this when the session uses
#: the default runner; the explicit `sync` tool keeps the PLAIN runner
#: (normal error recording — a deliberate sync reports its own failure).
_stale_refresh_runner = _isolated_refresh_runner(_default_sync_runner)


def _refresh_suppressed_reason(db_path, slug: str) -> str | None:
    """Why a stale read must not re-arm a refresh right now, or None when
    a refresh may start (no recorded failure, or the backoff window has
    elapsed). The reason names the job registry's recorded error when it
    still holds the failed job (the settled-jobs bound prunes old ones)."""
    with _BACKOFF_LOCK:
        failed_at = _REFRESH_BACKOFF.get((str(db_path), slug))
    if failed_at is None:
        return None
    remaining = failed_at + _refresh_backoff_s() - time.monotonic()
    if remaining <= 0:
        return None
    recorded: str | None = None
    for job in _jobs_snapshot(str(db_path)):
        if job.get("repo") == slug and job.get("state") == "failed":
            recorded = job.get("error") or recorded  # latest failure wins
    detail = f" ({recorded})" if recorded else ""
    return (
        "background refresh failed"
        f"{detail}; retry suppressed for {int(remaining) + 1}s "
        f"({_REFRESH_BACKOFF_ENV})"
    )


def _default_stale_after_s() -> int:
    raw = os.environ.get(_STALE_ENV)
    if not raw:
        return _DEFAULT_STALE_AFTER_S
    try:
        value = int(raw)
    except ValueError:
        _log(f"ignoring non-integer {_STALE_ENV}={raw!r}")
        return _DEFAULT_STALE_AFTER_S
    return value if value >= 0 else _DEFAULT_STALE_AFTER_S


# ---------------------------------------------------------------------------
# Resolution (repo argument > roots > server cwd), then the CLI guard stack


def _root_paths(roots: list) -> list[Path]:
    """file:// URIs to local directories; everything else is skipped.

    urlparse + url2pathname is the 3.11/3.12-safe parse for Windows
    file:///E:/... URIs (Path.from_uri is 3.13+)."""
    out: list[Path] = []
    for entry in roots:
        if not isinstance(entry, dict):
            continue
        uri = entry.get("uri")
        if not isinstance(uri, str):
            continue
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme != "file":
            continue
        out.append(Path(urllib.request.url2pathname(parsed.path)))
    return out


def _roots_slugs(ctx) -> list[tuple[str, str]]:
    """Distinct (host, slug) pairs from the client's roots, in reply order.

    Sends one roots/list request and reads lines until the matching reply
    arrives; a result error, empty roots, unresolvable roots, or EOF all
    degrade to no roots (the caller falls through to the server cwd)."""
    ctx.server_seq += 1
    req_id = f"srv-{ctx.server_seq}"
    _send(ctx.stdout, {"jsonrpc": "2.0", "id": req_id, "method": "roots/list"})
    while True:
        line = ctx.stdin.readline()
        if line == "":
            raise ToolError(
                "bad_request",
                "client closed the connection during roots resolution",
                hint="pass an explicit repo argument to avoid the roots round-trip",
            )
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            _error(ctx.stdout, None, _JSONRPC_PARSE_ERROR, "Parse error (line is not JSON)")
            continue
        if not isinstance(msg, dict):
            _error(ctx.stdout, None, _JSONRPC_INVALID_REQUEST, "Invalid Request (line is not an object)")
            continue
        if msg.get("id") != req_id:
            if "method" in msg:
                # A pipelined client request or notification: never drop
                # it - buffer and process after this handshake.
                ctx.deferred.append(msg)
            continue
        result = msg.get("result")
        if not isinstance(result, dict):
            return []  # client answered with an error: no roots
        pairs: list[tuple[str, str]] = []
        for path in _root_paths(result.get("roots") or []):
            try:
                host, slug = remote_info(path)
            except RepoError:
                continue  # a root that is not a git checkout is not fatal
            pair = (host, slug)
            if pair not in pairs:
                pairs.append(pair)
        return pairs


def _resolve_target(arguments: dict, ctx) -> tuple[str, Path]:
    """(slug, db_path) for a tool call; raises ToolError on failure.

    Resolution order: explicit repo argument > the server --repo pin >
    MCP roots (only for a session whose client declared the roots
    capability at initialize — issue #71 C4) > the server cwd's git
    origin — then the same store-first resolve_db chain the CLI uses, so
    a linked worktree and its main checkout resolve one DB and reads
    never create. The database itself is operator-pinned (--db /
    ZAXBYGRAPH_DB); tool calls cannot choose it. On the modern
    (2026-07-28) path the roots round-trip never runs: a repo-less,
    un-pinned call raises _InputRequired so the dispatcher answers an
    InputRequiredResult naming repo."""
    repo_arg = arguments.get("repo")
    if repo_arg is None:
        repo_arg = ctx.default_repo
    # The per-call `db` tool argument was removed (review round 2): a
    # model-reachable path is a filesystem oracle and, via the sync tool,
    # an arbitrary directory-creation primitive. The database is pinned
    # by the OPERATOR (server --db / ZAXBYGRAPH_DB), never by a call.
    db_arg = ctx.default_db
    if repo_arg is not None:
        try:
            slug = validate_slug(str(repo_arg))
        except RepoError as exc:
            raise ToolError(
                "bad_request", f"invalid repo argument ({exc})", hint="pass repo as OWNER/REPO"
            ) from exc
        host: str | None = DEFAULT_HOST
    elif getattr(ctx, "modern", False):
        # The modern era dropped Roots: never send a server-initiated
        # roots/list. An unresolved repository becomes an InputRequiredResult
        # at the dispatch layer.
        raise _InputRequired()
    else:
        # Capability-honest roots (issue #71 C4 / plan CF-C): the
        # roots/list round-trip runs ONLY for a client that declared the
        # roots capability at initialize (recorded on ctx there). An
        # undeclared client never receives a server-initiated request —
        # resolution falls through to the server cwd's git origin, and a
        # repo-less call that resolves nothing answers the existing
        # structured bad_request naming repo.
        pairs: list[tuple[str, str]] = []
        if ctx.roots_declared:
            pairs = _roots_slugs(ctx)
            if len(pairs) > 1:
                names = ", ".join(sorted(slug for _h, slug in pairs))
                raise ToolError(
                    "bad_request",
                    f"roots span multiple repos ({names})",
                    hint="pass an explicit repo argument naming one of them",
                )
        if pairs:
            host, slug = pairs[0]
        else:
            try:
                host, slug = remote_info(ctx.root)
            except RepoError as exc:
                raise ToolError(
                    "bad_request",
                    f"could not determine repo ({exc})",
                    hint="pass a repo argument or run the server from a git checkout",
                ) from exc
    try:
        db_path, _chain = resolve_db(slug, cwd=ctx.root, explicit=db_arg, host=host)
    except RepoError as exc:
        raise ToolError(
            "bad_request",
            str(exc),
            hint="pass a repo argument with --db spelling",
            slug=slug,
        ) from exc
    return slug, db_path


def _open_corpus(slug: str, db_path: Path) -> sqlite3.Connection:
    """Read-only connection with the CLI's corpus guards, mapped to
    ToolError (never creates a file or directory)."""
    try:
        conn = open_existing(db_path)
    except FileNotFoundError:
        raise ToolError(
            "no_corpus",
            f"no graph for {slug}; resolved database: {db_path}",
            hint=f"run: zaxbygraph sync --repo {slug}",
            slug=slug,
            db_path=db_path,
        ) from None
    except RuntimeError as exc:
        # Environment guards (SQLite floor, FTS5 probe) get a coded
        # ToolError like every other fault, not the session's generic
        # internal-error path (PR #47 review).
        raise ToolError(
            "runtime",
            str(exc),
            hint="the environment guard rejected this store open",
            slug=slug,
            db_path=db_path,
        ) from None
    try:
        try:
            row = conn.execute(
                "SELECT repo FROM sync_state WHERE lower(repo) = ?", (slug,)
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ToolError(
                "runtime",
                f"{db_path} is not a zaxbygraph database: {exc}",
                hint="run zaxbygraph sync to create or repair the graph",
                slug=slug,
                db_path=db_path,
            ) from exc
        if row is not None:
            if row["repo"] != slug:
                raise ToolError(
                    "bad_request",
                    f"no corpus for {slug} in {db_path}; this database holds "
                    f"{row['repo']} - the same repo with pre-fold casing; "
                    "run: zaxbygraph doctor --consolidate",
                    hint=f"run: zaxbygraph doctor --consolidate --repo {slug}",
                    slug=slug,
                    db_path=db_path,
                )
            # Issue #72 C10 (plan CF-E): the ONE shared no-corpus decision —
            # a failed FIRST sync leaves a sync_state row with zero items and
            # no completed full sync; reads must answer no_corpus, not
            # empty-success. Live count, never the cached item_count.
            live_items, last_full_sync_at = corpus_state(conn, slug)
            if live_items == 0 and last_full_sync_at is None:
                raise ToolError(
                    "no_corpus",
                    f"no corpus for {slug} in {db_path}; zero items and no "
                    "completed full sync (a failed first sync is still no "
                    "corpus)",
                    hint=f"run: zaxbygraph sync --repo {slug}",
                    slug=slug,
                    db_path=db_path,
                )
        else:
            others = [
                r["repo"] for r in conn.execute("SELECT repo FROM sync_state ORDER BY repo")
            ]
            if others:
                raise ToolError(
                    "bad_request",
                    f"no corpus for {slug} in {db_path}; this database holds: "
                    + ", ".join(others),
                    hint="pass a repo argument naming one of the held repos",
                    slug=slug,
                    db_path=db_path,
                )
            raise ToolError(
                "no_corpus",
                f"no graph for {slug}; resolved database: {db_path}",
                hint=f"run: zaxbygraph sync --repo {slug}",
                slug=slug,
                db_path=db_path,
            )
    except BaseException:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# Envelope assembly


def _error_envelope(code_key: str, message: str, hint: str | None, slug, db_path) -> dict:
    error: dict = {"code": code_key, "message": message}
    if hint:
        error["hint"] = hint
    return build_envelope(None, slug=slug, db_path=db_path, error=error)


def _read_envelope(conn, slug, db_path, data, truncated, ctx) -> dict:
    """Envelope for a successful read, with the staleness refresh kick.

    A stale read (age_s > stale_after_s) answers from current data and
    starts at most one background sync; freshness.refreshing is true when
    a refresh was started or is already in flight (a joined read is
    genuinely refreshing too — preserving C9). The kicked runner runs
    under CF-B refresh isolation (snapshot-restore + failure backoff);
    the explicit `sync` tool keeps the plain runner. Only a
    backoff-suppressed re-arm leaves refreshing unset and carries
    freshness.refresh_failed naming the recorded failure. Failed reads
    never reach this function, so a no-corpus call never kicks a refresh
    (and never creates anything)."""
    fresh = _freshness(conn, slug)
    age = fresh.get("age_s")
    if age is not None and age > ctx.stale_after_s:
        suppressed = _refresh_suppressed_reason(db_path, slug)
        if suppressed is not None:
            # A refresh failed recently for this store: do not storm it.
            # Honest about it — no refreshing claim, and the recorded
            # failure is surfaced (issue #71 C3 / plan CF-B).
            fresh["refresh_failed"] = suppressed
        else:
            if ctx.sync_runner is _default_sync_runner:
                kick_runner = _stale_refresh_runner
            else:
                # Injected seam (tests): the SAME isolation wrapped around
                # the injected body, so the contract holds either way.
                kick_runner = _isolated_refresh_runner(ctx.sync_runner)
            try:
                # started OR joined-in-flight (an external lock holder is
                # a genuinely running sync too): refreshing is true.
                _start_job(db_path, slug, kick_runner)
                fresh["refreshing"] = True
            except Exception as exc:
                _log(f"staleness refresh for {slug} could not start: {exc}")
    return build_envelope(
        data, conn=conn, slug=slug, db_path=db_path, truncated=truncated, freshness=fresh
    )


def _int_arg(arguments: dict, key: str, default: int) -> int:
    """A typed optional integer argument; a wrong type is a structured
    bad_request, never a protocol-frame internal error."""
    value = arguments.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(
            "bad_request",
            f"{key} must be an integer",
            hint=f"pass {key} as an integer",
        )
    return value


def _bool_arg(arguments: dict, key: str) -> bool:
    """A strict optional boolean argument: absent/None is False, a real
    bool passes, anything else is a structured bad_request (the string
    "false" must never silently mean True)."""
    value = arguments.get(key)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ToolError(
        "bad_request",
        f"{key} must be a boolean",
        hint=f"pass {key} as true or false",
    )


def _int_cursor(raw, tool: str) -> int:
    """A paging tool's opaque cursor, validated symmetrically with the
    producer: search/file_history/open_items mint next_cursor as
    str(offset) — a decimal integer string — and consume it as int(). A
    cursor that is not a string, or a string that does not decode as
    that integer shape, is a structured bad_request — never a silent
    page-1 reset (issue #71 C12). A huge-but-valid offset stays clamped
    to _MAX_CURSOR."""
    if raw is None:
        return 0
    if not isinstance(raw, str):
        raise ToolError(
            "bad_request",
            f"{tool} cursor must be a string",
            hint="pass the next_cursor value from the previous page verbatim",
        )
    try:
        value = int(raw)
    except ValueError:
        raise ToolError(
            "bad_request",
            f"{tool} cursor is malformed: {raw!r}",
            hint="pass the next_cursor value from the previous page verbatim",
        ) from None
    if value < 0:
        # The producer mints only non-negative offsets: a negative cursor is
        # hand-crafted input, answered as bad_request rather than the silent
        # page-1 reset this function's own docstring forbids (FB-010).
        raise ToolError(
            "bad_request",
            f"{tool} cursor is malformed: {raw!r}",
            hint="pass the next_cursor value from the previous page verbatim",
        )
    if value == 0:
        return 0
    return min(value, _MAX_CURSOR)


def _item_number(arguments: dict, tool: str) -> int:
    """A typed item number in SQLite's signed-64-bit range: a wrong type or
    an out-of-range magnitude is a structured bad_request, never a raw
    sqlite bind OverflowError surfacing as runtime (pr49 review PRR-013)."""
    number = arguments.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        raise ToolError(
            "bad_request", f"{tool} needs an integer number",
            hint=f'try {tool} {{"number": 12}}',
        )
    if not 1 <= number <= 2**63 - 1:
        raise ToolError(
            "bad_request", f"{tool} number is out of range",
            hint="pass the item number as a positive integer",
        )
    return number


# ---------------------------------------------------------------------------
# Tool handlers: (arguments, ctx) -> (envelope dict)

def _tool_graph_status(arguments: dict, ctx) -> dict:
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        data = status(conn, slug)
        observer = read_lock_observer(db_path)
        if observer is not None:
            data["sync_lock"] = observer
        jobs = _jobs_snapshot(str(db_path))
        if jobs:
            data["jobs"] = jobs
        return _read_envelope(conn, slug, db_path, data, False, ctx)
    finally:
        conn.close()


def _tool_search(arguments: dict, ctx) -> dict:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("bad_request", "search needs a non-empty query", hint='try search {"query": "watermark"}')
    exclude_bots = _bool_arg(arguments, "exclude_bots")
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        limit = _clamp_limit(min(_int_arg(arguments, "limit", 20), _MAX_PAGE))
        offset = _int_cursor(arguments.get("cursor"), "search")
        # offset rides INTO the query (FB-006): search() stamps
        # data.offset/matched_mode/bots_omitted against the page it
        # computed, so cursor pages must ask for their own window — the
        # old fetch-wide-then-slice answered metadata from page 0 on
        # every cursor page.
        try:
            result = search(
                conn, query, limit=limit, offset=offset, repo=slug,
                exclude_bots=exclude_bots,
            )
        except ValueError as exc:
            # The pre-v12 bot-flag gate (FB-004): a structured refusal
            # naming the sync remedy, not the runtime catch-all.
            raise ToolError(
                "bad_request", str(exc),
                hint="run: zaxbygraph sync --repo <repo> to upgrade the store",
                slug=slug, db_path=db_path,
            ) from None
        total = result.get("total_matches")
        # The query layer's own limit-independent truncation evidence (page
        # < matched, issue #72 C14) promotes into the envelope flag here.
        data_truncated = bool(result.pop("truncated", False))
        # Truncation is judged against the source's own match count, not
        # the capped fetch — via the same honest page_truncated formula the
        # CLI search uses (08b review Q3: an offset PAST the end skipped
        # matches the caller never saw and must not read as "everything
        # shown") — AND a full page always offers a cursor: the AND-to-OR
        # fallback can reveal more matches at a wider window, so "page
        # length == limit" alone must not read as "the end" (review F10).
        truncated = page_truncated(offset, len(result["items"]), limit, total)
        if len(result["items"]) == limit:
            truncated = True
        truncated = truncated or data_truncated
        if truncated and result["items"] and offset + len(result["items"]) <= _MAX_CURSOR:
            # An overshoot page (offset past the end) reports truncated:true
            # with NO next_cursor: repeating str(offset) would trap any
            # while-truncated cursor follower in a self-loop (08b r2). A
            # mint past the _MAX_CURSOR clamp is the same live-loop at the
            # boundary — the follower would clamp back to the same window
            # forever (FB-011) — so the ceiling page is terminal too.
            result["next_cursor"] = str(offset + len(result["items"]))
        return _read_envelope(conn, slug, db_path, result, truncated, ctx)
    finally:
        conn.close()


def _tool_get_item(arguments: dict, ctx) -> dict:
    number = _item_number(arguments, "get_item")
    max_body_chars = _int_arg(arguments, "max_body_chars", None)
    max_comments = _int_arg(arguments, "max_comments", None)
    max_reviews = _int_arg(arguments, "max_reviews", None)
    for name, value in (
        ("max_body_chars", max_body_chars),
        ("max_comments", max_comments),
        ("max_reviews", max_reviews),
    ):
        if value is not None and value < 0:
            raise ToolError(
                "bad_request",
                f"{name} must be a non-negative integer",
                hint=f"pass {name} as 0 or a positive integer",
            )
    exclude_bots = _bool_arg(arguments, "exclude_bots")
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        try:
            data = item(conn, number, repo=slug, exclude_bots=exclude_bots)
        except ValueError as exc:
            # The pre-v12 bot-flag gate (FB-004): a structured refusal
            # naming the sync remedy, matching _tool_search — not the
            # runtime catch-all with a stderr traceback.
            raise ToolError(
                'bad_request', str(exc),
                hint='run: zaxbygraph sync --repo <repo> to upgrade the store',
                slug=slug, db_path=db_path,
            ) from None
        if data is None:
            raise ToolError(
                "not_found",
                f"item #{number} not found",
                hint=f"try graph_status or search first; or run: zaxbygraph sync --repo {slug}",
                slug=slug,
                db_path=db_path,
            )
        # An explicit max_body_chars keeps its cap semantics — 0 is the
        # explicit UNCAPPED override on both adapters (08b review Q2);
        # absent flags fall to the bounded defaults (issue #71 C5 / plan
        # CF-D), shared with the CLI item command via query.apply_item_bounds.
        truncated = _truncate_bodies(data, max_body_chars)
        truncated = apply_item_bounds(
            data,
            slug,
            max_body_chars=max_body_chars,
            max_comments=max_comments,
            max_reviews=max_reviews,
        ) or truncated
        return _read_envelope(conn, slug, db_path, data, truncated, ctx)
    finally:
        conn.close()


def _tool_related(arguments: dict, ctx) -> dict:
    number = _item_number(arguments, "related")
    depth = _int_arg(arguments, "depth", 1)
    if depth < 1:
        raise ToolError(
            "bad_request", "depth must be at least 1", hint='try related {"number": 12, "depth": 2}'
        )
    if depth > _MAX_DEPTH:
        raise ToolError(
            "bad_request",
            f"depth is capped at {_MAX_DEPTH}",
            hint="each hop is a full graph expansion on a serialized server",
        )
    max_nodes = _int_arg(arguments, "max_nodes", 100)
    if max_nodes < 1:
        raise ToolError(
            "bad_request",
            "max_nodes must be at least 1",
            hint="pass max_nodes as a positive integer",
        )
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        data = related(
            conn,
            number,
            depth=depth,
            repo=slug,
            max_nodes=min(max_nodes, _MAX_PAGE),
        )
        # The bounded expansion reports its own truncation evidence (issue
        # #71 C7); promote it to the envelope like the CLI read path.
        truncated = bool(data.pop("truncated", False))
        return _read_envelope(conn, slug, db_path, data, truncated, ctx)
    except LookupError as exc:
        # A ghost number is the caller's not_found (issue #72 AC2, 08b
        # review finding 2), not an internal error — exactly like the CLI
        # read path and _tool_what_closed.
        raise ToolError("not_found", str(exc), hint="run graph_status or search to list stored items", slug=slug, db_path=db_path) from exc
    finally:
        conn.close()


def _tool_path(arguments: dict, ctx) -> dict:
    a = arguments.get("a")
    b = arguments.get("b")
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        raise ToolError("bad_request", "path needs string endpoints a and b", hint='try path {"a": "12", "b": "src/cli.py"}')
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        data = path_between(conn, a, b, repo=slug)
        return _read_envelope(conn, slug, db_path, data, False, ctx)
    except LookupError as exc:
        # A ghost endpoint number is not_found (issue #72 AC2, 08b review
        # finding 2) — same mapping as the CLI read path and what_closed.
        raise ToolError("not_found", str(exc), hint="run graph_status or search to list stored items", slug=slug, db_path=db_path) from exc
    finally:
        conn.close()


def _tool_pr_overlap(arguments: dict, ctx) -> dict:
    numbers = arguments.get("numbers")
    if not isinstance(numbers, list) or not all(
        isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 2**63 - 1
        for n in numbers
    ):
        raise ToolError(
            "bad_request",
            "pr_overlap needs numbers as a list of integers",
            hint='try pr_overlap {"numbers": [10, 11]}',
        )
    if len(numbers) < 2:
        raise ToolError(
            "bad_request",
            "pr_overlap needs at least two numbers to form a pair",
            hint='try pr_overlap {"numbers": [10, 11]}',
        )
    if len(numbers) > _MAX_NUMBERS:
        raise ToolError(
            "bad_request",
            f"pr_overlap accepts at most {_MAX_NUMBERS} numbers",
            hint="the pair count grows quadratically; split the request",
        )
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        data = pr_overlap(conn, numbers, repo=slug)
        return _read_envelope(conn, slug, db_path, data, False, ctx)
    except LookupError as exc:
        raise ToolError("not_found", str(exc), hint="run graph_status or search to list stored items", slug=slug, db_path=db_path) from exc
    finally:
        conn.close()


def _tool_file_history(arguments: dict, ctx) -> dict:
    path = arguments.get("path")
    if not isinstance(path, str) or not path:
        raise ToolError("bad_request", "file_history needs a path", hint='try file_history {"path": "src/cli.py"}')
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        limit = _clamp_limit(min(_int_arg(arguments, "limit", 30), _MAX_PAGE))
        offset = _int_cursor(arguments.get("cursor"), "file_history")
        # AC2 gate shared with the CLI (08b review finding 3): backslashes
        # normalize, then the lookup is EXACT-CASE against the stored
        # pr_files vocabulary. A miss is unknown_path with the suggestion
        # hint — never ok:true with entries:[].
        try:
            normalized = resolve_file_history_path(conn, slug, path)
        except UnknownFilePathError as exc:
            raise ToolError(
                "unknown_path",
                exc.message,
                hint=exc.hint,
                slug=slug,
                db_path=db_path,
            ) from exc
        result = file_history(conn, normalized, limit=limit + offset, repo=slug)
        entries = result.get("entries") or []
        result["entries"] = entries[offset : offset + limit]
        # file_history over-fetches limit+1 at the window size, so its own
        # truncated flag is the post-offset more-exist evidence. Promote
        # (and remove) it for shape parity with the file-history command.
        truncated = bool(result.pop("truncated", False))
        if truncated and offset + len(result["entries"]) <= _MAX_CURSOR:
            # A mint past the _MAX_CURSOR clamp live-loops the follower
            # (FB-011, same boundary shape as search's) — terminal page.
            result["next_cursor"] = str(offset + len(result["entries"]))
        return _read_envelope(conn, slug, db_path, result, truncated, ctx)
    finally:
        conn.close()


def _tool_what_closed(arguments: dict, ctx) -> dict:
    number = _item_number(arguments, "what_closed")
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        data = what_closed(conn, number, repo=slug)
        return _read_envelope(conn, slug, db_path, data, False, ctx)
    except LookupError as exc:
        raise ToolError("not_found", str(exc), hint="run graph_status or search to list stored items", slug=slug, db_path=db_path) from exc
    finally:
        conn.close()


def _tool_open_items(arguments: dict, ctx) -> dict:
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        limit = _clamp_limit(min(_int_arg(arguments, "limit", 50), _MAX_PAGE))
        offset = _int_cursor(arguments.get("cursor"), "open_items")
        # SQL-level paging: the fetch is bounded where the cost is (F1).
        rows = open_items(conn, repo=slug, limit=limit + 1, offset=offset)
        truncated = len(rows) > limit
        page = rows[:limit]
        data: dict = {"items": page}
        if truncated and offset + len(page) <= _MAX_CURSOR:
            # Terminal at the clamp ceiling (FB-011, same as search).
            data["next_cursor"] = str(offset + len(page))
        return _read_envelope(conn, slug, db_path, data, truncated, ctx)
    finally:
        conn.close()


#: sqlite error-message classifier for the sql tool's error mapping
#: (issue #71 AC11): an unknown table gets the store's real table list as
#: the hint. The error CODE stays exactly as today (`runtime` at this
#: surface) — only the hint key is added, mirroring the CLI's cmd_sql.
_NO_SUCH_TABLE_RE = re.compile(r"^no such table", re.IGNORECASE)


def _tool_sql(arguments: dict, ctx) -> dict:
    statement = arguments.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        raise ToolError("bad_request", "sql needs a statement", hint='try sql {"statement": "SELECT number, title FROM items LIMIT 5"}')
    slug, db_path = _resolve_target(arguments, ctx)
    # Same two independent guard layers as the CLI sql command: the
    # statement guard here, the connection authorizer inside
    # connect_readonly_query (AGENTS.md read-only escape hatch).
    try:
        assert_read_sql(statement)
    except ValueError as exc:
        raise ToolError(
            "bad_sql",
            str(exc),
            hint="only one SELECT/WITH/EXPLAIN statement per call; "
            "see `zaxbygraph schema [TABLE]` for the store's tables and columns",
        ) from exc
    conn = _open_corpus(slug, db_path)
    try:
        guarded = connect_readonly_query(db_path)
    except FileNotFoundError:
        conn.close()
        raise ToolError(
            "no_corpus",
            f"no graph for {slug}; resolved database: {db_path}",
            hint=f"run: zaxbygraph sync --repo {slug}",
            slug=slug,
            db_path=db_path,
        ) from None
    except RuntimeError as exc:
        raise ToolError(
            "runtime",
            str(exc),
            hint="the environment guard rejected this store open",
            slug=slug,
            db_path=db_path,
        ) from None
    finally:
        conn.close()
    limit = _clamp_limit(min(_int_arg(arguments, "limit", 200), _MAX_PAGE))
    try:
        data = run_sql(
            guarded,
            statement,
            limit=limit,
            repo=slug,
            deadline=sql_deadline_from_env(),
            max_bytes=sql_max_bytes_from_env(),
        )
    except SqlTimeoutError as exc:
        # A deadline expiry is its own coded failure (issue #71 AC1) —
        # `timeout`, never `runtime` — so a caller narrows the statement
        # or widens ZAXBYGRAPH_SQL_TIMEOUT_S instead of retrying blind.
        guarded.close()
        raise ToolError(
            "timeout",
            str(exc),
            hint="narrow the statement (LIMIT, tighter predicates) or raise "
            "ZAXBYGRAPH_SQL_TIMEOUT_S (clamped to (0, 600])",
            slug=slug,
            db_path=db_path,
        ) from exc
    except ValueError as exc:
        # run_sql converts execution-time sqlite failures (no such
        # column/table, malformed expressions) to ValueError — the single
        # most common failure of an sql tool. It must answer as the
        # envelope error object, never escape the protocol frame. The code
        # stays `runtime` exactly as before; only the hint is enriched
        # (issue #71 AC11: an unknown table names the store's real tables).
        message = str(exc)
        if _NO_SUCH_TABLE_RE.match(message):
            hint = store_tables_hint(guarded) or (
                "check table names with the zaxbygraph://schema resource"
            )
        else:
            hint = "check column names with the zaxbygraph://schema resource"
        guarded.close()
        raise ToolError(
            "runtime",
            message,
            hint=hint,
            slug=slug,
            db_path=db_path,
        ) from exc
    except sqlite3.Error as exc:  # defensive: run_sql converts these today
        guarded.close()
        if "interrupted" in str(exc).lower():
            raise ToolError(
                "timeout",
                str(exc),
                hint="narrow the statement (LIMIT, tighter predicates) or raise "
                "ZAXBYGRAPH_SQL_TIMEOUT_S (clamped to (0, 600])",
                slug=slug,
                db_path=db_path,
            ) from exc
        raise ToolError(
            "runtime",
            str(exc),
            hint="check column names with the zaxbygraph://schema resource",
            slug=slug,
            db_path=db_path,
        ) from exc
    # run_sql's own shape: positional row arrays (a deliberate divergence
    # from the CLI's objects default — consumers index columns
    # positionally alongside the returned columns list). Same staleness
    # contract as every other read tool (issue AC5).
    try:
        return _read_envelope(guarded, slug, db_path, data, bool(data.get("truncated")), ctx)
    finally:
        guarded.close()


def _tool_sync(arguments: dict, ctx) -> dict:
    slug, db_path = _resolve_target(arguments, ctx)
    # The sync tool is the one explicit write surface: it may create what
    # a sync would create (cmd_sync's connect() does the same mkdir).
    db_path.parent.mkdir(parents=True, exist_ok=True)
    runner = ctx.sync_runner
    force = _bool_arg(arguments, "force")
    include_patches = _bool_arg(arguments, "include_patches")
    source = arguments.get("source")
    if source is not None and source not in ("graphql", "rest"):
        raise ToolError(
            "bad_request",
            "source must be 'graphql' or 'rest'",
            hint='e.g. "source": "rest"',
        )
    numbers_raw = arguments.get("numbers")
    numbers: list[int] | None = None
    if numbers_raw is not None:
        if force:
            # Mirror the CLI's own cross-flag contract (cmd_sync rejects
            # the pair outright; sync_repo would silently drop force —
            # pr49 review PRR-012).
            raise ToolError(
                "bad_request",
                "force cannot be combined with numbers",
                hint="a targeted refresh never touches the watermark; drop force",
            )
        if not isinstance(numbers_raw, list) or not numbers_raw or not all(
            isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in numbers_raw
        ):
            raise ToolError(
                "bad_request",
                "numbers must be a non-empty list of positive integers",
                hint='e.g. "numbers": [12, 34]',
            )
        if len(numbers_raw) > _MAX_NUMBERS:
            # Same ceiling as pr_overlap (pr49 review PRR-012): client-chosen
            # numbers drive per-item fetch work on the operator's credential.
            raise ToolError(
                "bad_request",
                f"numbers is capped at {_MAX_NUMBERS} items",
                hint=f"refresh at most {_MAX_NUMBERS} items per call",
            )
        numbers = numbers_raw
    if runner is _default_sync_runner and (
        force or include_patches or numbers or source
    ):
        # Advertised arguments must actually reach sync_repo; the injected
        # test seam keeps its pinned (db_path, repo) signature.
        def runner(db_path_, repo_, _force=force, _patches=include_patches,
                   _numbers=numbers, _source=source):
            return _default_sync_runner(
                db_path_, repo_, force=_force, include_patches=_patches,
                numbers=_numbers, source=_source,
            )
    try:
        job_id, started = _start_job(db_path, slug, runner)
    except Exception as exc:
        raise ToolError(
            "runtime", f"sync could not start: {exc}", hint="check the database directory permissions"
        ) from exc
    if started:
        data = {"job": job_id, "started": True}
    else:
        data = {"joined": True, "job": job_id}
    return build_envelope(data, conn=None, slug=slug, db_path=db_path)


# ---------------------------------------------------------------------------
# Derived/work-selection tools (issues #29/#30 surfaces; #32 exposes them)

def _translated_derive(fn, conn, slug: str, db_path: Path, /, *args) -> dict:
    """Run one CLI derived-read wrapper and translate its exceptions into
    the CLI's coded vocabulary — the same mapping _run_derived_read applies
    (LookupError -> not_found, ConfigError -> config_error, ValueError ->
    bad_request, sqlite -> store_unreadable), preserving slug/db identity.
    ConfigError precedes ValueError: it IS a ValueError."""
    try:
        return fn(conn, *args, repo=slug, db_path=db_path)
    except LookupError as exc:
        raise ToolError(
            "not_found", str(exc), hint="pass an existing item number",
            slug=slug, db_path=db_path,
        ) from exc
    except DeriveConfigError as exc:
        message = str(exc) + (f" (config file: {exc.path})" if exc.path else "")
        raise ToolError(
            "config_error", message, hint="fix the derive weight configuration",
            slug=slug, db_path=db_path,
        ) from exc
    except ValueError as exc:
        raise ToolError(
            "bad_request", str(exc), hint="fix the tool arguments",
            slug=slug, db_path=db_path,
        ) from exc
    except sqlite3.Error as exc:
        raise ToolError(
            "store_unreadable", f"store unreadable: {exc}",
            hint="check the database file", slug=slug, db_path=db_path,
        ) from exc


def _int_array_csv(arguments: dict, key: str) -> str | None:
    """A strict optional int-array tool argument, carried to the query layer
    in the CLI's canonical CSV text form (never str(list): the wrapper's
    _csv_numbers parses CSV, and "[1, 2]" would raise)."""
    values = arguments.get(key)
    if values is None:
        return None
    if not isinstance(values, list) or any(
        isinstance(v, bool) or not isinstance(v, int) for v in values
    ):
        raise ToolError(
            "bad_request", f"{key} must be an array of integers",
            hint=f"pass {key} as an array of integers",
        )
    if len(values) > _MAX_NUMBERS:
        raise ToolError(
            "bad_request",
            f"{key} is capped at {_MAX_NUMBERS} numbers (the pair fan grows "
            "quadratically; narrow the pool)",
            hint=f"pass at most {_MAX_NUMBERS} issue numbers",
        )
    return ",".join(str(v) for v in values)


def _tool_similar_items(arguments: dict, ctx) -> dict:
    # Validate BEFORE resolving (pr49 review PRR-020): identical malformed
    # calls answer bad_request uniformly, and identity is simply unknown at
    # validation time on every tool.
    number = _item_number(arguments, "similar_items")
    limit = _clamp_limit(min(_int_arg(arguments, "limit", 20), _MAX_PAGE))
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        data = _translated_derive(_similar_query, conn, slug, db_path, number, limit, None, False)
        return _read_envelope(conn, slug, db_path, data, False, ctx)
    finally:
        conn.close()


def _tool_triage(arguments: dict, ctx) -> dict:
    limit = _clamp_limit(min(_int_arg(arguments, "limit", 20), _MAX_PAGE))
    cursor = arguments.get("cursor")
    if cursor is not None:
        if not isinstance(cursor, str):
            raise ToolError(
                "bad_request", "cursor must be a string",
                hint="pass the cursor value from the previous triage page",
            )
        # Validated symmetrically with the producer (derive._cursor_key
        # mints "%.9f:%d" and derive.triage parses total:number halves):
        # a malformed cursor is a structured bad_request here, never a
        # silent page-1 reset (issue #71 C12). A well-formed but stale
        # cursor still restarts from the top inside derive (the
        # documented restart), which is honest — the shape is valid.
        total_text, _, number_text = cursor.partition(":")
        try:
            int(number_text)
            total = float(total_text)
        except ValueError:
            raise ToolError(
                "bad_request", f"cursor is malformed: {cursor!r}",
                hint="pass the cursor value from the previous triage page verbatim",
            ) from None
        if total != total or total in (float("inf"), float("-inf")):
            # nan/inf parse as floats but the producer never mints them
            # (FB-012): accepting them restarted pagination at page one.
            raise ToolError(
                "bad_request", f"cursor is malformed: {cursor!r}",
                hint="pass the cursor value from the previous triage page verbatim",
            )
    top_k = arguments.get("top_k")
    if top_k is not None:
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise ToolError("bad_request", "top_k must be an integer", hint="pass top_k as an integer")
        top_k = min(top_k, _MAX_TOP_K)
    include_gated = _bool_arg(arguments, "include_gated")
    # Validated symmetrically with _tool_bundle: the declared as_of argument
    # is a string or absent, and it rides the same positional chain into
    # _triage_query (derive.triage parses/validates the instant itself).
    as_of = arguments.get("as_of")
    if as_of is not None and not isinstance(as_of, str):
        raise ToolError("bad_request", "as_of must be a string", hint="pass an ISO-8601 Z instant like 2027-01-05T00:00:00Z")
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        data = _translated_derive(
            _triage_query, conn, slug, db_path, limit, cursor, top_k, include_gated,
            None, as_of,
        )
        return _read_envelope(conn, slug, db_path, data, False, ctx)
    finally:
        conn.close()


def _tool_bundle(arguments: dict, ctx) -> dict:
    size = arguments.get("size")
    if size is not None and not isinstance(size, str):
        raise ToolError("bad_request", "size must be a string like '2-3'", hint="pass size as a string")
    count = _int_arg(arguments, "count", 5)
    # pool/exclude are capped like pr_overlap's numbers (pr49 review PRR-006):
    # the derive pair fan is quadratic in pool size, and an uncapped
    # client-chosen pool stalls the whole serialized server.
    pool = _int_array_csv(arguments, "pool")
    exclude = _int_array_csv(arguments, "exclude")
    allow_shared_files = _bool_arg(arguments, "allow_shared_files")
    max_files = arguments.get("max_files")
    if max_files is not None and (isinstance(max_files, bool) or not isinstance(max_files, int)):
        raise ToolError("bad_request", "max_files must be an integer", hint="pass max_files as an integer")
    budget_tokens = arguments.get("budget_tokens")
    if budget_tokens is not None and (isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int)):
        raise ToolError("bad_request", "budget_tokens must be an integer", hint="pass budget_tokens as an integer")
    as_of = arguments.get("as_of")
    if as_of is not None and not isinstance(as_of, str):
        raise ToolError("bad_request", "as_of must be a string", hint="pass an ISO-8601 Z instant like 2027-01-05T00:00:00Z")
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        data = _translated_derive(
            _bundle_query, conn, slug, db_path,
            size if size is not None else "2-3", count, pool, exclude,
            allow_shared_files, max_files, budget_tokens, None, as_of,
        )
        return _read_envelope(conn, slug, db_path, data, False, ctx)
    finally:
        conn.close()


def _tool_brief(arguments: dict, ctx) -> dict:
    number = _item_number(arguments, "brief")
    budget_tokens = _int_arg(arguments, "budget_tokens", 4000)
    live = _bool_arg(arguments, "live")
    exclude_bots = _bool_arg(arguments, "exclude_bots")
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        data = _translated_derive(
            _brief_query, conn, slug, db_path, number, budget_tokens, live,
            exclude_bots, None,
        )
        return _read_envelope(conn, slug, db_path, data, False, ctx)
    finally:
        conn.close()


def _tool_churn(arguments: dict, ctx) -> dict:
    limit = _clamp_limit(min(_int_arg(arguments, "limit", 30), _MAX_PAGE))
    merged_only = _bool_arg(arguments, "merged_only")
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        try:
            data = churn(conn, limit, merged_only=merged_only, repo=slug)
        except sqlite3.Error as exc:
            # churn skips _translated_derive (it is a query.py read, not a
            # derived wrapper) but carries the SAME coded vocabulary and
            # identity as the CLI's churn runner: a store fault is a
            # store_unreadable envelope, never the generic runtime path.
            raise ToolError(
                "store_unreadable", f"store unreadable: {exc}",
                hint="check the database file", slug=slug, db_path=db_path,
            ) from exc
        except ValueError as exc:
            raise ToolError(
                "bad_request", str(exc), hint="fix the tool arguments",
                slug=slug, db_path=db_path,
            ) from exc
        return _read_envelope(conn, slug, db_path, data, False, ctx)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool registry (the typed surface; order is documentation order)

def _schema(properties: dict, required: list[str] | None = None) -> dict:
    out: dict = {"type": "object", "properties": properties, "additionalProperties": True}
    if required:
        out["required"] = required
    return out


_REPO_DB = {
    "repo": {
        "type": "string",
        "description": "OWNER/REPO slug (default: the server --repo pin, then MCP roots for clients that declared the roots capability, then the server cwd git origin)",
    },
}
_LIMIT = {
    "type": "integer",
    "minimum": 1,
    "maximum": _MAX_PAGE,
    "description": f"Maximum rows to return (capped at {_MAX_PAGE})",
}
_CURSOR = {"type": "string", "description": "Opaque cursor from a previous truncated page"}

# ---------------------------------------------------------------------------
# Result shape registry (issue #72 C17/C23, plan CF-I/CF-L): ONE table
# describing every tool's real payload. tools/list derives each tool's
# outputSchema from it (_output_schema_for) and the CLI `schema --output
# COMMAND` prints the same shapes (CLI_OUTPUT_SHAPES) — one source of truth
# for both surfaces and the SKILL.md shape table.
#
# Honesty rules: `data_keys` names the payload's ALWAYS-present top-level
# keys (a bare-list payload's row fields); the `data` schema may add
# conditional keys (next_cursor, bots_omitted, ...) as non-required
# properties; nested row objects stay open (additionalProperties true)
# because store rows carry more columns than any schema should pin. Types
# use JSON Schema unions (["string", "null"]) where a value is genuinely
# nullable. The null alternative for `data` itself is added by
# _output_schema_for: failure envelopes carry data:null against the same
# schema.

#: envelope keys every result carries ("error" joins them on failures)
_ENVELOPE_KEYS = ("ok", "db", "repo", "freshness", "data", "truncated")

_STR = {"type": "string"}
_STR_NULL = {"type": ["string", "null"]}
_INT = {"type": "integer"}
_INT_NULL = {"type": ["integer", "null"]}
_BOOL = {"type": "boolean"}
_NUM_NULL = {"type": ["number", "null"]}
_OPEN_OBJ = {"type": "object", "additionalProperties": True}


def _open_objects(description: str | None = None) -> dict:
    """array of open row objects (row fields are named in the description)."""
    out: dict = {"type": "array", "items": dict(_OPEN_OBJ)}
    if description:
        out["description"] = description
    return out


def _props(fields: dict, required: tuple | None = None) -> dict:
    out: dict = {"type": "object", "properties": fields, "additionalProperties": True}
    if required:
        out["required"] = list(required)
    return out


_TOOL_SHAPES: dict[str, dict] = {
    "graph_status": {
        "cli": "status",
        "description": "Sync state and corpus coverage for the resolved"
        " repo: one sync_state row per held repo, grouped item counts,"
        " coverage, and snapshot provenance when the store was installed"
        " by snapshot pull.",
        "data_keys": ("counts", "coverage", "repos", "snapshot"),
        "data": _props(
            {
                "repos": _open_objects(
                    "sync_state rows: repo, item_count, issues_since"
                    " (watermark), last_error, full_sync_pending,"
                    " last_full_sync_at, last_incr_sync_at, complete, ..."
                ),
                "counts": _open_objects(
                    "grouped item counts: {repo, kind, state, c}"
                ),
                "coverage": dict(
                    _OPEN_OBJ, description="corpus coverage per stored section"
                ),
                "snapshot": {
                    "type": ["object", "null"],
                    "additionalProperties": True,
                    "description": "{publisher, manifest_sha256, pulled_at}"
                    " when the store came from snapshot pull; null otherwise",
                },
                "sync_lock": {
                    "type": ["object", "null"],
                    "additionalProperties": True,
                    "description": "present while another process holds the"
                    " sync lock",
                },
                "jobs": _open_objects("live sync jobs (present while one runs)"),
            },
            ("counts", "coverage", "repos", "snapshot"),
        ),
    },
    "search": {
        "cli": "search",
        "description": "Ranked matches with limit-independent counts"
        " (strict_matches/broadened_matches/total_matches never change with"
        " the page size); comment and review hits merge into their item.",
        "data_keys": (
            "broadened_matches", "corpus_items", "index_stale", "items",
            "matched_mode", "offset", "strict_matches", "total_matches",
        ),
        "row_fields": {
            "items": (
                "author", "comment_snippet", "html_url", "kind",
                "matching_comments", "matching_reviews", "number", "repo",
                "review_snippet", "snippet", "state", "title", "updated_at",
            ),
        },
        "data": _props(
            {
                "items": _open_objects(
                    "hit rows: {repo, number, kind, title, state, author,"
                    " updated_at, html_url, snippet, matching_comments,"
                    " comment_snippet, matching_reviews, review_snippet}"
                ),
                "matched_mode": _STR,
                "total_matches": _INT,
                "strict_matches": _INT,
                "broadened_matches": _INT,
                "offset": _INT,
                "corpus_items": _INT,
                "index_stale": _BOOL,
                "next_cursor": {
                    "type": "string",
                    "description": "present when the page is truncated"
                    " (MCP; the CLI pages with --offset)",
                },
                "bots_omitted": {
                    "type": "integer",
                    "description": "present under exclude_bots/--no-bots",
                },
            },
            (
                "broadened_matches", "corpus_items", "index_stale", "items",
                "matched_mode", "offset", "strict_matches", "total_matches",
            ),
        ),
    },
    "get_item": {
        "cli": "item",
        "description": "One item row (every items column) plus labels,"
        " comments, reviews, files and edges, bounded by default with"
        " omission counts. The CLI multi-number form (item N1 N2 ...) returns"
        " a bare list of these rows in input order, unstored numbers included"
        " as per-row not_found entries.",
        "data_keys": (
            "additions", "api_url", "author", "author_association",
            "base_ref", "body", "branch_token", "changed_files", "closed_at",
            "comment_count", "comments", "comments_omitted", "commits",
            "created_at", "deletions", "dependencies_blocked_by",
            "dependencies_blocking", "dependencies_total_blocked_by",
            "dependencies_total_blocking", "draft", "edges", "files",
            "head_ref", "html_url", "id", "index_stale", "item_type", "kind",
            "labels", "labels_text", "locked", "merge_commit", "merged_at",
            "merged_by", "milestone", "node_id", "number", "paths_text",
            "reactions_total", "repo", "reviews", "reviews_omitted", "state",
            "state_reason", "sub_issues_completed", "sub_issues_total",
            "title", "updated_at",
        ),
        "row_fields": {
            "comments": ("author", "body", "created_at", "github_id",
                         "html_url", "kind", "pk"),
            "edges": ("confidence", "dst_id", "dst_type", "evidence", "rel",
                      "source", "src_id", "src_type"),
        },
        "data": _props(
            {
                "repo": _STR,
                "number": _INT,
                "kind": _STR,
                "title": _STR_NULL,
                "body": _STR_NULL,
                "state": _STR,
                "author": _STR_NULL,
                "created_at": _STR_NULL,
                "updated_at": _STR_NULL,
                "html_url": _STR_NULL,
                "labels": _open_objects("label rows {name, color}"),
                "comments": _open_objects(
                    "comment rows {pk, github_id, kind, author, created_at,"
                    " body, html_url}"
                ),
                "reviews": _open_objects(
                    "review rows {id, author, state, submitted_at, body,"
                    " html_url}"
                ),
                "files": _open_objects(
                    "pr file rows {path, status, additions, deletions,"
                    " changes}"
                ),
                "edges": _open_objects(
                    "edge rows {src_type, src_id, rel, dst_type, dst_id,"
                    " confidence, evidence, source}"
                ),
                "index_stale": _BOOL,
                "comments_omitted": _INT,
                "reviews_omitted": _INT,
                "bots_omitted": {
                    "type": "integer",
                    "description": "present under exclude_bots/--no-bots",
                },
                "body_chars_omitted": {
                    "type": "integer",
                    "description": "present when the default body bound cut"
                    " the body",
                },
                "truncated": {
                    "type": "boolean",
                    "description": "set on the row when its own body was cut",
                },
            },
            (
                "author", "body", "comments", "comments_omitted",
                "created_at", "edges", "files", "html_url", "index_stale",
                "kind", "labels", "number", "repo", "reviews",
                "reviews_omitted", "state", "title", "updated_at",
            ),
        ),
    },
    "related": {
        "cli": "related",
        "description": "The item's bounded graph neighborhood (node and"
        " serialized-byte budgets; truncated:true names the cut).",
        "data_keys": ("edges", "index_stale", "nodes", "number", "repo"),
        "row_fields": {
            "nodes": ("kind", "number", "state", "title"),
            "edges": ("confidence", "dst_id", "dst_type", "evidence", "rel",
                      "source", "src_id", "src_type"),
        },
        "data": _props(
            {
                "number": _INT,
                "repo": _STR,
                "nodes": _open_objects(
                    "{number, kind, title, state}; number is null for"
                    " repo-qualified foreign cross-reference placeholders"
                ),
                "edges": _open_objects(
                    "edge rows {src_type, src_id, rel, dst_type, dst_id,"
                    " confidence, evidence, source}"
                ),
                "index_stale": _BOOL,
            },
            ("edges", "index_stale", "nodes", "number", "repo"),
        ),
    },
    "path": {
        "cli": "path",
        "description": "Undirected path between two item numbers or file"
        " paths; path is null (with exit 0 / ok:true) when no route exists.",
        "data_keys": ("a", "b", "path", "repo"),
        "data": _props(
            {
                "a": _STR,
                "b": _STR,
                "repo": _STR_NULL,
                "path": {
                    "type": ["array", "null"],
                    "items": dict(
                        _OPEN_OBJ,
                        description="hop {type, id}",
                    ),
                    "description": "null when no route exists",
                },
            },
            ("a", "b", "path", "repo"),
        ),
    },
    "pr_overlap": {
        "cli": "overlap",
        "description": "Shared file paths for each unordered pair of the"
        " given PR numbers ([] for disjoint pairs), in the caller's order.",
        "data_keys": ("pairs",),
        "data": _props(
            {
                "pairs": {
                    "type": "array",
                    "items": _props(
                        {
                            "a": _INT,
                            "b": _INT,
                            "shared": {
                                "type": "array",
                                "items": _STR,
                                "description": "sorted shared paths",
                            },
                        },
                        ("a", "b", "shared"),
                    ),
                },
            },
            ("pairs",),
        ),
    },
    "file_history": {
        "cli": "file-history",
        "description": "PRs that touched a path, newest first, each entry"
        " carrying its GitHub-reported closed_issues and keyword-derived"
        " keyword_closes separately.",
        "data_keys": ("entries", "index_stale"),
        "row_fields": {
            "entries": ("closed_issues", "html_url", "keyword_closes",
                        "number", "state", "title", "updated_at"),
        },
        "data": _props(
            {
                "entries": _open_objects(
                    "{number, title, state, updated_at, html_url,"
                    " closed_issues, keyword_closes}"
                ),
                "index_stale": _BOOL,
                "next_cursor": {
                    "type": "string",
                    "description": "present when the page is truncated"
                    " (MCP-only paging; the file-history CLI returns the"
                    " whole list)",
                },
            },
            ("entries", "index_stale"),
        ),
    },
    "what_closed": {
        "cli": "what-closed",
        "description": "What closed an issue: GitHub-reported closing PRs"
        " (prs), keyword-derived closing items (keyword_closes) and closing"
        " commits, every entry carrying its edge provenance; a PR has no"
        " closers (the row's kind says which).",
        "data_keys": ("commits", "index_stale", "keyword_closes", "kind",
                      "number", "prs"),
        "data": _props(
            {
                "number": _INT,
                "kind": _STR,
                "prs": _open_objects("{number, evidence, source?}"),
                "keyword_closes": _open_objects("{number, evidence, source?}"),
                "commits": _open_objects("{sha, evidence, source?}"),
                "index_stale": _BOOL,
            },
            ("commits", "index_stale", "keyword_closes", "kind", "number",
             "prs"),
        ),
    },
    "open_items": {
        "cli": None,  # the CLI `open` command streams the BARE row list
        "description": "Open issues and PRs enriched with labels,"
        " assignees and linked_open_prs. MCP wraps the page one level"
        " ({items, next_cursor?}); the CLI open command returns the bare"
        " row list.",
        "data_keys": ("items",),
        "row_fields": {
            "items": ("assignees", "author", "html_url", "kind", "labels",
                      "linked_open_prs", "number", "repo", "title",
                      "updated_at"),
        },
        "data": _props(
            {
                "items": _open_objects(
                    "{repo, number, kind, title, author, updated_at,"
                    " html_url, labels, assignees, linked_open_prs}"
                ),
                "next_cursor": {
                    "type": "string",
                    "description": "present when the page is truncated",
                },
            },
            ("items",),
        ),
    },
    "sql": {
        "cli": "sql",
        "description": "Read-only SQL result: the column names and the row"
        " stream (MCP rows are positional arrays matching columns; the CLI"
        " defaults to objects keyed by column, --rows array for positional).",
        "data_keys": ("columns", "rows"),
        "data": _props(
            {
                "columns": {"type": "array", "items": _STR},
                "rows": {
                    "type": "array",
                    "items": {
                        "type": ["array", "object"],
                        "description": "positional cells (MCP) or objects"
                        " keyed by column (CLI default)",
                    },
                },
                "truncated": {
                    "type": "boolean",
                    "description": "kept inside data on MCP; the CLI promotes"
                    " it to the envelope flag",
                },
                "warnings": {
                    "type": "array",
                    "items": _props(
                        {
                            "column": _STR,
                            "literal": _STR,
                            "stored_values": {
                                "type": "array",
                                "items": _STR,
                                "description": "the stored DISTINCT"
                                " vocabulary of that column (bounded)",
                            },
                        },
                        ("column", "literal", "stored_values"),
                    ),
                    "description": "out-of-vocabulary literal warnings"
                    " ({column, literal, stored_values}; query.run_sql"
                    " emits one object per warning)",
                },
                "total_rows": {
                    "type": "integer",
                    "description": "present when truncated (the unfiltered"
                    " total, when computable)",
                },
            },
            ("columns", "rows"),
        ),
    },
    "sync": {
        "cli": None,  # the write surface; schema --output refuses non-read commands
        "description": "The started-or-joined background sync job (never"
        " blocks; graph_status reports progress).",
        "data_keys": ("job",),
        "data": _props(
            {
                "job": _STR,
                "started": {
                    "type": "boolean",
                    "description": "this call started the job",
                },
                "joined": {
                    "type": "boolean",
                    "description": "a sync was already running and this call"
                    " joined it",
                },
            },
            ("job",),
        ),
    },
    "similar_items": {
        "cli": "similar",
        "description": "Issues related to one item with per-signal evidence"
        " and the effective weight table (derived, read-only).",
        "data_keys": ("candidates", "capabilities", "derived",
                      "ls_files_applied", "ls_files_head", "repo",
                      "resolved_path_count", "target", "weights"),
        "data": _props(
            {
                "target": _INT,
                "repo": _STR,
                "candidates": _open_objects(
                    "scored candidates with per-signal evidence"
                ),
                "resolved_path_count": _INT,
                "weights": dict(
                    _OPEN_OBJ, description="the effective weight table"
                ),
                "capabilities": {
                    "type": "array",
                    "items": _STR,
                    "description": "degraded-signal notes (empty when every"
                    " signal was available)",
                },
                "derived": _BOOL,
                "ls_files_applied": _BOOL,
                "ls_files_head": _STR_NULL,
            },
            ("candidates", "capabilities", "derived", "ls_files_applied",
             "ls_files_head", "repo", "resolved_path_count", "target",
             "weights"),
        ),
    },
    "triage": {
        "cli": "triage",
        "description": "Open issues ranked workable by the documented"
        " formula, with gates naming every exclusion; pages via cursor"
        " (derived, read-only).",
        "data_keys": ("blocking_source", "closeable_trackers",
                      "config_source", "cursor", "derived", "formula_version",
                      "gated", "hotspot_asof", "repo", "rows",
                      "stage2_weight", "top_k", "triage_asof", "truncated",
                      "weights"),
        "row_fields": {
            "rows": ("analog", "gates", "hotspot", "hotspot_raw", "note",
                     "number", "ranked", "regression", "resolved_paths",
                     "scores", "stage2", "title", "total_score"),
        },
        "data": _props(
            {
                "repo": _STR,
                "rows": _open_objects(
                    "{number, title, gates, ranked, scores, total_score,"
                    " stage2, hotspot, hotspot_raw, regression, analog,"
                    " resolved_paths, note?}"
                ),
                "weights": dict(
                    _OPEN_OBJ, description="the effective weight table"
                ),
                "config_source": _STR,
                "top_k": _INT_NULL,
                "stage2_weight": _NUM_NULL,
                "formula_version": _STR,
                "triage_asof": _STR_NULL,
                "hotspot_asof": _STR_NULL,
                "blocking_source": _STR_NULL,
                "gated": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "{count, by_gate, numbers}: every gated"
                    " open issue under its gate names (issue #69 AC5)",
                },
                "closeable_trackers": {
                    "type": "array",
                    "items": {"type": ["integer", "object"]},
                    "description": "tracking-gated containers whose tracked"
                    " work is all done (issues #69 AC1)",
                },
                "cursor": _STR_NULL,
                "truncated": _BOOL,
                "derived": _BOOL,
            },
            ("blocking_source", "closeable_trackers", "config_source",
             "cursor", "derived", "formula_version", "gated",
             "hotspot_asof", "repo", "rows", "stage2_weight", "top_k",
             "triage_asof", "truncated", "weights"),
        ),
    },
    "bundle": {
        "cli": "bundle",
        "description": "Evidence-backed 2-3-issue work bundles over the"
        " triage pool, with dropped pool members named (derived,"
        " read-only).",
        "data_keys": ("bundles", "closeable_trackers", "config_source",
                      "derived", "dropped", "excluded", "formula_version",
                      "gated", "merge_candidates", "pool",
                      "related_not_bundleable", "repo", "unbundled",
                      "weights"),
        "data": _props(
            {
                "repo": _STR,
                "bundles": _open_objects(
                    "bundle payloads: members, file sets, co-changed tests,"
                    " open-PR collisions, dependency order"
                ),
                "merge_candidates": _open_objects(
                    "{kind, numbers, affinity, evidence}"
                ),
                "related_not_bundleable": _open_objects(
                    "related pairs the bundler could not use"
                ),
                "excluded": {
                    "type": "array",
                    "items": {"type": ["integer", "object"]},
                    "description": "numbers (or entry objects) removed from"
                    " the pool before ranking",
                },
                "pool": {
                    "type": "array",
                    "items": _INT,
                    "description": "the numbers the bundles were built from",
                },
                "dropped": _open_objects(
                    "{number, reason} for pool members that could not be used"
                ),
                "unbundled": _open_objects(
                    "{number, reason} for pool members no bundle absorbed"
                    " (below floor, no anchor, shared files, cohesion stop;"
                    " issue #69 AC5)"
                ),
                "gated": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "{count, by_gate, numbers}: every gated"
                    " open issue under its gate names (issue #69 AC5)",
                },
                "closeable_trackers": {
                    "type": "array",
                    "items": {"type": ["integer", "object"]},
                    "description": "tracking-gated containers whose tracked"
                    " work is all done (issues #69 AC1)",
                },
                "weights": dict(
                    _OPEN_OBJ, description="the effective weight table"
                ),
                "config_source": _STR,
                "formula_version": _STR,
                "derived": _BOOL,
                "truncated": {
                    "type": "boolean",
                    "description": "present when budget_tokens cut the"
                    " payload",
                },
            },
            ("bundles", "closeable_trackers", "config_source", "derived",
             "dropped", "excluded", "formula_version", "gated",
             "merge_candidates", "pool", "related_not_bundleable", "repo",
             "unbundled", "weights"),
        ),
    },
    "brief": {
        "cli": "brief",
        "description": "The token-budgeted work packet for one issue:"
        " triage features, bounded body and comments, implicated files,"
        " closest analog and linked PRs (derived, read-only; live=true adds"
        " live CI/review state via gh).",
        "data_keys": ("analog", "body", "comments", "config_source",
                      "content_trust", "derived", "features", "files",
                      "formula_version", "linked_prs", "number", "repo",
                      "weights"),
        "data": _props(
            {
                "repo": _STR,
                "number": _INT,
                "features": dict(
                    _OPEN_OBJ,
                    description="the triage row for this issue"
                    " ({number, title, gates, ranked, scores, ...})",
                ),
                "content_trust": _STR,
                "comments": _open_objects(
                    "{author, author_association, created_at, body}"
                ),
                "body": {
                    "type": "object",
                    "properties": {"untrusted_text": _STR},
                    "required": ["untrusted_text"],
                    "additionalProperties": True,
                    "description": "the item body inside the AC6"
                    " untrusted-content wrapper"
                    " {untrusted_text: str} (derive._untrusted)",
                },
                "files": {
                    "type": "object",
                    "properties": {
                        "resolved": {
                            "type": "array",
                            "items": _STR,
                            "description": "stored paths the item implicates",
                        },
                        "unresolved_count": _INT,
                    },
                    "additionalProperties": True,
                },
                "analog": {
                    "type": ["object", "null"],
                    "additionalProperties": True,
                    "description": "the closest merged-PR analog (null when"
                    " none scored)",
                },
                "linked_prs": _open_objects(
                    "{number, state, title}"
                ),
                "weights": dict(
                    _OPEN_OBJ, description="the effective weight table"
                ),
                "config_source": _STR,
                "formula_version": _STR,
                "derived": _BOOL,
                "bots_omitted": {
                    "type": "integer",
                    "description": "present under exclude_bots/--no-bots",
                },
                "live": {
                    "type": ["object", "null"],
                    "additionalProperties": True,
                    "description": "live CI/review state (present only"
                    " under live=true)",
                },
                "live_error": {
                    "type": "string",
                    "description": "present when the live gh fan failed",
                },
                "truncated": {
                    "type": "boolean",
                    "description": "present when the token budget cut the"
                    " packet",
                },
            },
            ("analog", "body", "comments", "config_source", "content_trust",
             "derived", "features", "files", "formula_version",
             "linked_prs", "number", "repo", "weights"),
        ),
    },
    "churn": {
        "cli": "churn",
        "description": "Files ranked by PR touch count — a BARE LIST of"
        " per-path rows (not wrapped in an object), on both surfaces.",
        "data_keys": ("additions", "deletions", "path", "prs"),
        "data": {
            "type": "array",
            "items": _props(
                {
                    "path": _STR,
                    "prs": _INT,
                    "additions": _INT_NULL,
                    "deletions": _INT_NULL,
                },
                ("additions", "deletions", "path", "prs"),
            ),
            "description": "bare list of per-path rows",
        },
    },
}


def _output_schema_for(tool_name: str) -> dict:
    """The tool's outputSchema, derived from the shape registry (issue #72
    C17): the shared envelope object plus the tool's own data member.
    Permissive at both levels (additionalProperties true) — the envelope
    gains `error` on failures and row objects carry more columns than any
    schema should pin — and the data member's type union gains the null
    alternative, because failure envelopes answer data:null against the
    SAME schema (a representative error result must validate too)."""
    entry = _TOOL_SHAPES[tool_name]
    data = dict(entry["data"])
    declared = data.get("type")
    if isinstance(declared, str):
        data["type"] = [declared, "null"]
    elif isinstance(declared, list) and "null" not in declared:
        data["type"] = [*declared, "null"]
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "db": {"type": ["string", "null"]},
            "repo": {"type": ["string", "null"]},
            "freshness": {
                "type": ["object", "null"],
                "properties": {
                    "synced_at": _STR_NULL,
                    "age_s": _INT_NULL,
                    "complete": _BOOL,
                    "refreshed": _BOOL,
                    "refreshing": _BOOL,
                    "refresh_failed": _STR_NULL,
                },
                "additionalProperties": True,
            },
            "data": data,
            "truncated": _BOOL,
            "error": {
                "type": ["object", "null"],
                "properties": {
                    "code": _STR,
                    "message": _STR,
                    "hint": _STR,
                },
                "additionalProperties": True,
            },
        },
        "required": list(_ENVELOPE_KEYS),
        "additionalProperties": True,
    }


#: CLI read command -> the registry shape its payload matches (issue #72
#: C23 / plan CF-L). Most commands share their MCP twin's entry verbatim;
#: `open` genuinely differs (the CLI streams the bare row list the
#: open_items tool wraps under data.items), so it carries its own
#: bare-list entry whose row fields come from the same registry.
_CLI_BARE_LIST_NOTE = "bare list of rows (one JSON object per row; no wrapper key)"

CLI_OUTPUT_SHAPES: dict[str, dict] = {
    command: {
        "description": entry["description"],
        "data_kind": "object"
        if isinstance(entry["data"].get("type"), str)
        and entry["data"]["type"] == "object"
        else "list",
        "data_keys": list(entry["data_keys"]),
        "mcp_tool": tool,
    }
    for tool, entry in _TOOL_SHAPES.items()
    if (command := entry.get("cli"))
}
CLI_OUTPUT_SHAPES["open"] = {
    "description": _TOOL_SHAPES["open_items"]["description"]
    + " The CLI payload is the "
    + _CLI_BARE_LIST_NOTE
    + " that the open_items MCP tool wraps under data.items.",
    "data_kind": "list",
    "data_keys": list(
        _TOOL_SHAPES["open_items"]["row_fields"]["items"]
    ),
    "mcp_tool": "open_items",
}
CLI_OUTPUT_SHAPES["item"]["description"] += (
    " Single-number item answers one row object; multi-number item answers the "
    + _CLI_BARE_LIST_NOTE
    + " (unstored numbers included as not_found rows)."
)

_TOOLS: list[dict] = [
    {
        "name": "graph_status",
        "description": "Sync freshness, counts, corpus coverage, and any running sync for the resolved repo (the status command, plus live sync progress)",
        "inputSchema": _schema(dict(_REPO_DB)),
    },
    {
        "name": "search",
        "description": "Relevance-ranked full-text search over titles, bodies, labels, comments, review bodies, and PR touched paths",
        "inputSchema": _schema(
            {
                "query": {"type": "string"},
                "limit": _LIMIT,
                "cursor": _CURSOR,
                "exclude_bots": {
                    "type": "boolean",
                    "description": "Omit bot-authored matches (items, comments, reviews filtered out of the match SQL pre-ranking; reports bots_omitted, the excluded bot-authored matches)",
                },
                **_REPO_DB,
            },
            ["query"],
        ),
    },
    {
        "name": "get_item",
        "description": "One issue/PR with labels, comments, reviews, files, and edges (bounded by default: body <= 20 KiB with a truncation marker, comments/reviews <= 5 with comments_omitted/reviews_omitted)",
        "inputSchema": _schema(
            {
                "number": {"type": "integer"},
                "max_body_chars": {"type": "integer", "minimum": 0, "description": "Truncate item/comment/review bodies to N chars (overrides the default 20 KiB body bound; 0 is the explicit uncapped override, matching the CLI --max-body-chars 0)"},
                "max_comments": {"type": "integer", "minimum": 0, "description": "Cap the comment list to K entries (overrides the default 5; reports comments_omitted)"},
                "max_reviews": {"type": "integer", "minimum": 0, "description": "Cap the review list to K entries (overrides the default 5; reports reviews_omitted)"},
                "exclude_bots": {
                    "type": "boolean",
                    "description": "Omit bot-authored comments and reviews, filtered in the query before omission counting (reports bots_omitted, the excluded comment+review rows)",
                },
                **_REPO_DB,
            },
            ["number"],
        ),
    },
    {
        "name": "related",
        "description": "One-hop (or deeper) neighborhood of an item in the graph, bounded by a node budget (default 100) and a serialized byte budget",
        "inputSchema": _schema(
            {
                "number": {"type": "integer"},
                "depth": {"type": "integer", "minimum": 1, "maximum": _MAX_DEPTH},
                "max_nodes": {"type": "integer", "minimum": 1, "maximum": _MAX_PAGE, "description": f"Stop expanding at this many nodes (default 100, capped at {_MAX_PAGE}); truncated:true names the cut"},
                **_REPO_DB,
            },
            ["number"],
        ),
    },
    {
        "name": "path",
        "description": "Undirected path between two item numbers or file paths",
        "inputSchema": _schema({"a": {"type": "string"}, "b": {"type": "string"}, **_REPO_DB}, ["a", "b"]),
    },
    {
        "name": "pr_overlap",
        "description": "Shared file paths for each pair of the given PR numbers ([] for disjoint pairs) — the collision check before stacking PRs",
        "inputSchema": _schema(
            {
                "numbers": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2,
                    "maxItems": _MAX_NUMBERS,
                },
                **_REPO_DB,
            },
            ["numbers"],
        ),
    },
    {
        "name": "file_history",
        "description": "PRs that touched a path, newest first, each entry carrying closed_issues (GitHub-reported) and keyword_closes (keyword-derived)",
        "inputSchema": _schema({"path": {"type": "string"}, "limit": _LIMIT, "cursor": _CURSOR, **_REPO_DB}, ["path"]),
    },
    {
        "name": "what_closed",
        "description": "Closing PRs (prs), keyword-derived closing items (keyword_closes) and commits for an issue (reports the item kind; a PR has no closers), each entry carrying its edge source provenance",
        "inputSchema": _schema({"number": {"type": "integer"}, **_REPO_DB}, ["number"]),
    },
    {
        "name": "open_items",
        "description": "Open issues and pull requests, each row enriched with labels (name-ordered), assignees (logins), and linked_open_prs (open PRs linked per the linked_pr/open-closer rule; numbers only)",
        "inputSchema": _schema({"limit": _LIMIT, "cursor": _CURSOR, **_REPO_DB}),
    },
    {
        "name": "sql",
        "description": "Read-only SQL (one SELECT/WITH/EXPLAIN) with the same guards as the CLI; rows are positional arrays matching the returned columns",
        "inputSchema": _schema({"statement": {"type": "string"}, "limit": _LIMIT, **_REPO_DB}, ["statement"]),
    },
    {
        "name": "sync",
        "description": "Start one background incremental sync; never blocks — returns a job id, graph_status reports progress",
        "inputSchema": _schema(
            {
                "force": {"type": "boolean", "description": "Ignore the watermark; full pull"},
                "numbers": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "description": f"Refresh exactly these items; the watermark is untouched (at most {_MAX_NUMBERS})",
                    "maxItems": _MAX_NUMBERS,
                },
                "include_patches": {"type": "boolean", "description": "Store pull file patches"},
                "source": {
                    "type": "string",
                    "enum": ["graphql", "rest"],
                    "description": "Fetch source for this run; omitted inherits the store's recorded profile",
                },
                **_REPO_DB,
            }
        ),
    },
    {
        "name": "similar_items",
        "description": "Issues related to one item with per-signal evidence (the CLI similar command; derived, read-only)",
        "inputSchema": _schema(
            {
                "number": {"type": "integer"},
                "limit": _LIMIT,
                **_REPO_DB,
            },
            ["number"],
        ),
    },
    {
        "name": "triage",
        "description": "Open issues ranked workable by the documented formula, with gate reasons (the CLI triage command; derived, read-only)",
        "inputSchema": _schema(
            {
                "limit": _LIMIT,
                "cursor": _CURSOR,
                "top_k": {"type": "integer", "minimum": 1, "maximum": _MAX_TOP_K, "description": f"Keep only the top K ranked rows (at most {_MAX_TOP_K})"},
                "include_gated": {"type": "boolean", "description": "Include blocked/assigned/locked rows (ranked: false)"},
                "as_of": {"type": "string", "description": "Answer from the as-of snapshot view at instant T (ISO-8601 Z)"},
                **_REPO_DB,
            }
        ),
    },
    {
        "name": "bundle",
        "description": "Evidence-backed 2-3-issue work bundles over the triage pool (the CLI bundle command; derived, read-only)",
        "inputSchema": _schema(
            {
                "size": {"type": "string", "description": "Bundle size range like '2-3'"},
                "count": {"type": "integer", "minimum": 1, "description": "At most K bundles"},
                "pool": {"type": "array", "items": {"type": "integer"}, "maxItems": _MAX_NUMBERS, "description": f"Restrict the pool to these issue numbers (at most {_MAX_NUMBERS})"},
                "exclude": {"type": "array", "items": {"type": "integer"}, "maxItems": _MAX_NUMBERS, "description": f"Remove these numbers from the pool (at most {_MAX_NUMBERS})"},
                "allow_shared_files": {"type": "boolean"},
                "max_files": {"type": "integer", "minimum": 1},
                "budget_tokens": {"type": "integer", "minimum": 1},
                "as_of": {"type": "string", "description": "Answer from the as-of snapshot view at instant T (ISO-8601 Z)"},
                **_REPO_DB,
            }
        ),
    },
    {
        "name": "brief",
        "description": "The token-budgeted work packet for one issue (the CLI brief command; derived, read-only; live=true queries GitHub with the operator gh credentials)",
        "inputSchema": _schema(
            {
                "number": {"type": "integer"},
                "budget_tokens": {"type": "integer", "minimum": 1},
                "live": {"type": "boolean", "description": "Query live CI/review state via gh"},
                "exclude_bots": {
                    "type": "boolean",
                    "description": "Omit bot-authored content: bot comments leave the comment ranking and bot-authored items (linked PRs, the analog) leave the packet (reports bots_omitted, the filtered rows)",
                },
                **_REPO_DB,
            },
            ["number"],
        ),
    },
    {
        "name": "churn",
        "description": "Files ranked by PR touch count (the CLI churn command; aggregation surface promoted from a documented gap)",
        "inputSchema": _schema(
            {
                "limit": _LIMIT,
                "merged_only": {"type": "boolean", "description": "Count merged PRs only"},
                **_REPO_DB,
            }
        ),
    },
]

# Issue #32: every tool declares an outputSchema and carries annotations
# with readOnlyHint true except sync (the only tool that mutates the store).
# Since issue #72 C17 the outputSchema is PER-TOOL, derived from the shape
# registry above (_output_schema_for) — it names the envelope keys, the
# tool's data keys and row fields, and stays permissive (additionalProperties
# true) at every level. The listing is SORTED BY NAME — one deterministic
# order for both protocol eras (the modern tools/list pins sorted order).
if {tool["name"] for tool in _TOOLS} != set(_TOOL_SHAPES):
    raise RuntimeError(
        "shape registry drift: _TOOL_SHAPES must describe exactly the"
        " advertised tools ("
        + ", ".join(sorted(
            {tool["name"] for tool in _TOOLS} ^ set(_TOOL_SHAPES)
        ))
        + ")"
    )
for _tool in _TOOLS:
    _tool["outputSchema"] = _output_schema_for(_tool["name"])
    _tool["annotations"] = {"readOnlyHint": _tool["name"] != "sync"}
_TOOLS.sort(key=lambda entry: entry["name"])

_TOOL_HANDLERS = {
    "graph_status": _tool_graph_status,
    "search": _tool_search,
    "get_item": _tool_get_item,
    "related": _tool_related,
    "path": _tool_path,
    "pr_overlap": _tool_pr_overlap,
    "file_history": _tool_file_history,
    "what_closed": _tool_what_closed,
    "open_items": _tool_open_items,
    "sql": _tool_sql,
    "sync": _tool_sync,
    "similar_items": _tool_similar_items,
    "triage": _tool_triage,
    "bundle": _tool_bundle,
    "brief": _tool_brief,
    "churn": _tool_churn,
}


# ---------------------------------------------------------------------------
# Protocol plumbing


def _send(stdout, obj: dict) -> None:
    try:
        stdout.write(json.dumps(obj, ensure_ascii=False))
        stdout.write("\n")
        stdout.flush()
    except OSError as exc:
        # Same translation as cli._write_stdout (FB-007): a dead client on
        # Windows raises plain OSError(EINVAL) that main()'s
        # BrokenPipeError catch would never see, turning the documented
        # quiet exit into a traceback + exit 1.
        if exc.errno in (errno.EINVAL, errno.EPIPE):
            raise BrokenPipeError(str(exc)) from exc
        raise


def _result(stdout, req_id, result: dict) -> None:
    _send(stdout, {"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(stdout, req_id, code: int, message: str) -> None:
    _send(stdout, {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


class _Context:
    """Per-serve state: resolution inputs, the sync seam, the id counter
    for server-originated requests, and the deferred-message buffer used
    while the inline roots handshake holds the read loop."""

    __slots__ = (
        "root",
        "stale_after_s",
        "sync_runner",
        "stdin",
        "stdout",
        "server_seq",
        "deferred",
        "default_repo",
        "default_db",
        "modern",
        "roots_declared",
    )

    def __init__(
        self,
        stdin,
        stdout,
        root: Path,
        stale_after_s: int,
        sync_runner,
        default_repo: str | None = None,
        default_db: str | None = None,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.root = root
        self.stale_after_s = stale_after_s
        self.sync_runner = sync_runner
        self.server_seq = 0
        self.deferred: list[dict] = []
        self.default_repo = default_repo
        self.default_db = default_db
        #: The CURRENT request's era, set by _dispatch for every message it
        #: processes and overwritten by the next one — modern is a property
        #: of a request, never of a session.
        self.modern = False
        #: Whether this session's client DECLARED ClientCapabilities.roots
        #: at initialize (issue #71 C4 / plan CF-C): recorded once at the
        #: initialize handshake, never per-request. Only a declared
        #: session is ever asked roots/list; undeclared sessions fall
        #: through to the server pin/cwd during resolution.
        self.roots_declared = False


def serve(
    stdin=None,
    stdout=None,
    *,
    cwd=None,
    stale_after_s=None,
    sync_runner=None,
    repo=None,
    db=None,
) -> None:
    """Serve MCP over the given TEXT streams until stdin EOF.

    cwd is the resolution root (default Path.cwd()); stale_after_s is the
    freshness threshold that kicks a background refresh (any positive age
    above it goes stale — use a huge value to disable); repo/db pin the
    resolution for a server started with --repo/--db (the per-call tool
    arguments still win); sync_runner(db_path, repo) replaces the
    background sync body (tests inject; the default runs the real locked
    incremental sync)."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    root = Path(cwd) if cwd is not None else Path.cwd()
    if stale_after_s is None:
        stale_after_s = _default_stale_after_s()
    if sync_runner is None:
        sync_runner = _default_sync_runner
    ctx = _Context(
        stdin,
        stdout,
        root,
        int(stale_after_s),
        sync_runner,
        default_repo=repo,
        default_db=db,
    )

    while True:
        line = stdin.readline()
        if line == "":
            return
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            # RecursionError is not a ValueError subclass: a deeply
            # nested frame must never kill the session (review F3).
            _error(stdout, None, _JSONRPC_PARSE_ERROR, "Parse error (line is not JSON)")
            continue
        if not isinstance(msg, dict):
            _error(stdout, None, _JSONRPC_INVALID_REQUEST, "Invalid Request (line is not an object)")
            continue
        _process(msg, ctx)
        # Messages that arrived while a roots handshake held the read
        # loop: every identified request gets exactly one response.
        while ctx.deferred:
            _process(ctx.deferred.pop(0), ctx)


def _tool_result(stdout, req_id, env: dict, modern: bool) -> None:
    """The ONE tools/call result write: the text block plus structuredContent
    (issue #32 AC2 — every tool answer, success or failure, carries the
    parsed envelope structurally), and resultType complete on the modern
    era (an isError result is still a COMPLETE call: the tool ran)."""
    result: dict = {
        "content": [{"type": "text", "text": json.dumps(env, ensure_ascii=False)}],
        "isError": env.get("error") is not None,
        "structuredContent": env,
    }
    if modern:
        result["resultType"] = "complete"
    _result(stdout, req_id, result)


def _cacheable(result: dict, ttl_ms: int, scope: str) -> dict:
    """Tag a modern listing/read result with the official CacheableResult
    fields plus resultType complete (issue #33)."""
    result["resultType"] = "complete"
    result["ttlMs"] = int(ttl_ms)
    result["cacheScope"] = scope
    return result


def _discover_result(modern: bool) -> dict:
    """server/discover: the versions this server speaks (the modern revision
    plus every served legacy revision, derived from the one constants seam),
    capabilities, and the tool registry — the issue-mandated `tools` field is
    an additive DiscoverResult extension (#33 AC9)."""
    result = {
        "supportedVersions": [_MODERN_PROTOCOL_VERSION, *_PROTOCOL_VERSIONS],
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
        },
        "tools": _TOOLS,
    }
    if modern:
        _cacheable(result, _LISTING_TTL_MS, "public")
    return result


def _error_data(stdout, req_id, code: int, message: str, data) -> None:
    """A JSON-RPC error with a structured data object (the modern-era
    UnsupportedProtocolVersionError carries supported/requested)."""
    error: dict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    _send(stdout, {"jsonrpc": "2.0", "id": req_id, "error": error})


def _process(msg: dict, ctx: "_Context") -> None:
    """Handle one inbound message: dispatch it with full error containment."""
    method = msg.get("method")
    if method is None:
        # A reply to a server-originated request that is no longer
        # awaited (roots replies are consumed inline); ignore.
        return
    stdout = ctx.stdout
    has_id = "id" in msg
    req_id = msg.get("id")
    try:
        _dispatch(msg, method, has_id, req_id, ctx)
    except _InputRequired:
        # Escaped _dispatch only if a future handler raises it outside the
        # modern tools/call|resources flow: answer the input-required result
        # rather than a protocol error (never a traceback).
        _result(
            stdout,
            req_id,
            {
                "resultType": "input_required",
                "requestState": _InputRequired.REQUEST_STATE,
                "content": [{"type": "text", "text": _InputRequired().hint_text()}],
            },
        )
    except BrokenPipeError:
        # The translated dead-pipe signal (FB-007): leave quietly
        # via the main() handler -- a traceback here re-breaks the
        # quiet exit.
        raise
    except ToolError as exc:
        if method == "tools/call":
            env = _error_envelope(exc.code_key, exc.message, exc.hint, exc.slug, exc.db_path)
            _tool_result(stdout, req_id, env, ctx.modern)
        else:
            message = exc.message + (f" (hint: {exc.hint})" if exc.hint else "")
            _error(stdout, req_id, _JSONRPC_INVALID_PARAMS, message)
    except Exception as exc:  # never let one fault kill the session
        _log(traceback.format_exc())
        if method == "tools/call":
            # A tool fault is a tool answer: the envelope error contract
            # holds even when the handler did not raise ToolError
            # (sqlite faults, unexpected TypeErrors — review F4).
            env = _error_envelope(
                "runtime",
                str(exc),
                "internal error; the server log has details",
                None,
                None,
            )
            _tool_result(stdout, req_id, env, ctx.modern)
        elif has_id:
            _error(stdout, req_id, _JSONRPC_INTERNAL_ERROR, f"Internal error: {exc}")


def _dispatch(msg: dict, method: str, has_id: bool, req_id, ctx: _Context) -> None:
    stdout = ctx.stdout
    params = msg.get("params")
    if params is not None and not isinstance(params, dict):
        # Non-object params are legal JSON-RPC framing but malformed for
        # every method this server serves — invalid params, never an
        # internal error (pr49 review PRR-001; base answered tools/list
        # for array params, so this must not become -32603).
        if has_id:
            _error(stdout, req_id, _JSONRPC_INVALID_PARAMS, "params must be an object")
        return
    params = params or {}
    era, declared = _era_of(params)
    if era == "malformed":
        # A namespaced protocolVersion that is present but not a string is
        # malformed modern metadata: -32602 (2026-07-28 requires the
        # per-request version as a string; the PR body promises -32602 for
        # exactly this shape — pr49 review PRR-004, which also stops the
        # silent legacy downgrade from walking a modern-intent call into
        # the roots round-trip).
        if has_id:
            _error(stdout, req_id, _JSONRPC_INVALID_PARAMS, "malformed modern metadata")
        return
    if era == "unsupported":
        # An unsupported declared modern version: UnsupportedProtocolVersionError
        # (2026-07-28 versioning) listing what this server CAN speak.
        # Notifications (no id) get no error reply, like the fallthrough.
        if has_id:
            _error_data(
                stdout,
                req_id,
                -32022,
                "Unsupported protocol version",
                {
                    "supported": [_MODERN_PROTOCOL_VERSION, *_PROTOCOL_VERSIONS],
                    "requested": declared,
                },
            )
        return
    modern = era == "modern"
    if modern and not isinstance(_modern_capabilities(params), dict):
        if has_id:
            _error(
                stdout,
                req_id,
                _JSONRPC_INVALID_PARAMS,
                "modern requests must carry io.modelcontextprotocol/clientCapabilities "
                "as an object",
            )
        return
    # The era is a property of THIS request; the next message overwrites it.
    # Every early return above happens BEFORE this assignment, so a failed
    # request can never leave its own era behind for the next message
    # (pr49 review PRR-003 follow-up: assign-era-last is what makes the
    # stale-era residue unreachable).
    ctx.modern = modern
    if method == "initialize":
        if not has_id:
            return
        init_params = msg.get("params") or {}
        # Per-session roots capability (plan CF-C): recorded AT
        # INITIALIZE. A well-formed declaration (the roots entry is an
        # object per the ClientCapabilities schema) entitles the server
        # to the roots/list round-trip later; absent or malformed means
        # the client never declared it and must never be asked.
        capabilities = init_params.get("capabilities")
        ctx.roots_declared = isinstance(capabilities, dict) and isinstance(
            capabilities.get("roots"), dict
        )
        requested = init_params.get("protocolVersion")
        version = requested if requested in _PROTOCOL_VERSIONS else _LATEST_PROTOCOL_VERSION
        _result(
            stdout,
            req_id,
            {
                "protocolVersion": version,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {"name": "zaxbygraph", "version": __version__},
            },
        )
        return
    if method == "ping":
        if modern:
            # The 2026-07-28 revision has no ping method. Notifications
            # (no id) get no reply at all — JSON-RPC forbids answering a
            # notification (pr49 review PRR-002), like every sibling branch.
            if has_id:
                _error(
                    stdout,
                    req_id,
                    _JSONRPC_METHOD_NOT_FOUND,
                    "ping is removed in the 2026-07-28 revision",
                )
            return
        if has_id:
            _result(stdout, req_id, {})
        return
    if method == "tools/list":
        if not has_id:
            return
        result: dict = {"tools": _TOOLS}
        if modern:
            _cacheable(result, _LISTING_TTL_MS, "public")
        _result(stdout, req_id, result)
        return
    if method == "server/discover":
        if not has_id:
            return
        _result(stdout, req_id, _discover_result(modern))
        return
    if method == "tools/call":
        if not has_id:
            return
        params = msg.get("params") or {}
        name = params.get("name")
        handler = _TOOL_HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            _error(
                stdout,
                req_id,
                _JSONRPC_INVALID_PARAMS,
                f"Unknown tool {name!r}; call tools/list for the registry",
            )
            return
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            env = handler(arguments, ctx)
        except _InputRequired as required:
            # The modern era resolves without Roots: a repo-less, un-pinned
            # call asks the client for `repo` instead of round-tripping.
            _result(
                stdout,
                req_id,
                {
                    "resultType": "input_required",
                    "requestState": required.REQUEST_STATE,
                    "content": [{"type": "text", "text": required.hint_text()}],
                },
            )
            return
        except ToolError as exc:
            env = _error_envelope(exc.code_key, exc.message, exc.hint, exc.slug, exc.db_path)
            _tool_result(stdout, req_id, env, modern)
            return
        except Exception as exc:  # a tool fault is a tool answer
            _log(traceback.format_exc())
            env = _error_envelope(
                "runtime", str(exc), "internal error; the server log has details", None, None
            )
            _tool_result(stdout, req_id, env, modern)
            return
        _tool_result(stdout, req_id, env, modern)
        return
    if method == "resources/list":
        if not has_id:
            return
        result = {"resources": [
            {
                "uri": _SCHEMA_RESOURCE_URI,
                "name": "schema",
                "description": "Table DDL and per-column notes for the resolved database (the schema command)",
                "mimeType": "application/json",
            }
        ]}
        if modern:
            _cacheable(result, _LISTING_TTL_MS, "public")
        _result(stdout, req_id, result)
        return
    if method == "resources/templates/list":
        if not has_id:
            return
        result = {"resourceTemplates": []}
        if modern:
            _cacheable(result, _LISTING_TTL_MS, "public")
        _result(stdout, req_id, result)
        return
    if method == "resources/read":
        if not has_id:
            return
        uri = (msg.get("params") or {}).get("uri")
        if uri != _SCHEMA_RESOURCE_URI:
            _error(
                stdout,
                req_id,
                _JSONRPC_INVALID_PARAMS,
                f"Unknown resource {uri!r}; resources/list names what exists",
            )
            return
        try:
            slug, db_path = _resolve_target({}, ctx)
        except _InputRequired as required:
            # The modern era resolves without Roots (covers the resource
            # sibling path too). The hint names the REAL remedies: this
            # channel has no arguments slot, so "pass repo" would be
            # unactionable (pr49 review PRR-009).
            _result(
                stdout,
                req_id,
                {
                    "resultType": "input_required",
                    "requestState": required.REQUEST_STATE,
                    "content": [
                        {"type": "text", "text": required.hint_text(resources_channel=True)}
                    ],
                },
            )
            return
        conn = _open_corpus(slug, db_path)
        try:
            payload = json.dumps(describe_schema(conn), ensure_ascii=False)
        finally:
            conn.close()
        result = {
            "contents": [
                {"uri": _SCHEMA_RESOURCE_URI, "mimeType": "application/json", "text": payload}
            ]
        }
        if modern:
            _cacheable(result, _RESOURCE_READ_TTL_MS, "private")
        _result(stdout, req_id, result)
        return
    # notifications/initialized, notifications/cancelled, unknown methods
    if has_id:
        _error(stdout, req_id, _JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")


__all__ = ["serve", "ToolError"]
