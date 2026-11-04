"""Issue #53: a malformed link node must never silently retract a
GitHub-reported link fact.

Two layers, one truth table:
- the STORE layer (`rebuild_links`, replay/hand-written bundles): a carried
  connection containing a node that fails `_link_dst` (null number, non-dict,
  self-reference), a carried plural null, or a singular list value must be
  union-only — the stored edge survives. An explicitly cleared singular
  value (`parent: null`) still retracts.
- the PRODUCTION GraphQL layer (`_children_from_node`): malformed
  connections/nodes must propagate union-only degradation
  (`links_incomplete`) instead of being normalized into complete-empty or
  cleared values — and never crash.

Base behavior pinned here (RED at f28e4b4): the store deletes a
carried-complete connection's edges and re-emits only parseable nodes, and
the GraphQL normalizer converts malformed input into `([], False)` /
`None`, so the production path retracts exactly like the store path.
"""
from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path

# Self-pin the repo src like this PR's other new modules: a non-editable
# `pip install` drops a real package directory into site-packages that
# SHADOWS the editable .pth, and discovery would then silently test the
# stale installed build.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fixtures import (
    REPO,
    FakeGitHubSource,
    TempDBTest,
    issue,
    pr_file,
    pull,
    review,
    ts,
)  # noqa: E402
from zaxbygraph.graphql import GraphQLSource  # noqa: E402
from zaxbygraph.sync import sync_repo  # noqa: E402


class _BulkLinkSource(FakeGitHubSource):
    """The tests/test_link_stream.py pattern: children dicts carry the v10
    `links` section; omitting it models the unfetched path and
    `links_incomplete` models the overflowed connection."""

    def __init__(self) -> None:
        super().__init__()
        self.links: dict[int, dict] = {}
        self.links_incomplete: set[int] = set()

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
            if n in self.links:
                children["links"] = deepcopy(self.links[n])
                if n in self.links_incomplete:
                    children["links_incomplete"] = True
            out[n] = children
        return out


def _links(**overrides) -> dict:
    base = {
        "blocked_by": [],
        "parent": None,
        "duplicate_of": None,
        "open_prs": [],
    }
    base.update(overrides)
    return base


def _edges(conn, number: int) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT rel, dst_id, source FROM edges WHERE repo = ?"
            " AND src_type = 'item' AND src_id = ? AND source = 'link'",
            (REPO, str(number)),
        )
    ]


