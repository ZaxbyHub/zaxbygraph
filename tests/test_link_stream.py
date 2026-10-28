"""Acceptance checks for issue-trace 26-27-text-refs-and-link-stream
(issue #27, schema v10): GitHub-reported link connections, PR commits and
reopen events become graph/store facts through the `link` stream.

Authored at arm's length against the trace's acceptance table. NEW-SURFACE
module (C6-C12): it is deliberately absent at base d33cfbc so the module's
absence is the frozen base signature for every check here.

Seam: every check drives the production sync path -- sync_repo with a bulk
source built on the fixtures_bulk PageShapedBulkSource pattern whose
fetch_children returns children dicts carrying the v10 keys the binding
contract freezes: `links` (GitHub-reported connections, normalized to the
house {"number", "repo"} node shape the closing_issues_references and
cross_referenced mappings already use), `pr_commits` (list of {sha, message}
dicts) and reopen events inside the existing `timeline` list. Degraded
re-ingests model the REST-source / 404 / capability-dropped paths by OMITTING
the section from the children dict (unfetched), and the overflow path by
flagging `links_incomplete`.

Hermetic: FakeGitHubSource-derived fakes only; no network, no gh.
"""

from __future__ import annotations

import sqlite3
import unittest
from copy import deepcopy

from fixtures import (
    REPO,
    FakeGitHubSource,
    TempDBTest,
    comment,
    issue,
    pull,
    ts,
)
from zaxbygraph.sync import sync_repo


class _BulkLinkSource(FakeGitHubSource):
    """The fixtures_bulk PageShapedBulkSource pattern plus the v10 children
    sections: plain item pages from list_issues, children dicts (timeline,
    links, pr_commits) from fetch_children. A section is COMPLETE whenever
    the children dict carries it unflagged; omitting it models the unfetched
    (REST / 404 / capability-dropped) path and `links_incomplete` models the
    overflowed connection."""

    def __init__(self) -> None:
        super().__init__()
        self.links: dict[int, dict] = {}
        self.links_incomplete: set[int] = set()
        self.pr_commits: dict[int, list[dict]] = {}
        self.timelines: dict[int, list[dict]] = {}

    def list_issues(self, since: str | None):
        page = [
            deepcopy(rec)
            for rec in sorted(
                self.issues.values(), key=lambda r: (r["updated_at"], r["number"])
            )
            if since is None or rec["updated_at"] >= since
        ]
        if page:
            yield page

    def fetch_children(self, items) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for raw in items:
            n = int(raw["number"])
            children: dict = {
                "issue_comments": deepcopy(self.issue_comments.get(n, [])),
                "review_comments": deepcopy(self.review_comments.get(n, [])),
                "reviews": deepcopy(self.reviews.get(n, [])),
                "files": deepcopy(self.files.get(n, [])),
            }
            if n in self.pulls:
                children["pull"] = deepcopy(self.pulls[n])
            if n in self.timelines:
                children["timeline"] = deepcopy(self.timelines[n])
            if n in self.links:
                children["links"] = deepcopy(self.links[n])
                if n in self.links_incomplete:
                    children["links_incomplete"] = True
            if n in self.pr_commits:
                children["pr_commits"] = deepcopy(self.pr_commits[n])
            out[n] = children
        return out


def _node(number: int, repo: str = REPO) -> dict:
    """One connection endpoint in the house node shape."""
    return {"number": number, "repo": repo}


def _links(
    *,
    blocked_by: tuple[dict, ...] = (),
    parent: dict | None = None,
    duplicate_of: dict | None = None,
    open_prs: tuple[dict, ...] = (),
) -> dict:
    """A complete `links` children section: every connection is reported, an
    absent connection as an empty list (the blockedBy / open-PR connections)
    or None (the SINGULAR parent / duplicateOf nodes -- the live GraphQL
    schema types them Issue node-or-null, not connections)."""
    return {
        "blocked_by": [dict(n) for n in blocked_by],
        "parent": dict(parent) if parent else None,
        "duplicate_of": dict(duplicate_of) if duplicate_of else None,
        "open_prs": [dict(n) for n in open_prs],
    }


def _bump(src: _BulkLinkSource, number: int, offset_s: int = 200_000) -> None:
    """Move an item past the stored watermark so the next sync re-ingests it
    (the incremental listing filters on updated_at)."""
    src.issues[number]["updated_at"] = ts(offset_s + number)


