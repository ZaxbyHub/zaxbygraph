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

#: Hard per-request cap on `timelineItems` (GitHub rejects first/last beyond
#: 250 on that connection with EXCESSIVE_PAGINATION — live-verified, #6).
_TIMELINE_PAGE_MAX = 250

#: Continuation page size while draining a flagged timeline (stays well under
#: the per-request cap).
_TIMELINE_CONTINUATION_PAGE = 100

#: Hard ceiling on continuation pages per item: a server that keeps reporting
#: hasNextPage with a non-advancing cursor can never spin the loop forever.
_MAX_CONTINUATION_PAGES = 50

#: Timeline events retained per item. Beyond this the retained set is the
#: contiguous NEWEST 500 (closed events are typically the newest facts), and
#: the sync records the truncation in fetch_log (#6).
_MAX_TIMELINE_EVENTS = 500

#: Sanity bound on the rendered query. The query travels on stdin (issue
#: #23), so Windows' argv limit no longer applies; this only keeps a runaway
#: field set from producing an unbounded request. The binding chunk size is
#: GRAPHQL_CHILDREN_PAGE.
_MAX_QUERY_CHARS = 100_000

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
    authorAssociation
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
        authorAssociation
        replyTo { databaseId }
        author { login url }
      }
    }
  }
}
"""

_CLOSING_REFS_NODES = """
closingIssuesReferences(first: 100) {
  totalCount
  pageInfo { hasNextPage }
  nodes {
    number
    repository { nameWithOwner }
  }
}
"""

#: GitHub-reported link selections (issue #27). parent/duplicateOf are
#: SINGULAR nodes (live-schema verified, plan-critic round 1) and may be
#: JSON null (no parent / not a duplicate) — key presence is the carried
#: signal, null is a cleared value.
_LINKED_NODES = """
blockedBy(first: 50) {
  totalCount
  pageInfo { hasNextPage }
  nodes {
    number
    repository { nameWithOwner }
  }
}
"""

_PARENT_NODE = """
parent {
  number
  repository { nameWithOwner }
}
"""

_DUPLICATE_NODE = """
duplicateOf {
  number
  repository { nameWithOwner }
}
"""

_OPEN_PR_NODES = """
closedByPullRequestsReferences(first: 50, includeClosedPrs: false) {
  totalCount
  pageInfo { hasNextPage }
  nodes {
    number
    repository { nameWithOwner }
  }
}
"""

#: Commit messages (issue #27 AC5). The scalar totalCount the v7 selection
#: carried survives inside the node-bearing connection — the probe drop-list
#: keeps the SAME `commits` field name so the fragment tuple and the rendered
#: query cannot drift (plan-critic round 1).
_COMMITS_NODES = """
commits(first: 50) {
  totalCount
  pageInfo { hasNextPage }
  nodes {
    commit {
      oid
      messageHeadline
      messageBody
    }
  }
}
"""

#: Timeline event types the page queries request. REOPENED_EVENT (issue #27
#: AC7) rides the same connection; a host whose schema lacks it is degraded
#: by the capability probe's ENUM introspection (unknown = fail open and
#: include, the #23 rule).
_TIMELINE_EVENT_TYPES = ("CLOSED_EVENT", "CROSS_REFERENCED_EVENT", "REOPENED_EVENT")
_TIMELINE_TYPES_ALL = "[" + ", ".join(_TIMELINE_EVENT_TYPES) + "]"

_TIMELINE_NODES_TMPL = """
timelineItems(first: 50, itemTypes: %(types)s) {
  totalCount
  pageInfo { hasNextPage endCursor }
  nodes {
    __typename
    ... on ClosedEvent {
      createdAt
      actor { login }
      closer {
        __typename
        ... on Commit { oid }
        ... on PullRequest { number }
      }
    }
    ... on CrossReferencedEvent {
      createdAt
      actor { login }
      isCrossRepository
      willCloseTarget
      source {
        __typename
        ... on Issue { number repository { nameWithOwner } }
        ... on PullRequest { number repository { nameWithOwner } }
      }
    }%(reopened)s
  }
}
"""

_REOPENED_FRAGMENT = """
    ... on ReopenedEvent {
      createdAt
    }