class StoreLayerMalformedTests(TempDBTest):
    def _seed_blocked(self, src: _BulkLinkSource, number: int = 10) -> None:
        src.add_issue(issue(number, title="blocked work"))
        src.add_issue(issue(11, title="blocker one"))
        src.add_issue(issue(12, title="blocker two"))

    def _sync(self, src: _BulkLinkSource) -> None:
        sync_repo(self.conn, src, REPO)

    def _bump(self, src: _BulkLinkSource, number: int) -> None:
        src.issues[number]["updated_at"] = ts(200_000 + number)

    def test_malformed_node_does_not_retract(self) -> None:
        src = _BulkLinkSource()
        self._seed_blocked(src)
        src.links[10] = _links(
            blocked_by=[{"number": 11, "repo": REPO}, {"number": 12, "repo": REPO}]
        )
        self._sync(src)
        self.assertEqual(len(_edges(self.conn, 10)), 2, _edges(self.conn, 10))
        self._bump(src, 10)
        src.links[10] = _links(
            blocked_by=[{"number": None, "repo": REPO}, {"number": 12, "repo": REPO}]
        )
        self._sync(src)
        edges = _edges(self.conn, 10)
        self.assertEqual(
            sorted(edges),
            [("blocked_by", "11", "link"), ("blocked_by", "12", "link")],
            "a malformed node must not retract the stored edge",
        )

    def test_self_reference_does_not_retract(self) -> None:
        src = _BulkLinkSource()
        self._seed_blocked(src)
        src.links[10] = _links(blocked_by=[{"number": 11, "repo": REPO}])
        self._sync(src)
        self.assertEqual(len(_edges(self.conn, 10)), 1)
        self._bump(src, 10)
        src.links[10] = _links(blocked_by=[{"number": 10, "repo": REPO}])
        self._sync(src)
        self.assertEqual(
            _edges(self.conn, 10),
            [("blocked_by", "11", "link")],
            "a self-referencing node must not retract the stored edge",
        )

    def test_carried_plural_null_does_not_retract(self) -> None:
        src = _BulkLinkSource()
        self._seed_blocked(src)
        src.links[10] = _links(blocked_by=[{"number": 11, "repo": REPO}])
        self._sync(src)
        self.assertEqual(len(_edges(self.conn, 10)), 1)
        self._bump(src, 10)
        src.links[10] = _links(blocked_by=None)
        self._sync(src)
        self.assertEqual(
            _edges(self.conn, 10),
            [("blocked_by", "11", "link")],
            "a carried plural null must not retract the stored edge",
        )

    def test_singular_list_value_does_not_retract(self) -> None:
        src = _BulkLinkSource()
        src.add_issue(issue(10, title="child work"))
        src.add_issue(issue(11, title="the parent"))
        src.links[10] = _links(parent={"number": 11, "repo": REPO})
        self._sync(src)
        self.assertEqual(
            _edges(self.conn, 10), [("child_of", "11", "link")], _edges(self.conn, 10)
        )
        self._bump(src, 10)
        src.links[10] = _links(parent=[])
        self._sync(src)
        self.assertEqual(
            _edges(self.conn, 10),
            [("child_of", "11", "link")],
            "a singular list value is malformed, not a cleared value",
        )

    def test_cleared_singular_value_still_retracts(self) -> None:
        """PRESERVING floor: an explicit null singular node is a cleared
        value — GitHub reported the link is gone, so the stored edge must
        be retracted on a complete re-fetch."""
        src = _BulkLinkSource()
        src.add_issue(issue(10, title="child work"))
        src.add_issue(issue(11, title="the parent"))
        src.links[10] = _links(parent={"number": 11, "repo": REPO})
        self._sync(src)
        self.assertEqual(len(_edges(self.conn, 10)), 1)
        self._bump(src, 10)
        src.links[10] = _links(parent=None)
        self._sync(src)
        self.assertEqual(_edges(self.conn, 10), [])


class GraphQLSeamMalformedTests(TempDBTest):
    """The PUBLIC seam: `GraphQLSource.fetch_children` -> `sync_repo`.

    Every GraphQL-layer test above calls `_children_from_node` directly, so
    a regression in the chunk/alias plumbing (`_fetch_children_chunk`
    dropping the node, `fetch_children` swallowing the flag) would pass
    them all while the production sync still retracted the link. These legs
    drive the real entry point with a stubbed transport (issue #53, review
    PRR-011)."""

    def _seam_source(self, node_links: dict) -> FakeGitHubSource:
        """A fake REST listing whose children come from a real
        GraphQLSource.fetch_children answering with `node_links`."""

        class _StubbedTransport(GraphQLSource):
            def __init__(self) -> None:
                super().__init__("probe", "clinic")
                # The real `_graphql` initializes these; the stub replaces it.
                self.last_dropped_sections: dict[int, list] = {}

            def _graphql(self, query: str, aliases: dict | None = None) -> dict:
                node = {"number": 10, "id": "I_10", "title": "blocked work"}
                node.update(node_links)
                # `_graphql` returns the UNWRAPPED `data` payload.
                return {"repository": {"i10": node}}

        class _SeamSource(FakeGitHubSource):
            def __init__(self) -> None:
                super().__init__()
                self._gql = _StubbedTransport()

            def fetch_children(self, items) -> dict[int, dict]:
                return self._gql.fetch_children(items)

        return _SeamSource()

    def _seed(self) -> FakeGitHubSource:
        src = _BulkLinkSource()
        src.add_issue(issue(10, title="blocked work"))
        src.add_issue(issue(11, title="blocker one"))
        src.add_issue(issue(12, title="blocker two"))
        src.links[10] = _links(
            blocked_by=[{"number": 11, "repo": REPO}, {"number": 12, "repo": REPO}]
        )
        return src

    def test_malformed_node_survives_the_public_seam(self) -> None:
        src = self._seed()
        sync_repo(self.conn, src, REPO)
        self.assertEqual(len(_edges(self.conn, 10)), 2, _edges(self.conn, 10))
        src.issues[10]["updated_at"] = ts(200_010)
        seam = self._seam_source(
            {
                "blockedBy": {
                    "nodes": [
                        {"number": True, "repository": None},
                        {"number": 12, "repository": {"nameWithOwner": REPO}},
                    ]
                }
            }
        )
        seam.issues[10] = src.issues[10]
        sync_repo(self.conn, seam, REPO)
        self.assertEqual(
            sorted(_edges(self.conn, 10)),
            [("blocked_by", "11", "link"), ("blocked_by", "12", "link")],
            "a boolean number through the public seam must not retract the edge",
        )

    def test_wellformed_node_still_retracts_through_the_seam(self) -> None:
        """PRESERVING floor: the seam path is not blanket union-only — a
        well-formed connection that drops a blocker still retracts it."""
        src = self._seed()
        sync_repo(self.conn, src, REPO)
        self.assertEqual(len(_edges(self.conn, 10)), 2)
        src.issues[10]["updated_at"] = ts(200_010)
        seam = self._seam_source(
            {"blockedBy": {"nodes": [{"number": 11, "repository": {"nameWithOwner": REPO}}]}}
        )
        seam.issues[10] = src.issues[10]
        sync_repo(self.conn, seam, REPO)
        self.assertEqual(
            _edges(self.conn, 10),
            [("blocked_by", "11", "link")],
            "a well-formed connection through the seam must still retract",
        )


