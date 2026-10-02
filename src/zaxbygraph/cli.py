from __future__ import annotations

import argparse
import json
import sqlite3
import sys
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
from zaxbygraph.repo import DEFAULT_HOST, RepoError, remote_info, resolve_repo, validate_slug
from zaxbygraph.sync import (
    SyncError,
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


def _want_json(args: argparse.Namespace) -> bool:
    fmt = getattr(args, "format", None)
    if fmt == "json":
        return True
    if fmt == "text":
        return False
    return not sys.stdout.isatty()


def _emit(data: Any, as_json: bool) -> None:
    if as_json:
        json.dump(data, sys.stdout, indent=2, ensure_ascii=False, default=str)
        sys.stdout.write("\n")
        return
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
            row = conn.execute(
                "SELECT repo FROM sync_state WHERE repo = ?", (slug,)
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise _ReadFailure(1, f"{db_path} is not a zaxbygraph database: {exc}")
        if row is None:
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
    try:
        conn, repo, _path = _open_for_read(args)
    except _ReadFailure as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.code
    try:
        data = query_fn(conn, *extra, repo=repo)
    finally:
        conn.close()
    _emit(data, _want_json(args))
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    try:
        if args.repo:
            slug = validate_slug(args.repo)
            host = DEFAULT_HOST
        else:
            host, slug = remote_info()
    except RepoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
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
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # A storage fault before the sync starts is reported as the same
        # result object, not a traceback (AGENTS.md: reported, not swallowed).
        if conn is not None:
            conn.close()
        _emit({"ok": False, "error": str(exc), "db": str(db_path), "repo": slug}, _want_json(args))
        return 1
    lock = acquire_sync_lock(db_path, wait=bool(getattr(args, "wait", False)))
    if lock is None:
        conn.close()
        _emit(
            {
                "ok": True,
                "joined": True,
                "repo": slug,
                "db": str(db_path),
            },
            _want_json(args),
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
        _emit({"ok": False, "error": str(exc), "db": str(db_path), "repo": slug}, _want_json(args))
        return 1
    except Exception as exc:  # recorded by sync_repo; reported, not a traceback
        _emit({"ok": False, "error": str(exc), "db": str(db_path), "repo": slug}, _want_json(args))
        return 1
    finally:
        lock.release_owned()
        conn.close()
    result["ok"] = True
    result["db"] = str(db_path)
    _emit(result, _want_json(args))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    return _run_read(args, status)


def cmd_search(args: argparse.Namespace) -> int:
    return _run_read(args, search, args.query, args.limit)


def cmd_item(args: argparse.Namespace) -> int:
    try:
        conn, repo, _path = _open_for_read(args)
    except _ReadFailure as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.code
    try:
        data = item(conn, args.number, repo=repo)
    finally:
        conn.close()
    if data is None:
        print(f"error: item #{args.number} not found", file=sys.stderr)
        return 1
    _emit(data, _want_json(args))
    return 0


def cmd_related(args: argparse.Namespace) -> int:
    return _run_read(args, related, args.number, args.depth)


def cmd_churn(args: argparse.Namespace) -> int:
    return _run_read(args, churn, args.limit)


def cmd_open(args: argparse.Namespace) -> int:
    return _run_read(args, open_items)


def cmd_path(args: argparse.Namespace) -> int:
    return _run_read(args, path_between, args.a, args.b)


def cmd_sql(args: argparse.Namespace) -> int:
    # Write rejection comes FIRST: the read-only contract is about the
    # statement, and it must keep its exit 2 before any DB access.
    try:
        assert_read_sql(args.statement)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        conn, repo, db_path = _open_for_read(args)
    except _ReadFailure as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.code
    conn.close()
    try:
        ro_conn = connect_readonly_query(db_path)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3 if isinstance(exc, FileNotFoundError) else 2
    try:
        try:
            data = run_sql(ro_conn, args.statement, repo=repo)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        ro_conn.close()
    _emit(data, _want_json(args))
    return 0


def cmd_where(args: argparse.Namespace) -> int:
    try:
        repo, host = _resolved_repo(args)
    except RepoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    serving, _chain = resolve_db(repo, explicit=getattr(args, "db", None), host=host)
    cwd = Path.cwd()
    common = git_common_root(cwd)
    store_path = store_db_path(host, repo)
    exists = False
    items = 0
    watermark = None
    complete = False
    if store_path.exists():
        try:
            conn = open_existing(store_path)
            try:
                row = conn.execute(
                    "SELECT issues_since, last_error, full_sync_pending FROM sync_state"
                    " WHERE repo = ?",
                    (repo,),
                ).fetchone()
                items = int(
                    conn.execute(
                        "SELECT COUNT(*) AS c FROM items WHERE repo = ?", (repo,)
                    ).fetchone()["c"]
                )
            finally:
                conn.close()
            if row is not None:
                watermark = row["issues_since"]
                complete = not row["full_sync_pending"] and row["last_error"] is None
                exists = True
        except sqlite3.DatabaseError:
            pass
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
    }
    _emit(data, _want_json(args))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        if args.repo:
            repo, host = validate_slug(args.repo), DEFAULT_HOST
        else:
            host, repo = remote_info()
    except RepoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    store_db = store_db_path(host, repo)
    if args.db:
        store_db = Path(args.db)
    scans = [Path(s) for s in args.scan]
    data = doctor_run(
        Path.cwd(),
        repo,
        store_db,
        extra_scans=scans,
        consolidate_flag=args.consolidate,
    )
    data["store"] = str(store_db)
    _emit(data, _want_json(args))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    try:
        conn, repo, _path = _open_for_read(args)
    except _ReadFailure as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.code
    try:
        data = export_graph(conn, repo=repo)
    finally:
        conn.close()
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
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
            choices=("json", "text"),
            default=None,
            help="json when stdout is not a TTY, text when it is",
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
    sp.set_defaults(func=cmd_sql)

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
            print(f"error: {exc}", file=sys.stderr)
            return 2
    return int(args.func(args))
