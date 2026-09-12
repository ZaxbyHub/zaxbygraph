from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from zaxbygraph.db import connect, init_schema
from zaxbygraph.github import GitHubError, GitHubSource
from zaxbygraph.sync import sync_repo

REPO = "acme/forgegate"


def ts(offset_s: int = 0) -> str:
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue(
    number: int,
    *,
    title: str = "title",
    body: str = "",
    state: str = "open",
    author: str = "alice",
    labels: list[dict] | None = None,
    comments: int = 0,
    updated_at: str | None = None,
    created_at: str | None = None,
    kind: str = "issue",
) -> dict:
    rec = {
        "id": 10_000 + number,
        "number": number,
        "node_id": f"I_{number}",
        "title": title,
        "body": body,
        "state": state,
        "user": {"login": author, "html_url": f"https://github.com/{author}"},
        "labels": labels or [],
        "comments": comments,
        "created_at": created_at or ts(number),
        "updated_at": updated_at or ts(number * 10),
        "closed_at": None if state == "open" else ts(number * 10 + 1),
        "locked": False,
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "url": f"https://api.github.com/repos/{REPO}/issues/{number}",
    }
    if kind == "pr":
        rec["pull_request"] = {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"}
        rec["html_url"] = f"https://github.com/{REPO}/pull/{number}"
    return rec


def pull(
    number: int,
    *,
    additions: int = 1,
    deletions: int = 1,
    changed_files: int = 1,
    commits: int = 1,
    merged: bool = False,
) -> dict:
    return {
        "id": 20_000 + number,
        "number": number,
        "additions": additions,
        "deletions": deletions,
        "changed_files": changed_files,
        "commits": commits,
        "draft": False,
        "merged_at": ts(number * 10 + 5) if merged else None,
        "merge_commit_sha": "abc" if merged else None,
        "base": {"ref": "main"},
        "head": {"ref": f"feat/{number}"},
        "user": {"login": "alice"},
    }


def comment(cid: int, body: str, author: str = "alice", created_at: str | None = None) -> dict:
    return {
        "id": cid,
        "body": body,
        "user": {"login": author, "html_url": f"https://github.com/{author}"},
        "created_at": created_at or ts(cid),
        "updated_at": created_at or ts(cid),
        "html_url": f"https://github.com/{REPO}/issues/1#issuecomment-{cid}",
    }


def review(rid: int, state: str, body: str = "", author: str = "bob") -> dict:
    return {
        "id": rid,
        "state": state,
        "body": body,
        "user": {"login": author},
        "submitted_at": ts(rid),
        "html_url": f"https://github.com/{REPO}/pull/1#pullrequestreview-{rid}",
    }


def pr_file(path: str, additions: int = 3, deletions: int = 1) -> dict:
    return {
        "filename": path,
        "status": "modified",
        "additions": additions,
        "deletions": deletions,
        "changes": additions + deletions,
        "sha": "deadbeef",
        "patch": "@@ -1 +1 @@\n-old\n+new\n",
    }


class FakeGitHubSource:
    """In-memory GitHubSource. list_issues honors since. Extra fetches are counted."""

    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self.pulls: dict[int, dict] = {}
        self.issue_comments: dict[int, list[dict]] = {}
        self.reviews: dict[int, list[dict]] = {}
        self.review_comments: dict[int, list[dict]] = {}
        self.files: dict[int, list[dict]] = {}
        self.releases: list[dict] = []
        self.extra_fetches = 0
        self.fail_after_n: int | None = None
        self.duplicate_comments = False
        self.duplicate_pr_files = False

    def add_issue(self, rec: dict) -> None:
        rec = deepcopy(rec)
        n = int(rec["number"])
        rec["comments"] = rec.get("comments", 0)
        self.issues[n] = rec
        self.issue_comments.setdefault(n, [])

    def add_pr(self, rec: dict, pull_raw: dict, files: list[dict] | None = None) -> None:
        rec = deepcopy(rec)
        rec["pull_request"] = rec.get("pull_request") or {"url": "x"}
        n = int(rec["number"])
        self.issues[n] = rec
        self.pulls[n] = deepcopy(pull_raw)
        self.files[n] = deepcopy(files or [])
        rec["comments"] = rec.get("comments", 0)
        self.issue_comments.setdefault(n, [])
        self.reviews.setdefault(n, [])
        self.review_comments.setdefault(n, [])

    def comment_on(self, number: int, body: str, author: str = "alice") -> dict:
        rec = self.issues[number]
        cid = 50_000 + len(self.issue_comments.get(number, [])) + number
        c = comment(cid, body, author=author, created_at=ts(9000 + cid % 1000))
        self.issue_comments.setdefault(number, []).append(c)
        rec["comments"] = len(self.issue_comments[number])
        # bump past any previous watermark
        rec["updated_at"] = ts(100_000 + cid)
        return c

    def fail_after(self, k: int) -> None:
        self.fail_after_n = k
        self.extra_fetches = 0

    def _tick(self) -> None:
        self.extra_fetches += 1
        if self.fail_after_n is not None and self.extra_fetches >= self.fail_after_n:
            raise GitHubError("API rate limit exceeded HTTP 429", status=429)

    def list_issues(self, since: str | None):
        items = sorted(self.issues.values(), key=lambda r: (r["updated_at"], r["number"]))
        for rec in items:
            if since is None or rec["updated_at"] >= since:
                yield deepcopy(rec)

    def get_pull(self, number: int) -> dict:
        self._tick()
        return deepcopy(self.pulls[number])

    def list_issue_comments(self, number: int) -> list[dict]:
        self._tick()
        recs = deepcopy(self.issue_comments.get(number, []))
        if self.duplicate_comments and recs:
            recs = recs + [deepcopy(recs[-1])]
        return recs

    def list_reviews(self, number: int) -> list[dict]:
        self._tick()
        return deepcopy(self.reviews.get(number, []))

    def list_review_comments(self, number: int) -> list[dict]:
        self._tick()
        return deepcopy(self.review_comments.get(number, []))

    def list_pr_files(self, number: int) -> list[dict]:
        self._tick()
        recs = deepcopy(self.files.get(number, []))
        if self.duplicate_pr_files and recs:
            recs = recs + [deepcopy(recs[-1])]
        return recs

    def list_releases(self) -> list[dict]:
        return deepcopy(self.releases)


class TempDBTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db_path = Path(self._td.name) / "history.db"
        self.conn = connect(self.db_path)
        init_schema(self.conn)
        self.src = FakeGitHubSource()

    def tearDown(self) -> None:
        self.conn.close()
        self._td.cleanup()

    def sync(self, **kwargs):
        return sync_repo(self.conn, self.src, REPO, **kwargs)

    def count(self, sql: str, params: tuple = ()) -> int:
        return int(self.conn.execute(sql, params).fetchone()[0])