class LinkStreamCheckBase(TempDBTest):
    """Row fetcher that turns a missing v10 surface into the check's frozen
    failure line instead of a bare traceback."""

    def rows(
        self, sql: str, params: tuple = (), fail_id: str = "LINK-STREAM"
    ) -> list[tuple]:
        try:
            return [tuple(r) for r in self.conn.execute(sql, params)]
        except sqlite3.OperationalError as exc:
            self.fail(f"{fail_id}: v10 surface missing at ingest ({exc})")
            raise  # unreachable: self.fail raises


class BlockedByLinkTests(LinkStreamCheckBase):
    def test_blocked_by_replaced_when_complete(self) -> None:
        """C6 / 27-AC1: a complete blockedBy connection stores owner->blocker
        `blocked_by` edges with source='link', and a later COMPLETE re-ingest
        exactly matches the report -- a blocker removed upstream is
        retracted. NEW-SURFACE: absent at base d33cfbc; GREEN once v10
        lands."""
        src = _BulkLinkSource()
        src.add_issue(issue(10, title="blocked work", body=""))
        src.links[10] = _links(blocked_by=(_node(11), _node(12)))
        sync_repo(self.conn, src, REPO)

        query = (
            "SELECT dst_id, source FROM edges WHERE repo = ?"
            " AND src_type = 'item' AND src_id = '10' AND rel = 'blocked_by'"
            " ORDER BY dst_id"
        )
        rows = self.rows(query, (REPO,), "BLOCKED-BY-EDGE-MISSING")
        if rows != [("11", "link"), ("12", "link")]:
            self.fail(
                "BLOCKED-BY-EDGE-MISSING: a complete blockedBy connection "
                "must store owner->blocker edges with source='link', got %r"
                % (rows,)
            )
        # Complete re-ingest without blocker 12: the section replaces the
        # stored set with exactly what GitHub now reports.
        _bump(src, 10)
        src.links[10] = _links(blocked_by=(_node(11),))
        sync_repo(self.conn, src, REPO)
        rows = self.rows(query, (REPO,), "BLOCKED-BY-NOT-RETRACTED")
        if rows != [("11", "link")]:
            self.fail(
                "BLOCKED-BY-NOT-RETRACTED: a complete blockedBy re-ingest "
                "must retract removed blockers and keep the reported one, "
                "got %r" % (rows,)
            )


class DegradedLinkTests(LinkStreamCheckBase):
    def test_link_edges_survive_degraded_reingest(self) -> None:
        """C7 / 27-AC2: an unfetched links section (REST source, 404 path,
        capability-dropped) never retracts an existing link edge, and an
        overflowed connection flagged incomplete is UNION-ONLY -- the stored
        edge survives while the newly reported blocker is added.
        NEW-SURFACE: absent at base d33cfbc; GREEN once v10 lands."""
        src = _BulkLinkSource()
        src.add_issue(issue(20, title="blocked work", body=""))
        src.links[20] = _links(blocked_by=(_node(5),))
        sync_repo(self.conn, src, REPO)

        query = (
            "SELECT dst_id, source FROM edges WHERE repo = ?"
            " AND src_type = 'item' AND src_id = '20' AND rel = 'blocked_by'"
            " ORDER BY dst_id"
        )
        rows = self.rows(query, (REPO,), "LINK-EDGE-RETRACTED")
        if rows != [("5", "link")]:
            self.fail(
                "LINK-EDGE-RETRACTED: baseline sync must record the blocker "
                "first, got %r" % (rows,)
            )
        # (a) Unfetched section: the children dict carries no links key.
        _bump(src, 20)
        del src.links[20]
        sync_repo(self.conn, src, REPO)
        rows = self.rows(query, (REPO,), "LINK-EDGE-RETRACTED")
        if rows != [("5", "link")]:
            self.fail(
                "LINK-EDGE-RETRACTED: an unfetched links section must not "
                "retract the stored blocked_by edge, got %r" % (rows,)
            )
        # (b) Overflowed connection: union-only. The truncated payload adds
        # blocker 77 WITHOUT carrying the stored blocker 5, so the store must
        # keep 5 and add 77 -- a delete-then-replace implementation now
        # fails.
        _bump(src, 20, 300_000)
        src.links[20] = _links(blocked_by=(_node(77),))
        src.links_incomplete.add(20)
        sync_repo(self.conn, src, REPO)
        rows = self.rows(query, (REPO,), "LINK-EDGE-RETRACTED")
        if rows != [("5", "link"), ("77", "link")]:
            self.fail(
                "LINK-EDGE-RETRACTED: an overflowed (links_incomplete) "
                "connection is union-only: the stored edge to 5 must survive "
                "and the newly reported blocker 77 must be added, got %r"
                % (rows,)
            )


