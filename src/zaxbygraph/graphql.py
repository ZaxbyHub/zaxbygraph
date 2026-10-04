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

#: Upper bound on the rendered query so the argv element stays well under
#: Windows' 32767-char CreateProcessW limit (a 40-PR chunk renders ~34.5k
#: chars and would fail with WinError 206, an OSError no GitHubError handler
#: catches). Chunks flush early when the rendered query approaches this.
_MAX_QUERY_CHARS = 24000

#: Numbers per aliased existence probe.
GRAPHQL_DELETED_PAGE = 50

#: Seconds before a hung `gh` subprocess is killed instead of stalling the
#: sync (which holds the whole-run lock) indefinitely.
GH_TIMEOUT_S = 300

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

#: Review comments ride UNDER each review (`Review.comments`); PullRequest
#: has no top-level reviewComments connection, and the draft flag is
#: `isDraft` — both validated against the live schema (issue #5 smoke).
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
    comments(first: 100) {
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
isDraft
"""

_ISSUE_FIELDS = f"""
databaseId
{_ISSUE_COMMENT_NODES}
"""

_PULL_FIELDS = f"""
databaseId
{_ISSUE_COMMENT_NODES}
{_PULL_SCALARS}
{_REVIEW_NODES}
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


def _int_or_none(value) -> int | None:
    """Coerce a scalar from an external JSON payload to int without ever
    raising out of payload mapping (string digits concatenate otherwise)."""
    if isinstance(value, bool) or value is None:
        return None if value is None else int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _connection(nodes: dict | None) -> tuple[list[dict], bool]:
    """(nodes, incomplete) for one GraphQL connection payload. A null or
    malformed connection returns ([], False) — callers distinguish a
    genuinely empty page from a section the node did not carry by checking
    key presence before calling this."""
    if not isinstance(nodes, dict):
        return [], False
    out = [n for n in (nodes.get("nodes") or []) if isinstance(n, dict)]
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
                timeout=GH_TIMEOUT_S,
            )
        except FileNotFoundError as exc:
            raise GitHubError(
                "gh CLI not found. Install GitHub CLI and authenticate with gh auth login."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubError(
                f"gh api graphql timed out after {GH_TIMEOUT_S}s"
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
            # Per-alias NOT_FOUND errors ride alongside partial data when an
            # item was deleted between the listing and this query; the nulled
            # aliases then degrade to the per-item REST path below instead of
            # aborting the whole run. Anything else fails closed.
            hard = [
                e
                for e in (errors if isinstance(errors, list) else [])
                if isinstance(e, dict)
                and str(e.get("type", "")).upper() != "NOT_FOUND"
                and "Could not resolve to an issue" not in str(e.get("message", ""))
                and "Could not resolve to a pullRequest" not in str(e.get("message", ""))
            ]
            if hard or not isinstance(data.get("data"), dict):
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
        """Nested children for a listing page, one query per size-bounded
        chunk (issue #5; Windows argv limit per the PR-13 review).

        Returns {number: children}. Children carry REST-shaped
        issue_comments / review_comments / reviews / files / pull; a
        connection that overflowed its first page adds a
        `<name>_incomplete` flag so the sync finishes it over REST for that
        item only. Items whose node did not resolve are omitted (the sync
        then uses the per-item REST path for them)."""
        children: dict[int, dict] = {}
        chunk: list[dict] = []
        for raw in items:
            chunk.append(raw)
            if len(chunk) >= GRAPHQL_CHILDREN_PAGE or self._chunk_query_len(chunk) >= _MAX_QUERY_CHARS:
                children.update(self._fetch_children_chunk(chunk))
                chunk = []
        if chunk:
            children.update(self._fetch_children_chunk(chunk))
        return children

    def _chunk_query_len(self, chunk: list[dict]) -> int:
        """Rendered length of the query the chunk would produce (upper
        bound: counts every alias at its full field-set size)."""
        total = 120  # envelope + owner/repo + rateLimit selection
        for raw in chunk:
            if not isinstance(raw, dict) or "number" not in raw:
                continue
            fields = _PULL_FIELDS if "pull_request" in raw else _ISSUE_FIELDS
            total += len(fields) + 80
        return total

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
        # A section the node does not carry (permission mask, partial error)
        # must stay ABSENT so the sync's per-item REST fallback runs for it;
        # writing [] would present a silently empty page as complete.
        if "comments" in node:
            comments, comments_more = _connection(node.get("comments"))
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
            ]
            if comments_more:
                children["issue_comments_incomplete"] = True

        if "reviews" in node:
            reviews, reviews_more = _connection(node.get("reviews"))
            review_nodes = [r for r in reviews]
            children["reviews"] = [
                {
                    "id": r.get("databaseId"),
                    "state": r.get("state"),
                    "body": r.get("body"),
                    "user": {"login": (r.get("author") or {}).get("login")},
                    "submitted_at": r.get("submittedAt"),
                    "html_url": r.get("url"),
                }
                for r in review_nodes
            ]
            if reviews_more:
                children["reviews_incomplete"] = True

            review_payloads = [_connection(r.get("comments")) for r in review_nodes]
            review_comments = [c for payload, _ in review_payloads for c in payload]
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
            ]
            # Reviews beyond the first page never had their comments
            # requested, so their review comments are incomplete too.
            rc_more = reviews_more or any(more for _, more in review_payloads)
            if rc_more:
                children["review_comments_incomplete"] = True

        if "files" in node:
            files, files_more = _connection(node.get("files"))
            children["files"] = [
                {
                    "filename": f.get("path"),
                    "status": _CHANGE_TYPE_TO_STATUS.get(f.get("changeType"), "changed"),
                    "additions": _int_or_none(f.get("additions")),
                    "deletions": _int_or_none(f.get("deletions")),
                    "changes": _int_or_none(
                        (f.get("additions") or 0) + (f.get("deletions") or 0)
                    ),
                    "sha": None,
                }
                for f in files
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
                "draft": bool(node.get("isDraft")),
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
                "query { rateLimit { remaining resetAt } "
                f"repository(owner: {_q(self.owner)}, name: {_q(self.repo)}) {{ "
                + " ".join(aliases) + " } }"
            )
            payload = self._graphql(query)
            repo = payload.get("repository")
            if not isinstance(repo, dict):
                raise GitHubError("unexpected graphql repository payload")
            resolved = 0
            for n in chunk:
                if repo.get(f"i{n}") is not None or repo.get(f"p{n}") is not None:
                    resolved += 1
                else:
                    gone[n] = "deleted"
            if resolved == 0:
                # A chunk where NOTHING resolves is indistinguishable from a
                # repo-wide visibility change or a silently degraded
                # response; refusing to mass-mark is the safe direction.
                raise GitHubError(
                    "deletion probe resolved no nodes for a whole chunk; "
                    "refusing to mark items gone without corroboration"
                )
        return gone
