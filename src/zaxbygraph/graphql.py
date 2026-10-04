"""GraphQL bulk source (issue #5): REST listing pages + bulk nested children.

The listing stays on the REST issues endpoint — page by page, one `gh` call
per page — because the issues-list payload defines item identity and the
watermark (a `pullRequests` connection would key items by the pulls id,
which AC8 forbids). What GraphQL removes is the per-item REST fan-out: one
aliased query answers comments/reviews/review-comments/files/pull for a
whole page of items in a single call, and any connection that overflows its
page is flagged so the sync falls back to REST for that item only.

Auth stays `gh api graphql`; nothing but stdlib parses the response. Every
page query also carries `rateLimit { remaining resetAt }` so the source
always knows the last observed budget.
"""
from __future__ import annotations

import json
import subprocess

from zaxbygraph.github import GitHubError, GhApiSource, _status_from_stderr

#: Numbers per aliased children query (issue + pullRequest aliases each).
GRAPHQL_CHILDREN_PAGE = 40

#: Numbers per aliased existence probe.
GRAPHQL_DELETED_PAGE = 50

_ISSUE_COMMENT_NODES = """
comments(first: 100) {
  totalCount
  pageInfo { hasNextPage }
  nodes {
    databaseId
    body
    createdAt
    updatedAt
    url
    author { login url }
  }
}
"""

_REVIEW_NODES = """
reviews(first: 50) {
  totalCount
  pageInfo { hasNextPage }
  nodes {
    databaseId
    state
    body
    submittedAt
    url
    author { login }
  }
}
"""

_REVIEW_COMMENT_NODES = """
reviewComments(first: 100) {
  totalCount
  pageInfo { hasNextPage }
  nodes {
    databaseId
    body
    createdAt
    updatedAt
    url
    replyTo { databaseId }
    author { login url }
  }
}
"""

_FILE_NODES = """
files(first: 100) {
  totalCount
  pageInfo { hasNextPage }
  nodes {
    path
    additions
    deletions
    changeType
  }
}
"""

_PULL_SCALARS = """
additions
deletions
changedFiles
commits { totalCount }
mergedAt
mergeCommit { oid }
baseRefName
headRefName
draft
"""

_ISSUE_FIELDS = f"""
databaseId
{_ISSUE_COMMENT_NODES}
"""

_PULL_FIELDS = f"""
databaseId
{_PULL_SCALARS}
{_REVIEW_NODES}
{_REVIEW_COMMENT_NODES}
{_FILE_NODES}
"""

_CHANGE_TYPE_TO_STATUS = {
    "ADDED": "added",
    "DELETED": "removed",
    "MODIFIED": "modified",
    "RENAMED": "renamed",
    "CHANGED": "changed",
    "COPIED": "changed",
}


def _q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _connection(nodes: dict | None, name: str) -> tuple[list[dict], bool]:
    """(nodes, incomplete) for one GraphQL connection payload."""
    if not isinstance(nodes, dict):
        return [], False
    out = list(nodes.get("nodes") or [])
    total = nodes.get("totalCount")
    incomplete = bool((nodes.get("pageInfo") or {}).get("hasNextPage"))
    if isinstance(total, int) and total > len(out):
        incomplete = True
    return out, incomplete