def _source() -> GraphQLSource:
    return GraphQLSource("probe", "clinic")


def _children(links_node: dict) -> dict:
    node = {"number": 1, "id": "I_1"}
    node.update(links_node)
    return _source()._children_from_node(node)


class GraphQLLayerMalformedTests(unittest.TestCase):
    def _assert_incomplete(self, links_node: dict, connection: str) -> None:
        children = _children(links_node)
        incomplete = children.get("links_incomplete")
        self.assertIsNotNone(
            incomplete, f"{connection} malformed shape was not flagged: {children}"
        )
        self.assertIn(connection, incomplete, children)

    def test_plural_null_marks_incomplete(self) -> None:
        self._assert_incomplete({"blockedBy": None}, "blocked_by")

    def test_plural_scalar_marks_incomplete(self) -> None:
        self._assert_incomplete({"blockedBy": "malformed"}, "blocked_by")

    def test_non_list_nodes_marks_incomplete_without_crash(self) -> None:
        self._assert_incomplete({"blockedBy": {"nodes": 7}}, "blocked_by")

    def test_dict_nodes_marks_incomplete(self) -> None:
        self._assert_incomplete(
            {"blockedBy": {"nodes": {"number": 7}}}, "blocked_by"
        )

    def test_null_number_node_marks_incomplete(self) -> None:
        self._assert_incomplete(
            {"blockedBy": {"nodes": [{"number": 7}, {"number": None}]}},
            "blocked_by",
        )

    def test_non_dict_node_marks_incomplete(self) -> None:
        self._assert_incomplete(
            {"blockedBy": {"nodes": [{"number": 7}, "garbage"]}}, "blocked_by"
        )

    def test_singular_malformed_marks_incomplete(self) -> None:
        self._assert_incomplete({"parent": "garbage"}, "parent")

    def test_singular_null_number_marks_incomplete(self) -> None:
        self._assert_incomplete({"parent": {"number": None}}, "parent")

    def test_non_dict_pageinfo_marks_incomplete(self) -> None:
        self._assert_incomplete(
            {"blockedBy": {"nodes": [], "pageInfo": 7}}, "blocked_by"
        )

    def test_wellformed_plural_stays_complete(self) -> None:
        """PRESERVING floor: a well-formed connection is not flagged."""
        children = _children(
            {"blockedBy": {"nodes": [{"number": 7, "repository": None}]}}
        )
        self.assertNotIn("links_incomplete", children, children)
        self.assertEqual(
            children["links"]["blocked_by"], [{"number": 7, "repo": None}]
        )

    def test_singular_null_is_cleared_not_malformed(self) -> None:
        """PRESERVING floor: an explicit null singular node is cleared."""
        children = _children({"parent": None})
        self.assertNotIn("links_incomplete", children, children)
        self.assertIsNone(children["links"]["parent"])

    def test_timeline_non_dict_pageinfo_does_not_crash(self) -> None:
        """The initial timeline consumer must not crash on a malformed
        pageInfo (it re-dereferences the raw payload for endCursor)."""
        for bad in (7, "bad", ["bad"]):
            with self.subTest(pageInfo=bad):
                children = _source()._children_from_node(
                    {"number": 1, "timelineItems": {"nodes": [], "pageInfo": bad}}
                )
                self.assertIn("timeline", children)

    def test_timeline_non_dict_container_marks_incomplete(self) -> None:
        """A CARRIED malformed timelineItems container (a scalar or a list
        where the connection object belongs) must degrade to union-only —
        presenting a silently empty timeline as complete would delete the
        stored item_events (implementation-review finding 2)."""
        for bad in (7, "garbage", ["nodes"]):
            with self.subTest(timelineItems=bad):
                children = _source()._children_from_node(
                    {"number": 1, "timelineItems": bad}
                )
                self.assertEqual(children.get("timeline"), [])
                self.assertTrue(
                    children.get("timeline_incomplete"),
                    f"malformed timelineItems {bad!r} was not flagged: {children}",
                )

    def test_timeline_null_is_not_carried(self) -> None:
        """An absent (null) timeline section touches nothing — the key is
        absent from the children dict, so a degraded re-ingest cannot
        retract the stored events."""
        children = _source()._children_from_node(
            {"number": 1, "timelineItems": None}
        )
        self.assertNotIn("timeline", children)
        self.assertNotIn("timeline_incomplete", children)


