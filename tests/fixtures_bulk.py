"""Issue #5 PR-13 feedback fixtures (additive, OUTSIDE the frozen
fixtures.py: that file is checkpoint-anchored by the issue-tracer manifest,
so post-anchor additions live here instead)."""

from __future__ import annotations

from copy import deepcopy

from fixtures import (
    FakeGitHubSource,
    comment,
    issue,
    pr_file,
    pull,
    review,
    ts,
)


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