class GraphQLSource(GhApiSource):
    """Default sync source: REST listing pages, GraphQL bulk children."""

    def __init__(self, owner: str, repo: str, gh_bin: str = "gh") -> None:
        super().__init__(owner, repo, gh_bin)
        #: Last budget observed from a page query (None until one succeeds).
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset_at: str | None = None

    # -- transport ----------------------------------------------------------

    def _graphql(self, query: str) -> dict:
        cmd = [self.gh_bin, "api", "graphql", "-f", f"query={query}"]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise GitHubError(
                "gh CLI not found. Install GitHub CLI and authenticate with gh auth login."
            ) from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "gh api graphql failed").strip()
            raise self._with_rate_budget(
                GitHubError(err, status=_status_from_stderr(err))
            )
        try:
            data = json.loads(proc.stdout.strip() or "{}")
        except ValueError as exc:
            raise GitHubError(f"unexpected graphql payload: {exc}") from exc
        if not isinstance(data, dict):
            raise GitHubError("unexpected graphql payload")
        errors = data.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) and errors else {}
            message = str(first.get("message") or "graphql error") if isinstance(first, dict) else "graphql error"
            type_name = str(first.get("type", "")).upper() if isinstance(first, dict) else ""
            status = 429 if ("RATE_LIMIT" in type_name or "rate limit" in message.lower()) else None
            raise self._with_rate_budget(GitHubError(message, status=status))
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise GitHubError("unexpected graphql payload")
        budget = payload.get("rateLimit")
        if isinstance(budget, dict):
            remaining = budget.get("remaining")
            reset_at = budget.get("resetAt")
            if isinstance(remaining, int):
                self.rate_limit_remaining = remaining
            if isinstance(reset_at, str):
                self.rate_limit_reset_at = reset_at
        return payload

    # -- bulk children ------------------------------------------------------

    def fetch_children(self, items: list[dict]) -> dict[int, dict]:
        """Nested children for a listing page in one query per chunk.

        Returns {number: children}. Children carry REST-shaped
        issue_comments / review_comments / reviews / files / pull; a
        connection that overflowed its first page adds a
        `<name>_incomplete` flag so the sync finishes it over REST for that
        item only. Items whose node did not resolve are omitted (the sync
        then uses the per-item REST path for them)."""
        children: dict[int, dict] = {}
        for start in range(0, len(items), GRAPHQL_CHILDREN_PAGE):
            chunk = items[start:start + GRAPHQL_CHILDREN_PAGE]
            children.update(self._fetch_children_chunk(chunk))
        return children

    def _fetch_children_chunk(self, chunk: list[dict]) -> dict[int, dict]:
        aliases: list[str] = []
        wanted: list[tuple[int, str]] = []
        for raw in chunk:
            if not isinstance(raw, dict) or "number" not in raw:
                continue
            number = int(raw["number"])
            alias = ("p" if "pull_request" in raw else "i") + str(number)
            fields = _PULL_FIELDS if "pull_request" in raw else _ISSUE_FIELDS
            aliases.append(f"{alias}: {'pullRequest' if 'pull_request' in raw else 'issue'}(number: {number}) {{{fields}}}")
            wanted.append((number, alias))
        if not aliases:
            return {}
        query = (
            "query { rateLimit { remaining resetAt } "
            f"repository(owner: {_q(self.owner)}, name: {_q(self.repo)}) {{ "
            + " ".join(aliases) + " } }"
        )
        payload = self._graphql(query)
        repo = payload.get("repository")
        if not isinstance(repo, dict):
            raise GitHubError("unexpected graphql repository payload")
        out: dict[int, dict] = {}
        for number, alias in wanted:
            node = repo.get(alias)
            if not isinstance(node, dict):
                continue
            out[number] = self._children_from_node(node)
        return out

    def _children_from_node(self, node: dict) -> dict:
        children: dict = {}
        comments, comments_more = _connection(node.get("comments"), "comments")
        children["issue_comments"] = [
            {
                "id": c.get("databaseId"),
                "body": c.get("body"),
                "user": {
                    "login": (c.get("author") or {}).get("login"),
                    "html_url": (c.get("author") or {}).get("url"),
                },
                "created_at": c.get("createdAt"),
                "updated_at": c.get("updatedAt"),
                "html_url": c.get("url"),
                "in_reply_to_id": None,
            }
            for c in comments
            if isinstance(c, dict)
        ]
        if comments_more:
            children["issue_comments_incomplete"] = True

        if "reviews" in node:
            reviews, reviews_more = _connection(node.get("reviews"), "reviews")
            children["reviews"] = [
                {
                    "id": r.get("databaseId"),
                    "state": r.get("state"),
                    "body": r.get("body"),
                    "user": {"login": (r.get("author") or {}).get("login")},
                    "submitted_at": r.get("submittedAt"),
                    "html_url": r.get("url"),
                }
                for r in reviews
                if isinstance(r, dict)
            ]
            if reviews_more:
                children["reviews_incomplete"] = True

            review_comments, rc_more = _connection(node.get("reviewComments"), "reviewComments")
            children["review_comments"] = [
                {
                    "id": c.get("databaseId"),
                    "body": c.get("body"),
                    "user": {
                        "login": (c.get("author") or {}).get("login"),
                        "html_url": (c.get("author") or {}).get("url"),
                    },
                    "created_at": c.get("createdAt"),
                    "updated_at": c.get("updatedAt"),
                    "html_url": c.get("url"),
                    "in_reply_to_id": ((c.get("replyTo") or {}).get("databaseId")),
                }
                for c in review_comments
                if isinstance(c, dict)
            ]
            if rc_more:
                children["review_comments_incomplete"] = True

            files, files_more = _connection(node.get("files"), "files")
            children["files"] = [
                {
                    "filename": f.get("path"),
                    "status": _CHANGE_TYPE_TO_STATUS.get(f.get("changeType"), "changed"),
                    "additions": f.get("additions"),
                    "deletions": f.get("deletions"),
                    "changes": (f.get("additions") or 0) + (f.get("deletions") or 0),
                    "sha": None,
                }
                for f in files
                if isinstance(f, dict)
            ]
            if files_more:
                children["files_incomplete"] = True

            merge_commit = node.get("mergeCommit") or {}
            children["pull"] = {
                "additions": node.get("additions"),
                "deletions": node.get("deletions"),
                "changed_files": node.get("changedFiles"),
                "commits": (node.get("commits") or {}).get("totalCount"),
                "merged_at": node.get("mergedAt"),
                "merge_commit_sha": merge_commit.get("oid"),
                "base": {"ref": node.get("baseRefName")},
                "head": {"ref": node.get("headRefName")},
                "draft": bool(node.get("draft")),
            }
        return children

    # -- deletion oracle ----------------------------------------------------

    def check_deleted(self, numbers: list[int]) -> dict[int, str]:
        """Existence probe over stored numbers (issue #5 AC5).

        A number is reported deleted only when neither its issue nor its
        pullRequest node resolves. GraphQL cannot distinguish a transferred
        issue from a deleted one in this repo, so the verdict is always
        'deleted' here; sources with richer signals may report
        'transferred'."""
        gone: dict[int, str] = {}
        unique = sorted({int(n) for n in numbers})
        for start in range(0, len(unique), GRAPHQL_DELETED_PAGE):
            chunk = unique[start:start + GRAPHQL_DELETED_PAGE]
            aliases: list[str] = []
            for n in chunk:
                aliases.append(f"i{n}: issue(number: {n}) {{ number }}")
                aliases.append(f"p{n}: pullRequest(number: {n}) {{ number }}")
            query = (
                f"query {{ repository(owner: {_q(self.owner)}, name: {_q(self.repo)}) {{ "
                + " ".join(aliases) + " } }"
            )
            payload = self._graphql(query)
            repo = payload.get("repository")
            if not isinstance(repo, dict):
                raise GitHubError("unexpected graphql repository payload")
            for n in chunk:
                if repo.get(f"i{n}") is None and repo.get(f"p{n}") is None:
                    gone[n] = "deleted"
        return gone
