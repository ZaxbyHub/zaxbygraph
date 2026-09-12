from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TextIO

from zaxbygraph.extract import item_kind
from zaxbygraph.github import GitHubError, GitHubSource
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
    _ensure_state_row(conn, repo, include_patches)
    if force:
        conn.execute(
            "UPDATE sync_state SET issues_since = NULL, last_error = NULL WHERE repo = ?",
            (repo,),
        )
        conn.commit()

    row = conn.execute(
        "SELECT issues_since FROM sync_state WHERE repo = ?", (repo,)
    ).fetchone()
    since = None if row is None else row["issues_since"]
    full = since is None

    jsonl_handle: TextIO | None = None
    if jsonl_path is not None:
        jsonl_path.mkdir(parents=True, exist_ok=True)
        jsonl_handle = (jsonl_path / "events.jsonl").open("a", encoding="utf-8")

    ingested = 0
    last_number: int | None = None
    try:
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
            mark_sync_finished(conn, repo, full=full)
            recount(conn, repo)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        for rec in releases:
            _write_jsonl(jsonl_handle, "release", rec)
    except (GitHubError, sqlite3.Error) as exc:
        conn.execute("BEGIN IMMEDIATE")
        set_last_error(conn, repo, str(exc))
        conn.commit()
        raise SyncError(str(exc)) from exc
    finally:
        if jsonl_handle is not None:
            jsonl_handle.close()

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
