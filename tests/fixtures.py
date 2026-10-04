from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest

from zaxbygraph.db import connect, init_schema
from zaxbygraph.github import GitHubError, GitHubSource
from zaxbygraph.sync import sync_repo

REPO = "acme/forgegate"


def scrubbed_env() -> dict:
    """Child environment without the UTF-8 overrides that mask the locale
    decode/encode defects (issue #1). One definition for every test module
    that spawns a probe child, so the hostile-locale regime is corrected in
    one place.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO")
    }
    if os.name == "posix":
        env.update(LC_ALL="C", LANG="C", PYTHONCOERCECLOCALE="0")
    return env


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


# ==== issue #5 fetch-layer acceptance fakes (append-only) ====================
#
# Contracts these fakes pin. The reworked fetch layer must satisfy them; the
# fakes are additive and leave FakeGitHubSource above untouched.
#
# Listing protocol: list_issues(since) stays a lazy iterator and may yield
# either one item dict (REST shape, the --source rest path) or one PAGE - a
# list of item dicts (GraphQL bulk shape). A page must be consumed as it
# arrives, so everything delivered before a mid-listing failure is durable.
#
# Nested payload keys: a bulk item dict may carry its children inline under
# REST-named keys - "issue_comments", "review_comments", "reviews" (lists of
# the exact payloads the per-item REST methods return), "files" (list of REST
# pr-file payloads) and "pull" (the REST pull payload). A present section
# that is not flagged incomplete replaces every per-item REST call for that
# connection. A "<name>_incomplete" boolean (e.g. "files_incomplete") marks a
# truncated section the sync must finish through the per-item REST fallback
# for that item only.
#
# Rate limit: a GitHubError may carry machine-readable budget attributes
# rate_limit_remaining (int) and rate_limit_reset (ISO-8601 Z string). Such an
# error must be slept out through the sync module's `time` (injectable clock)
# and retried, not aborted; the observed window is reported as top-level
# sync-result keys and sync_state columns named rate_limit_remaining and
# rate_limit_reset_at (query.status surfaces them via SELECT *).
#
# Deletion: a source may expose check_deleted(numbers) -> {number: state}
# with state 'deleted' | 'transferred' for items GitHub no longer lists. It
# is consulted only after a listing that started at since=None drains to
# completion; an item absent from an incremental listing means "not updated",
# never "gone". Marking writes the verdict into items.state and leaves a
# fetch_log row (resource 'item', resource_id str(number)) whose note names
# the verdict.


class RateLimitedError(GitHubError):
    """A 429 with a machine-readable budget: hits remaining and the ISO-Z
    instant the window resets."""

    def __init__(self, message: str, *, remaining: int, reset_at: str) -> None:
        super().__init__(message, status=429)
        self.rate_limit_remaining = remaining
        self.rate_limit_reset = reset_at


class PagedFailureSource:
    """Streams listing PAGES lazily, then dies mid-listing (issue #5 AC1).

    Page 1 is genuinely delivered to the consumer before the page-2 fetch
    raises, so a streaming sync keeps it and a buffering sync loses it. The
    fixture items are plain zero-comment issues: no per-item fallback is ever
    legitimate, so those methods guard with AssertionError."""

    def __init__(self, pages: list[list[dict]]) -> None:
        self.pages = deepcopy(pages)

    def list_issues(self, since: str | None):
        for page in self.pages:
            if since is not None:
                page = [r for r in page if r.get("updated_at", "") >= since]
            yield deepcopy(page)
        raise GitHubError("HTTP 502", status=502)

    def _unreachable(self, name: str):
        raise AssertionError(f"{name} must not be called for this fixture")

    def get_pull(self, number: int) -> dict:
        self._unreachable("get_pull")

    def list_issue_comments(self, number: int) -> list[dict]:
        self._unreachable("list_issue_comments")

    def list_reviews(self, number: int) -> list[dict]:
        self._unreachable("list_reviews")

    def list_review_comments(self, number: int) -> list[dict]:
        self._unreachable("list_review_comments")

    def list_pr_files(self, number: int) -> list[dict]:
        self._unreachable("list_pr_files")

    def list_releases(self) -> list[dict]:
        return []


class BulkNestedSource(FakeGitHubSource):
    """Bulk-pages source (issue #5 AC2/AC3): list_issues yields item dicts
    whose nested connections ride inline under the REST-named keys. The
    per-item REST methods exist only as the overflow fallback and every call
    is counted."""

    def __init__(self) -> None:
        super().__init__()
        self.fallback_calls = 0
        self.files_fallback_numbers: list[int] = []
        self.incomplete_files: set[int] = set()

    def list_issues(self, since: str | None):
        items = sorted(self.issues.values(), key=lambda r: (r["updated_at"], r["number"]))
        for rec in items:
            if since is not None and rec["updated_at"] < since:
                continue
            n = int(rec["number"])
            bulk = deepcopy(rec)
            bulk["issue_comments"] = deepcopy(self.issue_comments.get(n, []))
            bulk["review_comments"] = deepcopy(self.review_comments.get(n, []))
            bulk["reviews"] = deepcopy(self.reviews.get(n, []))
            if n in self.incomplete_files:
                bulk["files"] = deepcopy(self.files.get(n, []))[:1]
                bulk["files_incomplete"] = True
            else:
                bulk["files"] = deepcopy(self.files.get(n, []))
            if n in self.pulls:
                bulk["pull"] = deepcopy(self.pulls[n])
            yield bulk

    def get_pull(self, number: int) -> dict:
        self.fallback_calls += 1
        return super().get_pull(number)

    def list_issue_comments(self, number: int) -> list[dict]:
        self.fallback_calls += 1
        return super().list_issue_comments(number)

    def list_reviews(self, number: int) -> list[dict]:
        self.fallback_calls += 1
        return super().list_reviews(number)

    def list_review_comments(self, number: int) -> list[dict]:
        self.fallback_calls += 1
        return super().list_review_comments(number)

    def list_pr_files(self, number: int) -> list[dict]:
        self.fallback_calls += 1
        self.files_fallback_numbers.append(number)
        return super().list_pr_files(number)


class RateLimitSource(FakeGitHubSource):
    """Serves one item, then rate-limits the next per-item call once (issue #5
    AC4). The retry after the sleep must succeed."""

    REMAINING = 0
    RESET_AT = "2026-10-03T12:20:00Z"

    def __init__(self) -> None:
        super().__init__()
        self.limit_failures_left = 1

    def list_issue_comments(self, number: int) -> list[dict]:
        if self.limit_failures_left > 0:
            self.limit_failures_left -= 1
            raise RateLimitedError(
                "API rate limit exceeded HTTP 429",
                remaining=self.REMAINING,
                reset_at=self.RESET_AT,
            )
        return super().list_issue_comments(number)


class DeletingSource(FakeGitHubSource):
    """Full-listing source with a deletion oracle (issue #5 AC5):
    check_deleted maps gone numbers to 'deleted' or 'transferred'."""

    def __init__(self) -> None:
        super().__init__()
        self.deletions: dict[int, str] = {}
        self.check_deleted_calls = 0

    def check_deleted(self, numbers) -> dict[int, str]:
        self.check_deleted_calls += 1
        return {n: self.deletions[n] for n in numbers if n in self.deletions}


class PageShapedBulkSource(FakeGitHubSource):
    """The REAL GraphQLSource's shape (issue #5 PR-13 review T1/T2): the
    listing yields PAGES of plain item dicts (no inline children) and the
    bulk children arrive through fetch_children - so a sync run exercises
    the production provided-map wiring no inline fixture reaches. The
    per-item REST methods remain as the fallback and count their calls."""

    def __init__(self) -> None:
        super().__init__()
        self.fallback_calls = 0
        self.fetch_children_calls: list[int] = []

    def list_issues(self, since: str | None):
        items = sorted(self.issues.values(), key=lambda r: (r["updated_at"], r["number"]))
        page: list[dict] = []
        for rec in items:
            if since is not None and rec["updated_at"] < since:
                continue
            page.append(deepcopy(rec))
            if len(page) >= 100:
                yield page
                page = []
        if page:
            yield page

    def fetch_children(self, items) -> dict[int, dict]:
        self.fetch_children_calls.append(len(items))
        out: dict[int, dict] = {}
        for raw in items:
            n = int(raw["number"])
            children: dict = {}
            if n in self.pulls:
                children["pull"] = deepcopy(self.pulls[n])
            children["issue_comments"] = deepcopy(self.issue_comments.get(n, []))
            children["reviews"] = deepcopy(self.reviews.get(n, []))
            children["review_comments"] = deepcopy(self.review_comments.get(n, []))
            children["files"] = deepcopy(self.files.get(n, []))
            out[n] = children
        return out

    def get_pull(self, number: int) -> dict:
        self.fallback_calls += 1
        return super().get_pull(number)

    def list_issue_comments(self, number: int) -> list[dict]:
        self.fallback_calls += 1
        return super().list_issue_comments(number)

    def list_reviews(self, number: int) -> list[dict]:
        self.fallback_calls += 1
        return super().list_reviews(number)

    def list_review_comments(self, number: int) -> list[dict]:
        self.fallback_calls += 1
        return super().list_review_comments(number)

    def list_pr_files(self, number: int) -> list[dict]:
        self.fallback_calls += 1
        return super().list_pr_files(number)


def add_page_prs(src: PageShapedBulkSource, count: int) -> None:
    """Same corpus shape as add_bulk_prs but for PageShapedBulkSource."""
    for n in range(1, count + 1):
        src.add_pr(
            issue(n, title=f"pr-{n}", comments=2, kind="pr", updated_at=ts(n * 100)),
            pull(n, changed_files=2),
            files=[pr_file(f"src/mod{n}/a.py"), pr_file(f"src/mod{n}/b.py")],
        )
        for slot in range(2):
            src.issue_comments[n].append(comment(50_000 + n * 10 + slot, f"note {slot}"))
        src.issues[n]["comments"] = 2
        src.reviews[n] = [review(60_000 + n, "APPROVED", body="ok")]
        src.review_comments[n] = [comment(70_000 + n, "inline note", author="bob")]


def add_bulk_prs(src: BulkNestedSource, count: int) -> None:
    """count PRs, each with 2 issue comments, 1 review, 1 review comment and
    2 files - every connection complete on its first page. Comment ids are
    allocated per (number, slot): comment_on's len-based scheme collides
    across numbers, and comments UNIQUE(repo, kind, github_id) would silently
    fold adjacent PRs' comments into one row."""
    for n in range(1, count + 1):
        src.add_pr(
            issue(n, title=f"pr-{n}", comments=2, kind="pr", updated_at=ts(n * 100)),
            pull(n, changed_files=2),
            files=[pr_file(f"src/mod{n}/a.py"), pr_file(f"src/mod{n}/b.py")],
        )
        for slot in range(2):
            src.issue_comments[n].append(comment(50_000 + n * 10 + slot, f"note {slot}"))
        src.issues[n]["comments"] = 2
        src.reviews[n] = [review(60_000 + n, "APPROVED", body="ok")]
        src.review_comments[n] = [comment(70_000 + n, "inline note", author="bob")]