"""


def _timeline_nodes(types: str = _TIMELINE_TYPES_ALL) -> str:
    """The timelineItems selection at a given itemTypes list. Page queries
    AND continuation queries render through the same helper (the continuation
    path calls GraphQLSource._timeline_selection, which shares this)."""
    reopened = _REOPENED_FRAGMENT if "REOPENED_EVENT" in types else ""
    return _TIMELINE_NODES_TMPL % {"types": types, "reopened": reopened}


#: Timeline events ride every page query (issue #6): closed events carry the
#: closer union (commit sha or pull request number), cross-references carry
#: source/target so direction is stored as carried per side; reopen events
#: (issue #27) land in item_events, never in edges.
_TIMELINE_NODES = _timeline_nodes()

#: Field-selection fragments for the page query, each tagged with the exact
#: schema field it renders; _PULL_TYPE_BLOCKS consumes this tuple. A
#: capability probe drops every fragment whose field the host schema lacks
#: (issue #23, per field).
_PULL_SCALAR_FRAGMENTS = (
    ("additions", "additions"),
    ("deletions", "deletions"),
    ("changedFiles", "changedFiles"),
    ("commits", _COMMITS_NODES),
    ("mergedAt", "mergedAt"),
    ("mergeCommit", "mergeCommit { oid }"),
    ("baseRefName", "baseRefName"),
    ("headRefName", "headRefName"),
    ("isDraft", "isDraft"),
    ("mergedBy", "mergedBy { login }"),
)

#: Issue-side selections, tagged with the exact schema field each renders so
#: the capability probe can drop them PER FIELD (issue #23).
_ISSUE_TYPE_BLOCKS = (
    ("comments", _ISSUE_COMMENT_NODES),
    ("blockedBy", _LINKED_NODES),
    ("parent", _PARENT_NODE),
    ("duplicateOf", _DUPLICATE_NODE),
    ("closedByPullRequestsReferences", _OPEN_PR_NODES),
    ("timelineItems", _TIMELINE_NODES),
)

_ISSUE_FIELDS = "\n".join(["databaseId", *(block for _, block in _ISSUE_TYPE_BLOCKS)])

_PULL_TYPE_BLOCKS = (
    ("comments", _ISSUE_COMMENT_NODES),
    *_PULL_SCALAR_FRAGMENTS,
    ("reviews", _REVIEW_NODES),
    ("files", _FILE_NODES),
    ("timelineItems", _TIMELINE_NODES),
    ("closingIssuesReferences", _CLOSING_REFS_NODES),
)

_PULL_FIELDS = "\n".join(["databaseId", *(block for _, block in _PULL_TYPE_BLOCKS)])

#: Field-selection fragments for the page query, each tagged with the exact
#: schema field it renders. A capability probe drops every fragment whose
#: field the host's schema no longer carries — PER FIELD, not per block, so
#: drift on any single selected field degrades that selection instead of
#: aborting the sync with a validation error (issue #23). Omitted names are
#: recorded for fetch_log.
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


def _commit_message(commit: dict) -> str | None:
    """Headline + body composed into one message string (issue #27 AC5); a
    null messageBody degrades to the headline alone."""
    headline = commit.get("messageHeadline")
    body = commit.get("messageBody")
    if not headline:
        return None
    if body:
        return headline + "\n\n" + body
    return headline


def _comment_payload(node: dict, *, in_reply_to_id=None) -> dict:
    """One comment node -> the REST-shaped dict ingest consumes. When the
    selection carried `authorAssociation` the key is mapped through; when the
    selection lacked it the key stays ABSENT so the store's snapshot fallback
    preserves a REST-native value instead of clearing it (issue #24 AC7)."""
    payload = {
        "id": node.get("databaseId"),
        "body": node.get("body"),
        "user": {
            "login": (node.get("author") or {}).get("login"),
            "html_url": (node.get("author") or {}).get("url"),
        },
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "html_url": node.get("url"),
        "in_reply_to_id": in_reply_to_id,
    }
    if "authorAssociation" in node:
        payload["author_association"] = node.get("authorAssociation")
    return payload


def _timeline_event(node: dict) -> dict:
    """One timelineItems node -> the normalized event dict the extract layer
    consumes (transport-agnostic shape; #6). Sub-objects are hardening
    against malformed payloads: an `or {}` guards null, and an isinstance
    guard keeps a non-object JSON value (string/list/number) from raising."""
    typename = node.get("__typename")
    if typename == "ClosedEvent":
        closer = node.get("closer")
        if not isinstance(closer, dict):
            closer = {}
        actor = node.get("actor")
        closer_type = closer.get("__typename")
        return {
            "type": "closed",
            "created_at": node.get("createdAt"),
            "actor_login": actor.get("login")
            if isinstance(actor, dict)
            else None,
            "commit_id": closer.get("oid") if closer_type == "Commit" else None,
            "closer_type": {"Commit": "commit", "PullRequest": "pull_request"}.get(closer_type),
            "closer_number": _int_or_none(closer.get("number"))
            if closer_type == "PullRequest"
            else None,
        }
    if typename == "ReopenedEvent":
        return {
            "type": "reopened",
            "created_at": node.get("createdAt"),
        }
    if typename == "CrossReferencedEvent":
        source = node.get("source")
        if not isinstance(source, dict):
            source = {}
        actor = node.get("actor")
        source_repo = source.get("repository")
        return {
            "type": "cross_referenced",
            "created_at": node.get("createdAt"),
            "actor_login": actor.get("login")
            if isinstance(actor, dict)
            else None,
            "source_typename": source.get("__typename"),
            "source_number": _int_or_none(source.get("number")),
            "source_repo": source_repo.get("nameWithOwner")
            if isinstance(source_repo, dict)
            else None,
            "is_cross_repository": bool(node.get("isCrossRepository")),
        }
    return {"type": str(typename or "unknown").lower()}


def _timeline_selection(args: str, types: str = _TIMELINE_TYPES_ALL) -> str:
    """The timelineItems selection shared by the page query and the
    continuation/replacement queries (always inside the 250-per-request
    GitHub cap). `types` is the capability-degraded itemTypes list the source
    computes (issue #27) — page and continuation cannot drift."""
    reopened = (
        " ... on ReopenedEvent { createdAt }" if "REOPENED_EVENT" in types else ""
    )
    return (
        "timelineItems(%s itemTypes: %s) {"
        " totalCount pageInfo { hasNextPage hasPreviousPage startCursor endCursor }"
        " nodes { __typename"
        " ... on ClosedEvent { createdAt actor { login } closer { __typename"
        " ... on Commit { oid } ... on PullRequest { number } } }"
        " ... on CrossReferencedEvent { createdAt actor { login } isCrossRepository"
        " willCloseTarget source { __typename"
        " ... on Issue { number repository { nameWithOwner } }"
        " ... on PullRequest { number repository { nameWithOwner } } } }%s } }"
    ) % (args, types, reopened)


class GraphQLSource(GhApiSource):
    """Default sync source: REST listing pages, GraphQL bulk children."""

    def __init__(self, owner: str, repo: str, gh_bin: str = "gh") -> None:
        super().__init__(owner, repo, gh_bin)
        #: Last budget observed from a page query (None until one succeeds).
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset_at: str | None = None
        #: Active capability map {"Issue": {fields}, "PullRequest": {fields}}
        #: once a probe ran or a persisted cache was attached (issue #23).
        #: None means "no capability knowledge" - the full field set is sent.
        self._capabilities: dict[str, set[str]] | None = None
        #: Field names omitted from page queries by the active capabilities,
        #: for sync to record in fetch_log.
        self.omitted_fields: list[str] = []
        #: Section-scoped runtime errors dropped by the last chunk query,
        #: {number: [(section, error-type, message-head), ...]} (issue #23).
        self.last_fetch_drops: dict[int, list[tuple[str, str, str]]] = {}

    # -- capability probe ---------------------------------------------------

    def probe_capabilities(self) -> dict[str, set[str]]:
        """Introspect the Issue/PullRequest field sets once and ACTIVATE the
        result for query building on this source (issue #23).

        A second call costs no introspection. The page-query builder consults
        the active map and omits absent fields; sync persists the map in the
        `meta` table keyed by host and gh version and re-attaches it with
        attach_capabilities on later runs. A probe failure propagates to the
        caller, which fails open (sync proceeds with the full field set)."""
        if self._capabilities is not None:
            return self._capabilities
        query = (
            "query { rateLimit { remaining resetAt } "
            'i: __type(name: "Issue") { fields(includeDeprecated: true) { name } } '
            'p: __type(name: "PullRequest") { fields(includeDeprecated: true) { name } } '
            'ic: __type(name: "IssueComment") { fields(includeDeprecated: true) { name } } '
            'rc: __type(name: "PullRequestReviewComment") '
            "{ fields(includeDeprecated: true) { name } } "
            't: __type(name: "IssueTimelineItemsItemType") '
            "{ enumValues { name } } }"
        )
        payload = self._graphql(query)
        caps: dict[str, set[str]] = {}
        for alias, type_name in (
            ("i", "Issue"),
            ("p", "PullRequest"),
            ("ic", "IssueComment"),
            ("rc", "PullRequestReviewComment"),
        ):
            node = payload.get(alias)
            names: set[str] = set()
            if isinstance(node, dict) and isinstance(node.get("fields"), list):
                names = {
                    str(f.get("name"))
                    for f in node["fields"]
                    if isinstance(f, dict) and f.get("name")
                }
            if names:
                # An empty introspection answer means "unknown", not "the type
                # has no fields" — omit it so the fail-open rule applies.
                caps[type_name] = names
        enum_node = payload.get("t")
        enum_names: set[str] = set()
        if isinstance(enum_node, dict) and isinstance(enum_node.get("enumValues"), list):
            enum_names = {
                str(v.get("name"))
                for v in enum_node["enumValues"]
                if isinstance(v, dict) and v.get("name")
            }
        if enum_names:
            # Stored ONLY when introspection answered (issue #27): a null or
            # absent value never reaches the persistence serializer (round-2
            # critic probe: sorted(None) aborts sync); an absent key on
            # re-attach means unknown, which fails open.
            caps["timeline_event_types"] = enum_names
        self._capabilities = caps
        return caps

    def attach_capabilities(self, caps: dict) -> None:
        """Activate a PERSISTED capability map (never introspects).

        Accepts the JSON round-trip shape {"Issue": [...], "PullRequest":
        [...], "IssueComment": [...], "PullRequestReviewComment": [...]};
        malformed or EMPTY entries are ignored — an empty field set
        means "unknown", never "the type has no fields", so a corrupt meta
        row degrades to no-capabilities (full field set) rather than
        stripping the query down to databaseId alone."""
        parsed: dict[str, set[str]] = {}
        for type_name in ("Issue", "PullRequest", "IssueComment", "PullRequestReviewComment", "timeline_event_types"):
            fields = caps.get(type_name) if isinstance(caps, dict) else None
            # The probe path passes sets, the meta-cache path passes lists
            # (JSON round-trip): accept any non-string collection, but every
            # entry must be a real field name — a corrupt row degrades to
            # "unknown" (full field set), never to a stripped query.
            if isinstance(fields, (list, set, tuple, frozenset)) and len(fields) > 0:
                names = {f for f in fields if isinstance(f, str) and f}
                if names:
                    parsed[type_name] = names
        self._capabilities = parsed or None

    def _timeline_item_types(self) -> str:
        """The itemTypes list the page/continuation queries request, degraded
        when the probe's ENUM introspection knows the host schema lacks
        REOPENED_EVENT (issue #27). Unknown (no probe, empty answer) fails
        open to the full list; the two baseline event types are never
        dropped."""
        event_types = (
            self._capabilities.get("timeline_event_types")
            if self._capabilities
            else None
        )
        if event_types is None:
            return _TIMELINE_TYPES_ALL
        kept = [t for t in _TIMELINE_EVENT_TYPES if t in event_types]
        if not kept:
            kept = ["CLOSED_EVENT", "CROSS_REFERENCED_EVENT"]
        return "[" + ", ".join(kept) + "]"

    def _timeline_block(self) -> str:
        return _timeline_nodes(self._timeline_item_types())

    def _fields_for(self, kind: str) -> str:
        """Field selection for one alias, honoring the active capability map.

        Without capabilities the full historical field set is rendered. With
        them, every block whose lead field the schema lacks is dropped and
        its name recorded in omitted_fields. Comment-scope fields
        (authorAssociation, issue #24) are gated per COMMENT type: a probe
        that knows the comment type and lacks the field strips the line from
        the comment blocks; an unknown comment type fails open."""
        if kind == "pr":
            blocks, lead = _PULL_TYPE_BLOCKS, _PULL_FIELDS
        else:
            blocks, lead = _ISSUE_TYPE_BLOCKS, _ISSUE_FIELDS
        caps = self._capabilities
        if caps is None:
            return lead
        present = caps.get("PullRequest" if kind == "pr" else "Issue")
        if present is None:
            # The probe reported nothing for this type: unknown, not absent —
            # fail open to the full selection (issue #23).
            return lead
        issue_comment_caps = caps.get("IssueComment")
        strip_issue = (
            issue_comment_caps is not None
            and "authorAssociation" not in issue_comment_caps
        )
        review_comment_caps = caps.get("PullRequestReviewComment")
        strip_review = (
            review_comment_caps is not None
            and "authorAssociation" not in review_comment_caps
        )
        parts = ["databaseId"]
        timeline_block = self._timeline_block()
        for field, block in blocks:
            if block is _TIMELINE_NODES:
                block = timeline_block
            if field in present:
                if strip_issue and block is _ISSUE_COMMENT_NODES:
                    block = block.replace("    authorAssociation\n", "")
                    # Record the sub-block drop like #23 records block drops
                    # (deduped): otherwise a host-side authorAssociation drop
                    # is the one capability drop status coverage can never
                    # see (issue #24 implementation review).
                    if "authorAssociation" not in self.omitted_fields:
                        self.omitted_fields.append("authorAssociation")
                elif strip_review and block is _REVIEW_NODES:
                    block = block.replace("        authorAssociation\n", "")
                    if "authorAssociation" not in self.omitted_fields:
                        self.omitted_fields.append("authorAssociation")
                parts.append(block)
            else:
                self.omitted_fields.append(field)
        return "\n".join(parts)

    # -- transport ----------------------------------------------------------

    def _graphql(self, query: str, aliases: dict[str, int] | None = None) -> dict:
        """One GraphQL request. The query travels on stdin as the JSON
        request body (`gh api graphql --input -`, issue #23): no argv element
        carries query text, so Windows' CreateProcessW command-line limit can
        never truncate a page query again.

        `aliases` maps this query's item aliases to their numbers; errors
        whose path scopes them to one alias's section
        (["repository", <alias>, <section>, ...]) are then DROPPED rather
        than raised - the section key is removed from that item's node so the
        sync's per-item REST fallback answers it, and the drop is recorded on
        last_fetch_drops for fetch_log. Validation errors (data null) and
        anything not scoped to a known alias stay hard."""
        self.last_dropped_sections: dict[int, list[tuple[str, str, str]]] = {}
        cmd = [self.gh_bin, "api", "graphql", "--input", "-"]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                input=json.dumps({"query": query}),
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
            error_list = errors if isinstance(errors, list) else []
            # A mixed payload (one alias NOT_FOUND next to a RATE_LIMITED)
            # must surface the rate-limit error so sync's sleep-retry sees it.
            first = next(
                (
                    e
                    for e in error_list
                    if isinstance(e, dict)
                    and (
                        "RATE_LIMIT" in str(e.get("type", "")).upper()
                        or "rate limit" in str(e.get("message", "")).lower()
                    )
                ),
                errors[0] if error_list else {},
            )
            message = str(first.get("message") or "graphql error") if isinstance(first, dict) else "graphql error"
            type_name = str(first.get("type", "")).upper() if isinstance(first, dict) else ""
            status = 429 if ("RATE_LIMIT" in type_name or "rate limit" in message.lower()) else None
            # Per-alias NOT_FOUND errors ride alongside partial data when an
            # item was deleted between the listing and this query; the nulled
            # aliases then degrade to the per-item REST path below instead of
            # aborting the whole run. A runtime per-field error scoped to one
            # alias's section (FORBIDDEN on a connection, issue #23) likewise
            # only drops that section: the errored key is removed from the
            # item's node so the established absent-section REST fallback
            # answers it. Validation errors (data null) and anything not
            # scoped to a known alias fail closed.
            hard = []
            dropped: dict[int, list[tuple[str, str, str]]] = {}
            for e in errors if isinstance(errors, list) else []:
                if not isinstance(e, dict):
                    continue
                etype = str(e.get("type", "")).upper()
                emsg = str(e.get("message", ""))
                if (
                    etype == "NOT_FOUND"
                    or "Could not resolve to an issue" in emsg
                    or "Could not resolve to a pullRequest" in emsg
                ):
                    continue
                if "RATE_LIMIT" in etype or "rate limit" in emsg.lower():
                    # A rate-limit error must always surface to the sleep/retry
                    # ladder, even when GitHub scopes it to one section with a
                    # path — dropping it would store partial data as a clean
                    # run and keep hammering a throttled API (PR #37 review).
                    hard.append(e)
                    continue
                path = e.get("path")
                if (
                    aliases is not None
                    and isinstance(path, list)
                    and len(path) >= 3
                    and path[0] == "repository"
                    and isinstance(path[1], str)
                    and path[1] in aliases
                    and isinstance(path[2], str)
                ):
                    number = aliases[path[1]]
                    dropped.setdefault(number, []).append(
                        (path[2], etype or "GRAPHQL_ERROR", emsg[:120])
                    )
                    continue
                hard.append(e)
            self.last_dropped_sections = dropped
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
        issue_comments / review_comments / reviews / files / pull, plus the
        item's normalized timeline events (#6). A comments/reviews/files
        connection that overflowed its first page adds a `<name>_incomplete`
        flag so the sync finishes it over REST for that item only;
        `timeline_incomplete` is different — GraphQL-only, never finished
        over REST — so fetch_children drains it itself (bounded; at the cap
        the retained set is the contiguous newest events and the flag stays
        set for sync to record in fetch_log). Items whose node did not
        resolve are omitted (the sync then uses the per-item REST path for
        them)."""
        children: dict[int, dict] = {}
        kinds: dict[int, str] = {}
        chunk: list[dict] = []
        self.last_fetch_drops = {}
        for raw in items:
            if isinstance(raw, dict) and "number" in raw:
                try:
                    kinds[int(raw["number"])] = "pr" if "pull_request" in raw else "issue"
                except (TypeError, ValueError):
                    pass  # malformed listing numbers are skipped per item in sync
            chunk.append(raw)
            if len(chunk) >= GRAPHQL_CHILDREN_PAGE or self._chunk_query_len(chunk) >= _MAX_QUERY_CHARS:
                children.update(self._fetch_children_chunk(chunk))
                chunk = []
        if chunk:
            children.update(self._fetch_children_chunk(chunk))
        for number in sorted(kinds):
            child = children.get(number)
            if child is not None and child.get("timeline_incomplete"):
                self._complete_timeline(number, kinds[number], child)
        return children

    def _timeline_page(self, number: int, kind: str, args: str) -> tuple[list[dict], dict]:
        """One timeline page for a single item: (events, pageInfo). `args`
        carries the caller's pagination clause (first/last/after/before) —
        every call site keeps its page size at or under _TIMELINE_PAGE_MAX,
        the GitHub per-request cap."""
        alias = ("p" if kind == "pr" else "i") + str(number)
        node_name = "pullRequest" if kind == "pr" else "issue"
        query = (
            "query { rateLimit { remaining resetAt } "
            f"repository(owner: {_q(self.owner)}, name: {_q(self.repo)}) {{ "
            f"{alias}: {node_name}(number: {number}) {{ {_timeline_selection(args, self._timeline_item_types())} }} }} }}"
        )
        # The alias map lets a section-scoped runtime error degrade the same
        # way the page query degrades: the timeline section drops and the
        # retained-set/truncation flag rules apply, instead of aborting the
        # whole sync (issue #23 review round 2).
        payload = self._graphql(query, aliases={alias: number})
        repo_payload = payload.get("repository")
        if not isinstance(repo_payload, dict):
            raise GitHubError("unexpected graphql repository payload")
        node = repo_payload.get(alias)
        if not isinstance(node, dict):
            return [], {}
        connection = node.get("timelineItems")
        if not isinstance(connection, dict):
            return [], {}
        events, _ = _connection(connection)
        return [_timeline_event(e) for e in events], connection.get("pageInfo") or {}

    def _complete_timeline(self, number: int, kind: str, child: dict) -> None:
        """Finish a flagged timeline and own the `timeline_incomplete` flag.

        The flag is CLEARED only when a continuation page explicitly reported
        `hasNextPage: false` AND the accumulated set is within
        _MAX_TIMELINE_EVENTS — a genuinely drained, genuinely small timeline
        is complete, not truncated. Every degenerate outcome keeps the flag
        set so sync records the truncation in fetch_log rather than silently
        presenting a partial timeline as complete:

        - the cap trips with pages still remaining (the retained set becomes
          the contiguous NEWEST _MAX_TIMELINE_EVENTS events via two chained
          `last:` pages walking back through the newest page's startCursor);
        - the accumulated set overshoots the cap because the last continuation
          page straddled it (same newest-window replacement);
        - a page is unanswerable (null endCursor with hasNextPage, a missing
          or null alias mid-drain) or the cursor stops advancing — the
          partial set is kept and the flag stays set.
        """
        events = list(child.get("timeline") or [])
        cursor = child.get("timeline_cursor")
        last_page_saw_more: bool | None = None
        pages = 0
        while (
            cursor
            and len(events) < _MAX_TIMELINE_EVENTS
            and pages < _MAX_CONTINUATION_PAGES
        ):
            page, info = self._timeline_page(
                number,
                kind,
                f"first: {_TIMELINE_CONTINUATION_PAGE} after: {_q(cursor)}",
            )
            pages += 1
            if not page and not info:
                # Unanswerable page (missing/null alias): we cannot know
                # whether more events exist, so keep the flag set.
                child["timeline"] = events
                child.pop("timeline_cursor", None)
                return
            events.extend(page)
            last_page_saw_more = bool(info.get("hasNextPage"))
            new_cursor = info.get("endCursor")
            if last_page_saw_more and new_cursor and new_cursor != cursor:
                cursor = new_cursor
            else:
                cursor = None
                if last_page_saw_more:
                    # hasNextPage true but the cursor is missing or did not
                    # advance: undrillable, keep the flag set.
                    child["timeline"] = events
                    child.pop("timeline_cursor", None)
                    return
        drained = last_page_saw_more is False and len(events) <= _MAX_TIMELINE_EVENTS
        if drained:
            child["timeline"] = events
            child.pop("timeline_incomplete", None)
        else:
            newest, info = self._timeline_page(number, kind, f"last: {_TIMELINE_PAGE_MAX}")
            if info.get("hasPreviousPage") and info.get("startCursor"):
                older, _ = self._timeline_page(
                    number,
                    kind,
                    f"last: {_TIMELINE_PAGE_MAX} before: {_q(info['startCursor'])}",
                )
                newest = older + newest
            child["timeline"] = newest
        child.pop("timeline_cursor", None)

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
        alias_numbers: dict[str, int] = {}
        for raw in chunk:
            if not isinstance(raw, dict) or "number" not in raw:
                continue
            number = int(raw["number"])
            alias = ("p" if "pull_request" in raw else "i") + str(number)
            fields = self._fields_for("pr" if "pull_request" in raw else "issue")
            aliases.append(f"{alias}: {'pullRequest' if 'pull_request' in raw else 'issue'}(number: {number}) {{{fields}}}")
            wanted.append((number, alias))
            alias_numbers[alias] = number
        if not aliases:
            return {}
        query = (
            "query { rateLimit { remaining resetAt } "
            f"repository(owner: {_q(self.owner)}, name: {_q(self.repo)}) {{ "
            + " ".join(aliases) + " } }"
        )
        payload = self._graphql(query, aliases=alias_numbers)
        repo = payload.get("repository")
        if not isinstance(repo, dict):
            raise GitHubError("unexpected graphql repository payload")
        for number, drops in self.last_dropped_sections.items():
            self.last_fetch_drops.setdefault(number, []).extend(drops)
        out: dict[int, dict] = {}
        for number, alias in wanted:
            node = repo.get(alias)
            if not isinstance(node, dict):
                continue
            for section, _, _ in self.last_dropped_sections.get(number, []):
                # The errored section must read as ABSENT (not empty) so the
                # sync's per-item REST fallback answers it (issue #23).
                node.pop(section, None)
            out[number] = self._children_from_node(node)
        return out

    def _children_from_node(self, node: dict) -> dict:
        children: dict = {}
        # A section the node does not carry (permission mask, partial error)
        # must stay ABSENT so the sync's per-item REST fallback runs for it;
        # writing [] would present a silently empty page as complete.
        if node.get("comments") is not None:
            comments, comments_more = _connection(node.get("comments"))
            children["issue_comments"] = [_comment_payload(c) for c in comments]
            if comments_more:
                children["issue_comments_incomplete"] = True

        if node.get("reviews") is not None:
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
                _comment_payload(
                    c, in_reply_to_id=(c.get("replyTo") or {}).get("databaseId")
                )
                for c in review_comments
            ]
            # Reviews beyond the first page never had their comments
            # requested, so their review comments are incomplete too.
            rc_more = reviews_more or any(more for _, more in review_payloads)
            if rc_more:
                children["review_comments_incomplete"] = True

        if node.get("files") is not None:
            files, files_more = _connection(node.get("files"))
            children["files"] = [
                {
                    "filename": f.get("path"),
                    "status": _CHANGE_TYPE_TO_STATUS.get(f.get("changeType"), "changed"),
                    "additions": _int_or_none(f.get("additions")),
                    "deletions": _int_or_none(f.get("deletions")),
                    "changes": (
                        None
                        if _int_or_none(f.get("additions")) is None
                        or _int_or_none(f.get("deletions")) is None
                        else _int_or_none(f.get("additions"))
                        + _int_or_none(f.get("deletions"))
                    ),
                    "sha": None,
                }
                for f in files
            ]
            if files_more:
                children["files_incomplete"] = True

        if node.get("baseRefName") is not None or node.get("mergedAt") is not None:
            # The pull section is gated on its OWN fields, not on `files`:
            # a node whose files connection is masked must not silently lose
            # mergedBy / closingIssuesReferences (issue #6 review round 2).
            # Presence alone is not enough: a permission-masked field comes
            # back null WITH the key present, and treating it as complete
            # would present an unanswered pull as real (issue #23).
            merge_commit = node.get("mergeCommit") or {}
            merged_by = node.get("mergedBy")
            merged_by_login = (
                merged_by.get("login") if isinstance(merged_by, dict) else None
            )
            closing_refs, closing_more = _connection(node.get("closingIssuesReferences"))
            pull_payload = {
                "additions": node.get("additions"),
                "deletions": node.get("deletions"),
                "changed_files": node.get("changedFiles"),
                "commits": (node.get("commits") or {}).get("totalCount"),
                "merged_at": node.get("mergedAt"),
                "merge_commit_sha": merge_commit.get("oid"),
                "base": {"ref": node.get("baseRefName")},
                "head": {"ref": node.get("headRefName")},
                "draft": bool(node.get("isDraft")),
                "merged_by": merged_by_login,
                "closing_issues_references": [
                    {
                        "number": _int_or_none(ref.get("number")),
                        "repo": (ref.get("repository") or {}).get("nameWithOwner"),
                    }
                    for ref in closing_refs
                ],
            }
            # Presence-preserving emission (issue #24): the pull-owned
            # ABSENT-merge columns must only see a key the node actually
            # carried. A capability-masked selection leaves the field out of
            # the node; emitting the REST key with null here would convert
            # "unknown" into explicit-null clearing of a stored value.
            for graphql_field, rest_key in (
                ("mergedAt", "merged_at"),
                ("mergeCommit", "merge_commit_sha"),
                ("mergedBy", "merged_by"),
            ):
                if graphql_field not in node:
                    pull_payload.pop(rest_key, None)
            children["pull"] = pull_payload
            if closing_more:
                children["closing_refs_incomplete"] = True

        # Commit messages (issue #27): independent of the pull gate — a
        # capability mask can drop baseRefName/mergedAt while commits answer.
        commits_conn = node.get("commits")
        if isinstance(commits_conn, dict) and "nodes" in commits_conn:
            commit_nodes, commits_more = _connection(commits_conn)
            children["pr_commits"] = [
                {
                    "sha": ((c.get("commit") or {}).get("oid")),
                    "message": _commit_message(c.get("commit") or {}),
                }
                for c in commit_nodes
            ]
            if commits_more:
                children["pr_commits_incomplete"] = True

        link_fields = (
            "blockedBy",
            "parent",
            "duplicateOf",
            "closedByPullRequestsReferences",
        )
        if any(field in node for field in link_fields):
            def _link_node(raw_node: object) -> dict | None:
                if not isinstance(raw_node, dict):
                    return None
                repo_ref = raw_node.get("repository")
                return {
                    "number": _int_or_none(raw_node.get("number")),
                    "repo": repo_ref.get("nameWithOwner")
                    if isinstance(repo_ref, dict)
                    else None,
                }

            links: dict = {}
            overflowed: list[str] = []
            if "blockedBy" in node:
                blocked_nodes, blocked_more = _connection(node.get("blockedBy"))
                links["blocked_by"] = [_link_node(n) for n in blocked_nodes]
                if blocked_more:
                    overflowed.append("blocked_by")
            if "parent" in node:
                links["parent"] = _link_node(node.get("parent"))
            if "duplicateOf" in node:
                links["duplicate_of"] = _link_node(node.get("duplicateOf"))
            if "closedByPullRequestsReferences" in node:
                open_nodes, open_more = _connection(
                    node.get("closedByPullRequestsReferences")
                )
                links["open_prs"] = [_link_node(n) for n in open_nodes]
                if open_more:
                    overflowed.append("open_prs")
            children["links"] = links
            if overflowed:
                children["links_incomplete"] = overflowed

        if node.get("timelineItems") is not None:
            events, timeline_more = _connection(node.get("timelineItems"))
            children["timeline"] = [_timeline_event(e) for e in events]
            # Retained for fetch_children's continuation; _resolve_children
            # never forwards it, and _complete_timeline pops it when it runs.
            page_info = (node.get("timelineItems") or {}).get("pageInfo") or {}
            children["timeline_cursor"] = page_info.get("endCursor")
            if timeline_more:
                children["timeline_incomplete"] = True
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
