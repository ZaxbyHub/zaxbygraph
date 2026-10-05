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
- Repo resolution asks the client for roots on every resolution that has
  no explicit repo argument (or server --repo pin); a client that never
  answers the roots request stalls that call (spec-conformant clients
  answer, with an error result at worst).
- A failed background refresh is retried on the next stale read; there
  is no backoff.
"""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
import sys
import threading
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

from zaxbygraph import __version__
from zaxbygraph.cli import _freshness, _item_count, _truncate_bodies, build_envelope
from zaxbygraph.db import connect, connect_readonly_query, init_schema, open_existing
from zaxbygraph.graphql import GraphQLSource as GhApiSource
from zaxbygraph.paths import resolve_db
from zaxbygraph.query import (
    _clamp_limit,
    assert_read_sql,
    file_history,
    item,
    open_items,
    path_between,
    pr_overlap,
    related,
    run_sql,
    search,
    status,
    what_closed,
)
from zaxbygraph.repo import DEFAULT_HOST, RepoError, remote_info, validate_slug
from zaxbygraph.schema_notes import describe_schema
from zaxbygraph.sync import acquire_sync_lock, read_lock_observer, sync_repo

#: Protocol revisions this server can speak. initialize echoes the
#: client's requested version when we serve it, else the latest one.
_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_LATEST_PROTOCOL_VERSION = _PROTOCOL_VERSIONS[0]

#: Staleness threshold default: the issue's proposed 15 minutes.
_DEFAULT_STALE_AFTER_S = 900
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
    db_path, repo: str, *, force: bool = False, include_patches: bool = False
) -> None:
    """The real background sync: the cmd_sync core without CLI output.

    The GraphQL bulk source is the shipped default; the REST fallback
    stays a CLI flag (the injected seam is pinned to (db_path, repo))."""
    conn = connect(db_path)
    try:
        init_schema(conn)
        source = GhApiSource(*repo.split("/", 1))
        sync_repo(conn, source, repo, force=force, include_patches=include_patches)
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
        while len(finished) > 50:  # bound the ledger in a long-lived server
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
    except OSError as exc:
        with _JOBS_LOCK:
            _JOBS[job_id]["state"] = "failed"
            _JOBS[job_id]["error"] = str(exc)
        raise
    if lock is None:
        # Another process owns the run lock: our caller joins it.
        with _JOBS_LOCK:
            _JOBS[job_id]["state"] = "joined"
        return job_id, False

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

    threading.Thread(target=_body, name=f"zaxbygraph-{job_id}", daemon=True).start()
    return job_id, True


def _jobs_snapshot(db_key: str) -> list[dict]:
    with _JOBS_LOCK:
        return [dict(job) for job in _JOBS.values() if job["db_path"] == db_key]


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
        except ValueError:
            continue
        if not isinstance(msg, dict) or msg.get("id") != req_id:
            if isinstance(msg, dict) and "method" in msg:
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

    Resolution order: explicit repo argument > MCP roots > the server
    cwd's git origin — then the same store-first resolve_db chain the CLI
    uses, so a linked worktree and its main checkout resolve one DB and
    nothing is ever created."""
    repo_arg = arguments.get("repo")
    if repo_arg is None:
        repo_arg = ctx.default_repo
    db_arg = arguments.get("db")
    if db_arg is None:
        db_arg = ctx.default_db
    if repo_arg is not None:
        try:
            slug = validate_slug(str(repo_arg))
        except RepoError as exc:
            raise ToolError(
                "bad_request", f"invalid repo argument ({exc})", hint="pass repo as OWNER/REPO"
            ) from exc
        host: str | None = DEFAULT_HOST
    else:
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
    a refresh was started or is already in flight. Failed reads never
    reach this function, so a no-corpus call never kicks a refresh (and
    never creates anything)."""
    fresh = _freshness(conn, slug)
    age = fresh.get("age_s")
    if age is not None and age > ctx.stale_after_s:
        try:
            _start_job(db_path, slug, ctx.sync_runner)
            fresh["refreshing"] = True
        except OSError as exc:
            _log(f"staleness refresh for {slug} could not start: {exc}")
    return build_envelope(
        data, conn=conn, slug=slug, db_path=db_path, truncated=truncated, freshness=fresh
    )


def _page(items: list, offset: int, limit: int) -> tuple[list, bool, str | None]:
    """Slice one page of an in-memory list and derive truncated + cursor.

    Only correct when `items` holds the FULL result (open_items); capped
    fetches judge truncation against their own source instead."""
    page = items[offset : offset + limit]
    truncated = offset + len(page) < len(items)
    next_cursor = str(offset + len(page)) if truncated else None
    return page, truncated, next_cursor


def _int_cursor(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


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
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        limit = _clamp_limit(arguments.get("limit", 20))
        offset = _int_cursor(arguments.get("cursor"))
        result = search(conn, query, limit=limit + offset, repo=slug)
        items = result.get("items") or []
        result["items"] = items[offset : offset + limit]
        total = result.get("total_matches")
        # Truncation is judged against the source's own match count, not
        # the capped fetch (the fetch can never exceed offset+limit).
        truncated = isinstance(total, int) and offset + len(result["items"]) < total
        if truncated:
            result["next_cursor"] = str(offset + len(result["items"]))
        return _read_envelope(conn, slug, db_path, result, truncated, ctx)
    finally:
        conn.close()


def _tool_get_item(arguments: dict, ctx) -> dict:
    number = arguments.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        raise ToolError("bad_request", "get_item needs an integer number", hint='try get_item {"number": 12}')
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        data = item(conn, number, repo=slug)
        if data is None:
            raise ToolError(
                "not_found",
                f"item #{number} not found",
                hint=f"try graph_status or search first; or run: zaxbygraph sync --repo {slug}",
                slug=slug,
                db_path=db_path,
            )
        truncated = _truncate_bodies(data, arguments.get("max_body_chars"))
        return _read_envelope(conn, slug, db_path, data, truncated, ctx)
    finally:
        conn.close()


def _tool_related(arguments: dict, ctx) -> dict:
    number = arguments.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        raise ToolError("bad_request", "related needs an integer number", hint='try related {"number": 12}')
    depth = arguments.get("depth", 1)
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
        depth = 1
    slug, db_path = _resolve_target(arguments, ctx)
    conn = _open_corpus(slug, db_path)
    try:
        data = related(conn, number, depth=depth, repo=slug)
        return _read_envelope(conn, slug, db_path, data, False, ctx)
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
    finally:
        conn.close()


def _tool_pr_overlap(arguments: dict, ctx) -> dict:
    numbers = arguments.get("numbers")
    if not isinstance(numbers, list) or not all(
        isinstance(n, int) and not isinstance(n, bool) for n in numbers
    ):
        raise ToolError(
            "bad_request",
            "pr_overlap needs numbers as a list of integers",
            hint='try pr_overlap {"numbers": [10, 11]}',
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
        limit = _clamp_limit(arguments.get("limit", 30))
        offset = _int_cursor(arguments.get("cursor"))
        result = file_history(conn, path, limit=limit + offset, repo=slug)
        entries = result.get("entries") or []
        result["entries"] = entries[offset : offset + limit]
        # file_history over-fetches limit+1 at the window size, so its own
        # truncated flag is the post-offset more-exist evidence.
        truncated = bool(result.get("truncated"))
        if truncated:
            result["next_cursor"] = str(offset + len(result["entries"]))
        return _read_envelope(conn, slug, db_path, result, truncated, ctx)
    finally:
        conn.close()


def _tool_what_closed(arguments: dict, ctx) -> dict:
    number = arguments.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        raise ToolError("bad_request", "what_closed needs an integer number", hint='try what_closed {"number": 5}')
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
        limit = _clamp_limit(arguments.get("limit", 50))
        offset = _int_cursor(arguments.get("cursor"))
        rows = open_items(conn, repo=slug)
        page, truncated, next_cursor = _page(rows, offset, limit)
        data: dict = {"items": page}
        if truncated:
            data["next_cursor"] = next_cursor
        return _read_envelope(conn, slug, db_path, data, truncated, ctx)
    finally:
        conn.close()


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
            hint="only one SELECT/WITH/EXPLAIN statement per call",
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
    finally:
        conn.close()
    limit = _clamp_limit(arguments.get("limit", 200))
    try:
        data = run_sql(guarded, statement, limit=limit, repo=slug)
    except ValueError as exc:
        # run_sql converts execution-time sqlite failures (no such
        # column/table, malformed expressions) to ValueError — the single
        # most common failure of an sql tool. It must answer as the
        # envelope error object, never escape the protocol frame.
        guarded.close()
        raise ToolError(
            "runtime",
            str(exc),
            hint="check column names with the zaxbygraph://schema resource",
            slug=slug,
            db_path=db_path,
        ) from exc
    except sqlite3.Error as exc:  # defensive: run_sql converts these today
        guarded.close()
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
    force = bool(arguments.get("force"))
    include_patches = bool(arguments.get("include_patches"))
    if runner is _default_sync_runner and (force or include_patches):
        # Advertised arguments must actually reach sync_repo; the injected
        # test seam keeps its pinned (db_path, repo) signature.
        def runner(db_path_, repo_, _force=force, _patches=include_patches):
            return _default_sync_runner(
                db_path_, repo_, force=_force, include_patches=_patches
            )
    try:
        job_id, started = _start_job(db_path, slug, runner)
    except OSError as exc:
        raise ToolError(
            "runtime", f"sync could not start: {exc}", hint="check the database directory permissions"
        ) from exc
    if started:
        data = {"job": job_id, "started": True}
    else:
        data = {"joined": True, "job": job_id}
    return build_envelope(data, conn=None, slug=slug, db_path=db_path)


# ---------------------------------------------------------------------------
# Tool registry (the typed surface; order is documentation order)

def _schema(properties: dict, required: list[str] | None = None) -> dict:
    out: dict = {"type": "object", "properties": properties, "additionalProperties": True}
    if required:
        out["required"] = required
    return out


_REPO_DB = {
    "repo": {"type": "string", "description": "OWNER/REPO slug (default: MCP roots, then the server cwd git origin)"},
    "db": {"type": "string", "description": "Explicit database path override"},
}
_LIMIT = {"type": "integer", "minimum": 1, "description": "Maximum rows to return"}
_CURSOR = {"type": "string", "description": "Opaque cursor from a previous truncated page"}

_TOOLS: list[dict] = [
    {
        "name": "graph_status",
        "description": "Sync freshness, counts, and any running sync for the resolved repo (the status command, plus live sync progress)",
        "inputSchema": _schema(dict(_REPO_DB)),
    },
    {
        "name": "search",
        "description": "Relevance-ranked full-text search over titles, bodies, labels, and comments",
        "inputSchema": _schema({"query": {"type": "string"}, "limit": _LIMIT, "cursor": _CURSOR, **_REPO_DB}, ["query"]),
    },
    {
        "name": "get_item",
        "description": "One issue/PR with labels, comments, reviews, files, and edges",
        "inputSchema": _schema(
            {
                "number": {"type": "integer"},
                "max_body_chars": {"type": "integer", "minimum": 1, "description": "Truncate item/comment/review bodies to N chars"},
                **_REPO_DB,
            },
            ["number"],
        ),
    },
    {
        "name": "related",
        "description": "One-hop (or deeper) neighborhood of an item in the graph",
        "inputSchema": _schema({"number": {"type": "integer"}, "depth": {"type": "integer", "minimum": 1}, **_REPO_DB}, ["number"]),
    },
    {
        "name": "path",
        "description": "Undirected path between two item numbers or file paths",
        "inputSchema": _schema({"a": {"type": "string"}, "b": {"type": "string"}, **_REPO_DB}, ["a", "b"]),
    },
    {
        "name": "pr_overlap",
        "description": "Shared file paths for each pair of the given PR numbers ([] for disjoint pairs) — the collision check before stacking PRs",
        "inputSchema": _schema({"numbers": {"type": "array", "items": {"type": "integer"}}, **_REPO_DB}, ["numbers"]),
    },
    {
        "name": "file_history",
        "description": "PRs that touched a path, newest first, each with the issues it closed",
        "inputSchema": _schema({"path": {"type": "string"}, "limit": _LIMIT, "cursor": _CURSOR, **_REPO_DB}, ["path"]),
    },
    {
        "name": "what_closed",
        "description": "Closing PRs and commits for an issue, each entry carrying its edge source provenance",
        "inputSchema": _schema({"number": {"type": "integer"}, **_REPO_DB}, ["number"]),
    },
    {
        "name": "open_items",
        "description": "Open issues and pull requests",
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
                "include_patches": {"type": "boolean", "description": "Store pull file patches"},
                **_REPO_DB,
            }
        ),
    },
]

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
}


# ---------------------------------------------------------------------------
# Protocol plumbing


def _send(stdout, obj: dict) -> None:
    stdout.write(json.dumps(obj, ensure_ascii=False))
    stdout.write("\n")
    stdout.flush()


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
        except ValueError:
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
    except ToolError as exc:
        if method == "tools/call":
            env = _error_envelope(exc.code_key, exc.message, exc.hint, exc.slug, exc.db_path)
            _result(
                stdout,
                req_id,
                {
                    "content": [{"type": "text", "text": json.dumps(env, ensure_ascii=False)}],
                    "isError": True,
                },
            )
        else:
            message = exc.message + (f" (hint: {exc.hint})" if exc.hint else "")
            _error(stdout, req_id, _JSONRPC_INVALID_PARAMS, message)
    except Exception as exc:  # never let one fault kill the session
        _log(traceback.format_exc())
        if has_id:
            _error(stdout, req_id, _JSONRPC_INTERNAL_ERROR, f"Internal error: {exc}")


def _dispatch(msg: dict, method: str, has_id: bool, req_id, ctx: _Context) -> None:
    stdout = ctx.stdout
    if method == "initialize":
        if not has_id:
            return
        requested = (msg.get("params") or {}).get("protocolVersion")
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
        if has_id:
            _result(stdout, req_id, {})
        return
    if method == "tools/list":
        if not has_id:
            return
        _result(stdout, req_id, {"tools": _TOOLS})
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
        env = handler(arguments, ctx)
        _result(
            stdout,
            req_id,
            {
                "content": [{"type": "text", "text": json.dumps(env, ensure_ascii=False)}],
                "isError": env.get("error") is not None,
            },
        )
        return
    if method == "resources/list":
        if not has_id:
            return
        _result(
            stdout,
            req_id,
            {
                "resources": [
                    {
                        "uri": _SCHEMA_RESOURCE_URI,
                        "name": "schema",
                        "description": "Table DDL and per-column notes for the resolved database (the schema command)",
                        "mimeType": "application/json",
                    }
                ]
            },
        )
        return
    if method == "resources/templates/list":
        if not has_id:
            return
        _result(stdout, req_id, {"resourceTemplates": []})
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
        slug, db_path = _resolve_target({}, ctx)
        conn = _open_corpus(slug, db_path)
        try:
            payload = json.dumps(describe_schema(conn), ensure_ascii=False)
        finally:
            conn.close()
        _result(
            stdout,
            req_id,
            {
                "contents": [
                    {"uri": _SCHEMA_RESOURCE_URI, "mimeType": "application/json", "text": payload}
                ]
            },
        )
        return
    # notifications/initialized, notifications/cancelled, unknown methods
    if has_id:
        _error(stdout, req_id, _JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")


__all__ = ["serve", "ToolError"]
