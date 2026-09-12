from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from zaxbygraph import __version__
from zaxbygraph.db import connect, connect_readonly_query, init_schema
from zaxbygraph.github import GhApiSource
from zaxbygraph.paths import default_db_path, default_jsonl_dir
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
from zaxbygraph.repo import RepoError, resolve_repo
from zaxbygraph.sync import SyncError, sync_repo


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


def _open_db(args: argparse.Namespace):
    path = Path(args.db) if getattr(args, "db", None) else default_db_path()
    conn = connect(path)
    init_schema(conn)
    return conn, path


def cmd_sync(args: argparse.Namespace) -> int:
    try:
        slug = resolve_repo(args.repo)
    except RepoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    owner, name = slug.split("/", 1)
    conn, db_path = _open_db(args)
    jsonl: Path | None = None
    if args.jsonl:
        jsonl = Path(args.jsonl)
    elif args.jsonl_flag:
        jsonl = default_jsonl_dir(db_path)
    try:
        result = sync_repo(
            conn,
            GhApiSource(owner, name),
            slug,
            force=args.force,
            include_patches=args.include_patches,
            jsonl_path=jsonl,
        )
    except SyncError as exc:
        _emit({"ok": False, "error": str(exc), "db": str(db_path), "repo": slug}, _want_json(args))
        return 1
    finally:
        conn.close()
    result["ok"] = True
    result["db"] = str(db_path)
    _emit(result, _want_json(args))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    conn, _ = _open_db(args)
    try:
        slug = None
        if args.repo:
            slug = resolve_repo(args.repo)
        data = status(conn, slug)
    except RepoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    _emit(data, _want_json(args))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    conn, _ = _open_db(args)
    try:
        data = search(conn, args.query, limit=args.limit, repo=args.repo)
    finally:
        conn.close()
    _emit(data, _want_json(args))
    return 0


def cmd_item(args: argparse.Namespace) -> int:
    conn, _ = _open_db(args)
    try:
        data = item(conn, args.number, repo=args.repo)
    finally:
        conn.close()
    if data is None:
        print(f"error: item #{args.number} not found", file=sys.stderr)
        return 1
    _emit(data, _want_json(args))
    return 0


def cmd_related(args: argparse.Namespace) -> int:
    conn, _ = _open_db(args)
    try:
        data = related(conn, args.number, depth=args.depth, repo=args.repo)
    finally:
        conn.close()
    _emit(data, _want_json(args))
    return 0


def cmd_churn(args: argparse.Namespace) -> int:
    conn, _ = _open_db(args)
    try:
        data = churn(conn, limit=args.limit, repo=args.repo)
    finally:
        conn.close()
    _emit(data, _want_json(args))
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    conn, _ = _open_db(args)
    try:
        data = open_items(conn, repo=args.repo)
    finally:
        conn.close()
    _emit(data, _want_json(args))
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    conn, _ = _open_db(args)
    try:
        data = path_between(conn, args.a, args.b, repo=args.repo)
    finally:
        conn.close()
    _emit(data, _want_json(args))
    return 0


def cmd_sql(args: argparse.Namespace) -> int:
    db_path = Path(args.db) if args.db else default_db_path()
    try:
        assert_read_sql(args.statement)
        conn = connect_readonly_query(db_path)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        data = run_sql(conn, args.statement)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    _emit(data, _want_json(args))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    conn, _ = _open_db(args)
    try:
        data = export_graph(conn, repo=args.repo)
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
        sp.add_argument("--db", help="SQLite path (default: .swarm or .zaxbygraph/history.db)")
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
    return p


def _sync_entry(args: argparse.Namespace) -> int:
    raw = getattr(args, "jsonl_raw", False)
    args.jsonl_flag = bool(raw)
    args.jsonl = None if raw is True or raw is False else str(raw)
    return cmd_sync(args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