class TimelineDrainSafetyTests(unittest.TestCase):
    """PRR-004, second half: a continuation must never clear the flag for a
    page whose events are not a faithful prefix.

    `_complete_timeline` clears `timeline_incomplete` when a continuation
    reports `hasNextPage: false`. The INITIAL page's malformed-container
    shapes already emit no cursor; these legs pin the shapes that still
    emitted one — a dropped non-dict node and a `totalCount` above the
    rendered count — where a well-formed drain replaced the stored
    `item_events` with a partial timeline (data loss)."""

    def _initial(self, **timeline) -> dict:
        node = {"number": 1, "id": "I_1", "timelineItems": timeline}
        return _source()._children_from_node(node)

    def test_dropped_node_emits_no_cursor(self) -> None:
        children = self._initial(
            nodes=[{"__typename": "ReopenedEvent"}, "garbage"],
            pageInfo={"hasNextPage": True, "endCursor": "X"},
        )
        self.assertTrue(children.get("timeline_incomplete"))
        self.assertNotIn(
            "timeline_cursor",
            children,
            "a page with a dropped node must not hand the drain a cursor",
        )

    def test_totalcount_overflow_emits_no_cursor(self) -> None:
        """A `totalCount` above the rendered count matters only on a page
        that says there is no next page: while `hasNextPage` is true the
        count is the collection's size and exceeding it is ordinary
        pagination (pinned by the PRESERVING floor below)."""
        children = self._initial(
            nodes=[{"__typename": "ReopenedEvent"}],
            pageInfo={"hasNextPage": False, "endCursor": "X"},
            totalCount=9,
        )
        self.assertTrue(children.get("timeline_incomplete"))
        self.assertNotIn("timeline_cursor", children)

    def test_faithful_overflowing_page_still_emits_a_cursor(self) -> None:
        """PRESERVING floor: ordinary pagination (hasNextPage true, every
        reported node rendered) keeps the cursor — the drain is the
        designed completion path, not the defect."""
        children = self._initial(
            nodes=[{"__typename": "ReopenedEvent"}, {"__typename": "ClosedEvent"}],
            pageInfo={"hasNextPage": True, "endCursor": "X"},
            totalCount=2,
        )
        self.assertTrue(children.get("timeline_incomplete"))
        self.assertEqual(children.get("timeline_cursor"), "X")

    def test_paginating_totalcount_still_emits_a_cursor(self) -> None:
        """PRESERVING floor: a page whose `totalCount` exceeds its node
        count WHILE hasNextPage is true is paginating, not truncated — the
        cursor must still be emitted or the drain can never finish a large
        timeline."""
        children = self._initial(
            nodes=[{"__typename": "ReopenedEvent"}],
            pageInfo={"hasNextPage": True, "endCursor": "X"},
            totalCount=61,
        )
        self.assertTrue(children.get("timeline_incomplete"))
        self.assertEqual(children.get("timeline_cursor"), "X")

    def test_null_pageinfo_flags_the_section(self) -> None:
        """A carried `pageInfo: null` is NOT a complete page.

        `_connection` reads a null pageInfo as `{}` (no next page), so the
        carried connection alone reports complete — the flag would never be
        set and `write_item_events` would replace the stored rows with the
        page's subset. The completeness decision must use the same predicate
        as the cursor decision (re-gate finding NEW-1)."""
        children = self._initial(
            nodes=[{"__typename": "ReopenedEvent"}],
            pageInfo=None,
        )
        self.assertTrue(
            children.get("timeline_incomplete"),
            f"a null pageInfo was not flagged: {children}",
        )
        self.assertNotIn("timeline_cursor", children)
        # an OMITTED pageInfo is the same shape
        omitted = _source()._children_from_node(
            {
                "number": 1,
                "id": "I_1",
                "timelineItems": {"nodes": [{"__typename": "ReopenedEvent"}]},
            }
        )
        self.assertTrue(
            omitted.get("timeline_incomplete"),
            f"an omitted pageInfo was not flagged: {omitted}",
        )
        self.assertNotIn("timeline_cursor", omitted)

    def test_paginating_final_page_still_drains(self) -> None:
        """PRESERVING floor for the totalCount rule: a continuation page
        whose `totalCount` is the COLLECTION's size (legitimately above the
        page's node count) with `hasNextPage: false` is a normal final page,
        not a truncated one — the drain must finish it."""

        class _PageSource(GraphQLSource):
            """Initial page (hasNextPage true, 60 events), then a final
            continuation page of 20 carrying the collection-wide count."""

            def _graphql(self, query: str, aliases: dict | None = None) -> dict:
                self.last_dropped_sections = {}
                if "after:" in query:
                    nodes = [
                        {"__typename": "ReopenedEvent", "createdAt": "t2"}
                        for _ in range(20)
                    ]
                    node = {
                        "timelineItems": {
                            "nodes": nodes,
                            "totalCount": 80,
                            "pageInfo": {"hasNextPage": False, "endCursor": "Y"},
                        }
                    }
                else:
                    nodes = [
                        {"__typename": "ReopenedEvent", "createdAt": "t1"}
                        for _ in range(60)
                    ]
                    node = {
                        "number": 1,
                        "id": "I_1",
                        "timelineItems": {
                            "nodes": nodes,
                            "totalCount": 80,
                            "pageInfo": {"hasNextPage": True, "endCursor": "X"},
                        },
                    }
                alias = ("p" if "pullRequest(" in query else "i") + "1"
                return {"repository": {alias: node}}

        src = _PageSource("probe", "clinic")
        child = src._children_from_node(
            {
                "number": 1,
                "id": "I_1",
                "timelineItems": {
                    "nodes": [
                        {"__typename": "ReopenedEvent", "createdAt": "t1"}
                        for _ in range(60)
                    ],
                    "totalCount": 80,
                    "pageInfo": {"hasNextPage": True, "endCursor": "X"},
                },
            }
        )
        self.assertEqual(child.get("timeline_cursor"), "X")
        src._complete_timeline(1, "issue", child)
        self.assertNotIn(
            "timeline_incomplete",
            child,
            "a paginating final page must drain:"
            f" {len(child.get('timeline') or [])} events",
        )
        self.assertEqual(len(child["timeline"]), 80)

    def test_unanswerable_replacement_page_keeps_the_retained_events(self) -> None:
        """The replacement page (`last:`) is answered by the same
        `_timeline_page`: when it is unanswerable the retained events must
        survive with the flag set, not be erased by an empty list (re-gate
        finding N2).

        The replacement branch is reached when the drain cannot complete —
        here the INITIAL page is unfaithful, so it emits no cursor, the
        continuation loop never runs, and `_complete_timeline` falls through
        to the replacement page."""

        class _ReplacementUnanswerable(GraphQLSource):
            def _graphql(self, query: str, aliases: dict | None = None) -> dict:
                self.last_dropped_sections = {}
                if "last:" in query:
                    # an unfaithful replacement page: a dropped non-dict node
                    node = {
                        "timelineItems": {
                            "nodes": [{"__typename": "ReopenedEvent"}, 7],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                else:
                    node = {
                        "number": 1,
                        "id": "I_1",
                        "timelineItems": {
                            "nodes": [
                                {"__typename": "ReopenedEvent", "createdAt": "t1"},
                                "garbage",
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "X"},
                        },
                    }
                alias = ("p" if "pullRequest(" in query else "i") + "1"
                return {"repository": {alias: node}}

        src = _ReplacementUnanswerable("probe", "clinic")
        child = src._children_from_node(
            {
                "number": 1,
                "id": "I_1",
                "timelineItems": {
                    "nodes": [
                        {"__typename": "ReopenedEvent", "createdAt": "t1"},
                        "garbage",
                    ],
                    "pageInfo": {"hasNextPage": True, "endCursor": "X"},
                },
            }
        )
        self.assertNotIn(
            "timeline_cursor", child, "an unfaithful page must emit no cursor"
        )
        src._complete_timeline(1, "issue", child)
        self.assertTrue(
            child.get("timeline_incomplete"),
            "an unanswerable replacement page must keep the flag set",
        )
        self.assertEqual(
            [e["created_at"] for e in child.get("timeline") or []],
            ["t1"],
            "the retained events must survive an unanswerable replacement page",
        )

    def test_wrong_typed_totalcount_is_not_an_answer(self) -> None:
        """A `totalCount` the store cannot compare (a string, a float) on a
        page that claims to be last is not an answer we can check, so the
        page is not faithful -- reading it as absent would let
        `write_item_events` replace the stored rows. GitHub emits an int or
        omits the key; this arm is the malformed-payload contract."""
        for bad in ("9", 9.5, True, [9]):
            with self.subTest(totalCount=bad):
                children = self._initial(
                    nodes=[{"__typename": "ReopenedEvent"}],
                    pageInfo={"hasNextPage": False, "endCursor": "X"},
                    totalCount=bad,
                )
                self.assertTrue(
                    children.get("timeline_incomplete"),
                    f"totalCount {bad!r} was not flagged: {children}",
                )
                self.assertNotIn("timeline_cursor", children)

    def test_unfaithful_continuation_page_keeps_the_flag(self) -> None:
        """The continuation page has the same hole: `_timeline_page` must
        report an unfaithful page as unanswerable so the drain cannot
        clear the flag on a partial accumulated set."""

        class _PageSource(GraphQLSource):
            """Answers the initial chunk, then a continuation whose `nodes`
            carries a dropped non-dict node and reports no next page."""

            def _graphql(self, query: str, aliases: dict | None = None) -> dict:
                self.last_dropped_sections = {}
                if "after:" in query:
                    node = {
                        "timelineItems": {
                            "nodes": [{"__typename": "ReopenedEvent"}, 7],
                            "pageInfo": {"hasNextPage": False, "endCursor": "Y"},
                            "totalCount": 4,
                        }
                    }
                else:
                    node = {
                        "number": 1,
                        "id": "I_1",
                        "timelineItems": {
                            "nodes": [
                                {"__typename": "ReopenedEvent", "createdAt": "t1"},
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "X"},
                        },
                    }
                alias = ("p" if "pullRequest(" in query else "i") + "1"
                return {"repository": {alias: node}}

        src = _PageSource("probe", "clinic")
        child = src._children_from_node(
            {
                "number": 1,
                "id": "I_1",
                "timelineItems": {
                    "nodes": [{"__typename": "ReopenedEvent", "createdAt": "t1"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "X"},
                },
            }
        )
        self.assertEqual(child.get("timeline_cursor"), "X")
        src._complete_timeline(1, "issue", child)
        self.assertTrue(
            child.get("timeline_incomplete"),
            "an unfaithful continuation page must keep the flag set: "
            f"{child}",
        )
        self.assertNotIn("timeline_cursor", child)


class CarriedSectionTests(unittest.TestCase):
    """PRR-010: every CARRIED section, not only the link connections, must
    degrade a malformed container to union-only — a silently empty page
    presented as complete would delete the stored children on re-ingest
    (issue #53, review PRR-010).

    The malformed-carried-container class is PER-SECTION (the normalizer
    carries eight of them), so each section gets its own leg: the container
    is malformed, the section's incomplete flag is set, and the section
    never reads as a complete empty page.
    """

    def _assert_flagged(self, name: str, node: dict, out_key: str,
                        flag: str, nested: tuple[str, list] | None = None) -> None:
        payload = {"number": 1, "id": "I_1"}
        payload.update(node)
        children = _source()._children_from_node(payload)
        self.assertTrue(
            children.get(flag),
            f"malformed {name} container was not flagged: {children}",
        )
        if nested is None:
            self.assertEqual(
                children.get(out_key),
                [],
                f"malformed {name} container must not present a complete page",
            )
        else:
            # the section lives inside a payload object (the pull section)
            key, want = nested
            self.assertEqual(
                children[out_key][key],
                want,
                f"malformed {name} container must not present a complete page",
            )

    def test_malformed_comments_container(self) -> None:
        self._assert_flagged(
            "comments", {"comments": "garbage"}, "issue_comments",
            "issue_comments_incomplete",
        )

    def test_malformed_reviews_container(self) -> None:
        self._assert_flagged(
            "reviews", {"reviews": 7}, "reviews", "reviews_incomplete"
        )

    def test_malformed_nested_review_comments_container(self) -> None:
        """Review comments ride the per-review `comments` connection, so a
        malformed one degrades that review's comments — never the whole
        review list."""
        self._assert_flagged(
            "review comments",
            {"reviews": {"nodes": [{"comments": "garbage"}]}},
            "review_comments",
            "review_comments_incomplete",
        )

    def test_malformed_files_container(self) -> None:
        self._assert_flagged(
            "files", {"files": "garbage"}, "files", "files_incomplete"
        )

    def test_absent_closing_refs_field_is_not_truncated(self) -> None:
        """A capability mask leaves the field out of the node; an unanswered
        section is not a truncated one (cubic PR #54 round 1). Calling
        `_carried_connection(None)` reported every affected PR as
        "closing references truncated"."""
        children = _source()._children_from_node(
            {
                "number": 1,
                "id": "I_1",
                "baseRefName": "main",
                "mergedAt": "2026-01-02T00:00:00Z",
            }
        )
        self.assertNotIn(
            "closing_refs_incomplete",
            children,
            "an absent closingIssuesReferences field must not be flagged"
            f" truncated: {children}",
        )
        self.assertEqual(
            children["pull"]["closing_issues_references"],
            [],
            "an absent field still emits the REST-shaped key as empty",
        )

    def test_present_null_closing_refs_is_still_malformed(self) -> None:
        """PRESERVING floor: a field PRESENT with null is a malformed
        carried section, so it stays flagged — the presence check must not
        swallow the truth table."""
        children = _source()._children_from_node(
            {
                "number": 1,
                "id": "I_1",
                "baseRefName": "main",
                "mergedAt": "2026-01-02T00:00:00Z",
                "closingIssuesReferences": None,
            }
        )
        self.assertTrue(
            children.get("closing_refs_incomplete"),
            "a present-null closingIssuesReferences must stay flagged:"
            f" {children}",
        )

    def test_malformed_closing_refs_container(self) -> None:
        """The closingIssuesReferences connection lives inside the pull
        gate (its own baseRefName/mergedAt fields), so the fixture carries
        a pull field to reach it."""
        self._assert_flagged(
            "closingIssuesReferences",
            {"baseRefName": "main", "closingIssuesReferences": 7},
            "pull",
            "closing_refs_incomplete",
            nested=("closing_issues_references", []),
        )


class CarriedSectionE2ETests(TempDBTest):
    """The same class end-to-end: the flag reaches `sync_repo`, so the
    item's children are NOT replaced with the silently empty page — the
    stored rows survive the re-ingest (issue #53, review PRR-010)."""

    def test_malformed_container_survives_the_public_seam(self) -> None:

        class _StubbedTransport(GraphQLSource):
            def __init__(self) -> None:
                super().__init__("probe", "clinic")
                self.last_dropped_sections: dict[int, list] = {}

            def _graphql(self, query: str, aliases: dict | None = None) -> dict:
                node = {"number": 10, "id": "I_10", "comments": "garbage"}
                return {"repository": {"i10": node}}

        class _SeamSource(FakeGitHubSource):
            def __init__(self) -> None:
                super().__init__()
                self._gql = _StubbedTransport()

            def fetch_children(self, items) -> dict[int, dict]:
                return self._gql.fetch_children(items)

        src = _BulkLinkSource()
        src.add_issue(issue(10, title="commented work"))
        src.comment_on(10, "kept comment")
        sync_repo(self.conn, src, REPO)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM comments WHERE number = 10"
            ).fetchone()[0],
            1,
        )
        src.issues[10]["updated_at"] = ts(300_010)
        seam = _SeamSource()
        seam.issues[10] = src.issues[10]
        # the REST fallback answers the section the GraphQL page could not:
        # if the incomplete flag were NOT set, the silently empty GraphQL
        # page would be trusted and the stored comment deleted
        seam.issue_comments[10] = src.issue_comments[10]
        sync_repo(self.conn, seam, REPO)
        self.assertEqual(
            self.conn.execute(
                "SELECT body FROM comments WHERE number = 10"
            ).fetchone()[0],
            "kept comment",
            "a malformed comments container must not retract the stored rows",
        )


    def test_null_pageinfo_does_not_erase_stored_events(self) -> None:
        """E2E for the same shape through the public seam: the flag reaches
        `sync_repo`, so `write_item_events` keeps the stored rows."""
        src = _BulkLinkSource()
        src.add_issue(issue(10, title="eventful work"))
        sync_repo(self.conn, src, REPO)
        for day in ("2026-01-03", "2026-01-04"):
            self.conn.execute(
                "INSERT INTO item_events(repo, number, type, created_at)"
                " VALUES (?, 10, 'reopened', ?)",
                (REPO, f"{day}T00:00:00Z"),
            )
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM item_events WHERE number = 10"
            ).fetchone()[0],
            2,
        )
        src.issues[10]["updated_at"] = ts(500_010)

        class _StubbedTransport(GraphQLSource):
            def __init__(self) -> None:
                super().__init__("probe", "clinic")
                self.last_dropped_sections: dict[int, list] = {}

            def _graphql(self, query: str, aliases: dict | None = None) -> dict:
                node = {
                    "number": 10,
                    "id": "I_10",
                    "timelineItems": {
                        "nodes": [
                            {
                                "__typename": "ReopenedEvent",
                                "createdAt": "2026-01-02T00:00:00Z",
                            }
                        ],
                        "pageInfo": None,
                    },
                }
                return {"repository": {"i10": node}}

        class _SeamSource(FakeGitHubSource):
            def __init__(self) -> None:
                super().__init__()
                self._gql = _StubbedTransport()

            def fetch_children(self, items) -> dict[int, dict]:
                return self._gql.fetch_children(items)

        seam = _SeamSource()
        seam.issues[10] = src.issues[10]
        sync_repo(self.conn, seam, REPO)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM item_events WHERE number = 10"
            ).fetchone()[0],
            2,
            "a null pageInfo must not present the page as complete and"
            " erase the stored events",
        )