class StructuralLinkTests(LinkStreamCheckBase):
    def test_parent_and_duplicate_edges(self) -> None:
        """C8 / 27-AC3: fetched `parent` / `duplicateOf` connections store
        owner-side `child_of` / `duplicate_of` link edges (source='link').
        Both fields are SINGULAR nodes in the live GraphQL schema, so the
        links section models them node-or-none and each owner yields exactly
        one such edge; same-repo endpoints are bare item ids and a cross-repo
        endpoint is repo-qualified so it can never attach to a same-numbered
        local item. A null (explicitly cleared) duplicateOf belongs to the
        survival family C7 pins, not to this check. NEW-SURFACE: absent at
        base d33cfbc; GREEN once v10 lands."""
        src = _BulkLinkSource()
        src.add_issue(issue(40, title="the child", body=""))
        src.links[40] = _links(parent=_node(41), duplicate_of=_node(42))
        # A second owner whose singular duplicateOf target lives in another
        # repo: the edge destination must be the repo-qualified form.
        src.add_issue(issue(43, title="cross-repo duplicate", body=""))
        src.links[43] = _links(duplicate_of=_node(50, "other/repo"))
        sync_repo(self.conn, src, REPO)

        child_rows = self.rows(
            "SELECT dst_id, source FROM edges WHERE repo = ?"
            " AND src_type = 'item' AND src_id = '40' AND rel = 'child_of'",
            (REPO,),
            "LINK-EDGE-MISSING",
        )
        if child_rows != [("41", "link")]:
            self.fail(
                "LINK-EDGE-MISSING: a fetched parent must store an "
                "owner-side child_of edge with source='link', got %r"
                % (child_rows,)
            )
        dup_rows = self.rows(
            "SELECT dst_id, source FROM edges WHERE repo = ?"
            " AND src_type = 'item' AND src_id = '40' AND rel = 'duplicate_of'",
            (REPO,),
            "LINK-EDGE-MISSING",
        )
        if dup_rows != [("42", "link")]:
            self.fail(
                "LINK-EDGE-MISSING: a singular same-repo duplicateOf node "
                "must store exactly one owner-side duplicate_of edge with "
                "source='link', got %r" % (dup_rows,)
            )
        xrepo_rows = self.rows(
            "SELECT dst_id, source FROM edges WHERE repo = ?"
            " AND src_type = 'item' AND src_id = '43' AND rel = 'duplicate_of'",
            (REPO,),
            "LINK-EDGE-MISSING",
        )
        if xrepo_rows != [("other/repo#50", "link")]:
            self.fail(
                "LINK-EDGE-MISSING: a singular cross-repo duplicateOf node "
                "must store one duplicate_of edge with the repo-qualified "
                "destination 'other/repo#50', got %r" % (xrepo_rows,)
            )


class OpenLinkedPrTests(LinkStreamCheckBase):
    def test_open_linked_prs_recorded(self) -> None:
        """C9 / 27-AC4: the open linked PRs a closedByPullRequestsReferences(
        includeClosedPrs: false) report carries are recorded from the issue's
        side as exactly one `linked_pr` edge (the relation name the plan
        pins) to the open PR, source='link', with no other link rows for the
        owner -- a renamed relation fails loudly. NEW-SURFACE: absent at base
        d33cfbc; GREEN once v10 lands."""
        src = _BulkLinkSource()
        src.add_issue(issue(45, title="tracked issue", body=""))
        src.links[45] = _links(open_prs=(_node(46),))
        # A stored closed PR that the report does not name: the includeClosed
        # filter is upstream, so this stream records only what it reported.
        src.add_pr(
            issue(47, title="closed pr", kind="pr", state="closed"),
            pull(47, merged=True),
            files=[],
        )
        sync_repo(self.conn, src, REPO)

        # Every source='link' row the owner owns, so extra or renamed
        # relations fail the exact-shape assertion, not just missing ones.
        rows = self.rows(
            "SELECT rel, dst_id FROM edges WHERE repo = ?"
            " AND src_type = 'item' AND src_id = '45' AND dst_type = 'item'"
            " AND source = 'link' ORDER BY rel, dst_id",
            (REPO,),
            "OPEN-LINKED-PR-MISSING",
        )
        if rows != [("linked_pr", "46")]:
            self.fail(
                "OPEN-LINKED-PR-MISSING: the issue's reported open linked PR "
                "must be recorded as exactly one owner-side rel='linked_pr' "
                "edge (source='link') to PR 46, and no other link rows may "
                "exist for the owner, got %r" % (rows,)
            )


