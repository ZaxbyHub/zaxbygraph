from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zaxbygraph import __version__
from zaxbygraph.db import connect, connect_readonly_query, init_schema, open_existing
from zaxbygraph.doctor import doctor as doctor_run
from zaxbygraph.github import GhApiSource
from zaxbygraph.paths import (
    default_jsonl_dir,
    git_common_root,
    legacy_db_paths,
    resolve_db,
    store_db_path,
)
from zaxbygraph.query import (
    assert_read_sql,
    churn,
    export_graph,
    item,
    open_items,
    path_between,
    related,
    run_sql,
    search,
    status,
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
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
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
    return {k: row[k] for k in fields if k in row}


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
    --fields projection, the stderr identity line on success, and the
    one-line stderr error echo on failure (the human contract the error-
    channel tests pin; the structured envelope goes to stdout)."""
    fields = _fields_of(args)
    payload = data
    columns: list[str] | None = None
    if fields and error is None:
        if isinstance(data, list):
            payload = [_project(row, fields) for row in data]
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
        if error is None and isinstance(payload, list):
            for row in payload:
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
    if identity_line:
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

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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
    if repo is None:
        # Explicit no-filter: open whatever file was named, guards skipped.
        try:
            return open_existing(db_path), None, db_path
        except FileNotFoundError:
            raise _ReadFailure(
                3,
                f"no database at {db_path}; run: zaxbygraph sync --repo OWNER/REPO",
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
            raise _ReadFailure(1, f"{db_path} is not a zaxbygraph database: {exc}")
        if row is not None:
            if row["repo"] != slug:
                raise _ReadFailure(
                    2,
                    f"no corpus for {slug} in {db_path}; this database holds "
                    f"{row['repo']} - the same repo with pre-fold casing; "
                    f"run: zaxbygraph doctor --consolidate --repo {slug} to adopt it "
                    f"(or zaxbygraph sync --repo {slug} to rebuild)",
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
        )
    try:
        data = query_fn(conn, *extra, repo=repo)
        fresh = _freshness(conn, repo)
        line = _identity_line(conn, repo, db_path)
    finally:
        conn.close()
    emit_result(
        args, data, slug=repo, db_path=db_path, freshness=fresh, identity_line=line
    )
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
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
        result = sync_repo(
            conn,
            GhApiSource(*slug.split("/", 1)),
            slug,
            force=args.force,
            include_patches=args.include_patches,
            jsonl_path=jsonl,
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
    return _run_read(args, search, args.query, args.limit)


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
        )
    try:
        data = item(conn, args.number, repo=repo)
        if data is not None:
            truncated = _truncate_bodies(data, args.max_body_chars)
            fresh = _freshness(conn, repo)
            line = _identity_line(conn, repo, db_path)
    finally:
        conn.close()
    if data is None:
        return emit_failure(
            args,
            1,
            f"item #{args.number} not found",
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
    truncated object `truncated: true`. Returns whether anything was cut."""
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
    return cut


def cmd_related(args: argparse.Namespace) -> int:
    return _run_read(args, related, args.number, args.depth)


def cmd_churn(args: argparse.Namespace) -> int:
    return _run_read(args, churn, args.limit)


def cmd_open(args: argparse.Namespace) -> int:
    return _run_read(args, open_items)


def cmd_path(args: argparse.Namespace) -> int:
    return _run_read(args, path_between, args.a, args.b)


_NO_SUCH_RE = re.compile(r"^(no such column|no such table)", re.IGNORECASE)
_TABLE_RE = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


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
        )
    conn.close()
    try:
        ro_conn = connect_readonly_query(db_path)
    except (ValueError, FileNotFoundError) as exc:
        code = 3 if isinstance(exc, FileNotFoundError) else 2
        return emit_failure(args, code, str(exc), slug=repo, db_path=db_path)
    try:
        try:
            data = run_sql(ro_conn, args.statement, limit=args.limit, repo=repo)
        except ValueError as exc:
            message = str(exc)
            match = _NO_SUCH_RE.match(message)
            if match is not None:
                code_key = match.group(1).lower().replace(" ", "_")
                hint = _column_hint(ro_conn, args.statement)
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
            return 1
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
        # later positions. Suffix repeats as name_2, name_3, ... so every
        # value survives under a deterministic key; `columns` keeps the true
        # names and `--rows array` preserves positions exactly.
        seen: dict[str, int] = {}
        keys: list[str] = []
        for c in cols:
            n = seen.get(c, 0)
            seen[c] = n + 1
            keys.append(c if n == 0 else f"{c}_{n + 1}")
        rows = [dict(zip(keys, row)) for row in rows]
    emit_result(
        args,
        {"columns": cols, "rows": rows},
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zaxbygraph",
        description="Local incremental GitHub issue/PR knowledge graph.",
    )
    p.add_argument("--version", action="version", version=f"zaxbygraph {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser, *, repo: bool = True) -> None:
        sp.add_argument("--db", help="SQLite path (default: user-level store for the repo)")
        sp.add_argument(
            "--format",
            choices=("json", "compact", "jsonl", "text"),
            default=None,
            help="json when stdout is not a TTY, text when it is; jsonl/compact for streams",
        )
        sp.add_argument(
            "--fields",
            default=None,
            help="Comma-separated keys to project result rows to (e.g. repo,number)",
        )
        if repo:
            sp.add_argument("--repo", help="OWNER/REPO (default: git origin)")

    sp = sub.add_parser("sync", help="Fetch issues/PRs into the local graph")
    add_common(sp)
    sp.add_argument("--force", action="store_true", help="Ignore watermark; full pull")
    sp.add_argument("--include-patches", action="store_true", help="Store pull file patches")
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

    sp = sub.add_parser("status", help="Counts and watermark (no bodies)")
    add_common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("search", help="FTS search over titles, bodies, comments")
    add_common(sp)
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("item", help="One issue/PR with comments, files, edges")
    add_common(sp)
    sp.add_argument("number", type=int)
    sp.add_argument(
        "--max-body-chars",
        type=int,
        default=None,
        metavar="N",
        help="Truncate the item/comment/review bodies to N chars (marks truncated)",
    )
    sp.set_defaults(func=cmd_item)

    sp = sub.add_parser("related", help="1-hop neighborhood")
    add_common(sp)
    sp.add_argument("number", type=int)
    sp.add_argument("--depth", type=int, default=1)
    sp.set_defaults(func=cmd_related)

    sp = sub.add_parser("churn", help="Files ranked by PR touch count")
    add_common(sp)
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_churn)

    sp = sub.add_parser("open", help="Open issues and pull requests")
    add_common(sp)
    sp.set_defaults(func=cmd_open)

    sp = sub.add_parser("path", help="Undirected path between two item numbers or file paths")
    add_common(sp)
    sp.add_argument("a")
    sp.add_argument("b")
    sp.set_defaults(func=cmd_path)

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
    add_common(sp)
    sp.add_argument(
        "table",
        nargs="?",
        default=None,
        help="One table name (default: every table)",
    )
    sp.set_defaults(func=cmd_schema)

    sp = sub.add_parser("export-graph", help="Graphify-shaped {nodes, edges} JSON")
    add_common(sp)
    sp.set_defaults(func=cmd_export)

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
    return p


def _sync_entry(args: argparse.Namespace) -> int:
    raw = getattr(args, "jsonl_raw", False)
    args.jsonl_flag = bool(raw)
    args.jsonl = None if raw is True or raw is False else str(raw)
    return cmd_sync(args)


def main(argv: list[str] | None = None) -> int:
    # Pin the streams before argparse runs: its usage/error text is the first
    # user-facing output and can carry non-ASCII from bad arguments.
    _force_utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "repo", None):
        try:
            args.repo = validate_slug(args.repo)
        except RepoError as exc:
            # Post-parse validation: still answers with the structured
            # envelope on stdout (argparse's own usage errors stay on stderr).
            return emit_failure(args, 2, str(exc))
    return int(args.func(args))