class StoreLayerBoolTests(TempDBTest):
    """PRR-003's store-layer leg: `_link_dst` rejects a boolean `number`
    on its own, so a store-layer caller (a hand-built bundle, a replay)
    can never stringify `True` into dst_id and retract a stored link edge.
    The GraphQL layer's guard is pinned separately by the seam test; this
    is the defense-in-depth half."""

    def test_bool_number_is_not_a_destination(self) -> None:
        from zaxbygraph.extract import _link_dst

        self.assertIsNone(
            _link_dst(REPO, {"number": True, "repo": None}, 10),
            "a boolean number must not become a link destination",
        )
        self.assertIsNone(
            _link_dst(REPO, {"number": False, "repo": None}, 10),
            "False is 0 in Python but is not a number in JSON",
        )

    def test_bool_number_does_not_retract_a_stored_link(self) -> None:
        """End-to-end through the store layer: a carried connection whose
        only node carries a boolean number must leave the stored edge."""
        src = _BulkLinkSource()
        src.add_issue(issue(10, title="blocked work"))
        src.add_issue(issue(11, title="blocker one"))
        src.links[10] = _links(blocked_by=[{"number": 11, "repo": REPO}])
        sync_repo(self.conn, src, REPO)
        self.assertEqual(len(_edges(self.conn, 10)), 1)
        src.issues[10]["updated_at"] = ts(400_010)
        src.links[10] = _links(blocked_by=[{"number": True, "repo": None}])
        sync_repo(self.conn, src, REPO)
        self.assertEqual(
            _edges(self.conn, 10),
            [("blocked_by", "11", "link")],
            "a boolean node must not retract the stored edge",
        )


if __name__ == "__main__":
    unittest.main()