class CommitKeywordTests(LinkStreamCheckBase):
    def test_commit_message_keywords(self) -> None:
        """C10 / 27-AC5: a PR commit message 'fixes #N' yields a
        `closes_keyword` edge whose evidence carries the commit sha, and
        NEVER the `closes` relation; the commit itself is recorded in
        pr_commits. NEW-SURFACE: absent at base d33cfbc; GREEN once v10
        lands."""
        src = _BulkLinkSource()
        src.add_issue(issue(61, title="target", body=""))
        src.add_pr(issue(60, title="fix pr", kind="pr", body=""), pull(60), files=[])
        src.pr_commits[60] = [{"sha": "cafe1234feedface", "message": "fixes #61"}]
        sync_repo(self.conn, src, REPO)

        kw = self.rows(
            "SELECT evidence, source FROM edges WHERE repo = ?"
            " AND src_type = 'item' AND src_id = '60'"
            " AND rel = 'closes_keyword' AND dst_type = 'item' AND dst_id = '61'",
            (REPO,),
            "COMMIT-KEYWORD-EDGE-MISSING",
        )
        self.assertEqual(
            len(kw),
            1,
            "COMMIT-KEYWORD-EDGE-MISSING: a PR commit message 'fixes #N' "
            "must yield exactly one closes_keyword edge, got %r" % (kw,),
        )
        evidence, source = kw[0]
        self.assertEqual(
            source,
            "keyword",
            "COMMIT-KEYWORD-EDGE-MISSING: the commit-message closure edge "
            "must be source='keyword', got %r" % (source,),
        )
        self.assertIn(
            "cafe1234feedface",
            evidence or "",
            "COMMIT-KEYWORD-EDGE-MISSING: closes_keyword evidence must carry "
            "the commit sha, got %r" % (evidence,),
        )
        closes = self.rows(
            "SELECT source FROM edges WHERE repo = ?"
            " AND src_type = 'item' AND src_id = '60' AND rel = 'closes'"
            " AND dst_type = 'item' AND dst_id = '61'",
            (REPO,),
            "COMMIT-CLOSES-FORBIDDEN",
        )
        if closes != []:
            self.fail(
                "COMMIT-CLOSES-FORBIDDEN: a commit-message keyword closure "
                "must never write the closes relation (reserved for timeline "
                "and closing-reference streams), got %r" % (closes,)
            )
        prow = self.rows(
            "SELECT sha, message FROM pr_commits WHERE repo = ? AND number = 60",
            (REPO,),
            "PR-COMMIT-ROW-MISSING",
        )
        self.assertIn(
            ("cafe1234feedface", "fixes #61"),
            prow,
            "PR-COMMIT-ROW-MISSING: the PR commit must be recorded with its "
            "sha and message, got %r" % (prow,),
        )


class DuplicateKeywordTests(LinkStreamCheckBase):
    def test_duplicate_keyword_stream(self) -> None:
        """C11 / 27-AC6: body and comment text 'duplicate of #N' yields a
        `duplicate_keyword` edge with source='keyword', and `duplicate_of`
        never carries source='keyword' (it is reserved for GitHub-reported
        link facts). NEW-SURFACE: absent at base d33cfbc; GREEN once v10
        lands."""
        src = _BulkLinkSource()
        src.add_issue(issue(70, title="the duplicate", body="duplicate of #71"))
        src.add_issue(issue(71, title="the original", body=""))
        src.add_issue(issue(72, title="another duplicate", body=""))
        src.issue_comments[72].append(comment(7101, "duplicate of #71"))
        src.issues[72]["comments"] = 1
        sync_repo(self.conn, src, REPO)

        body_rows = self.rows(
            "SELECT source FROM edges WHERE repo = ?"
            " AND src_type = 'item' AND src_id = '70'"
            " AND rel = 'duplicate_keyword' AND dst_type = 'item'"
            " AND dst_id = '71'",
            (REPO,),
            "DUPLICATE-KEYWORD-MISSING",
        )
        if body_rows != [("keyword",)]:
            self.fail(
                "DUPLICATE-KEYWORD-MISSING: body text 'duplicate of #N' "
                "must yield a duplicate_keyword edge with source='keyword', "
                "got %r" % (body_rows,)
            )
        comment_rows = self.rows(
            "SELECT source FROM edges WHERE repo = ?"
            " AND src_type = 'item' AND src_id = '72'"
            " AND rel = 'duplicate_keyword' AND dst_type = 'item'"
            " AND dst_id = '71'",
            (REPO,),
            "DUPLICATE-KEYWORD-COMMENT-MISSING",
        )
        if comment_rows != [("keyword",)]:
            self.fail(
                "DUPLICATE-KEYWORD-COMMENT-MISSING: comment text 'duplicate "
                "of #N' must yield a duplicate_keyword edge with "
                "source='keyword', got %r" % (comment_rows,)
            )
        leak = self.rows(
            "SELECT src_id FROM edges WHERE repo = ?"
            " AND rel = 'duplicate_of' AND source = 'keyword'",
            (REPO,),
            "DUPLICATE-OF-KEYWORD-FORBIDDEN",
        )
        if leak != []:
            self.fail(
                "DUPLICATE-OF-KEYWORD-FORBIDDEN: duplicate_of is reserved "
                "for GitHub-reported link facts and must never carry "
                "source='keyword', got %r" % (leak,)
            )


class ReopenEventTests(LinkStreamCheckBase):
    def test_reopen_events_recorded(self) -> None:
        """C12 / 27-AC7: REOPENED timeline events are recorded in item_events
        with their type and timestamp, kept through a degraded (unfetched
        timeline) re-ingest, and replaced only by a later complete timeline.
        NEW-SURFACE: absent at base d33cfbc; GREEN once v10 lands."""
        src = _BulkLinkSource()
        src.add_issue(issue(80, title="flappy", body=""))
        src.timelines[80] = [
            {"type": "reopened", "created_at": "2026-02-01T00:00:00Z",
             "actor_login": "zoe"},
            {"type": "reopened", "created_at": "2026-02-02T00:00:00Z",
             "actor_login": "zoe"},
        ]
        sync_repo(self.conn, src, REPO)

        query = (
            "SELECT type, created_at FROM item_events WHERE repo = ?"
            " AND number = 80 ORDER BY created_at"
        )
        rows = self.rows(query, (REPO,), "REOPEN-EVENT-MISSING")
        if rows != [
            ("reopened", "2026-02-01T00:00:00Z"),
            ("reopened", "2026-02-02T00:00:00Z"),
        ]:
            self.fail(
                "REOPEN-EVENT-MISSING: reopen timeline events must be "
                "recorded in item_events with their type and timestamp, got "
                "%r" % (rows,)
            )
        # Degraded re-ingest: the timeline section is unfetched (absent from
        # the children dict), so the stored events are kept.
        _bump(src, 80)
        del src.timelines[80]
        sync_repo(self.conn, src, REPO)
        rows = self.rows(query, (REPO,), "REOPEN-EVENTS-DROPPED")
        if len(rows) != 2:
            self.fail(
                "REOPEN-EVENTS-DROPPED: a degraded (unfetched timeline) "
                "re-ingest must keep the stored reopen events, got %r"
                % (rows,)
            )
        # Complete re-ingest: the section replaces the stored events.
        _bump(src, 80, 300_000)
        src.timelines[80] = [
            {"type": "reopened", "created_at": "2026-02-03T00:00:00Z",
             "actor_login": "zoe"},
        ]
        sync_repo(self.conn, src, REPO)
        rows = self.rows(query, (REPO,), "REOPEN-EVENTS-NOT-REPLACED")
        if rows != [("reopened", "2026-02-03T00:00:00Z")]:
            self.fail(
                "REOPEN-EVENTS-NOT-REPLACED: a complete timeline re-ingest "
                "replaces the item's reopen events, got %r" % (rows,)
            )


if __name__ == "__main__":
    unittest.main()
