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

import unittest
from copy import deepcopy

from fixtures import REPO, FakeGitHubSource, TempDBTest, issue, pull, ts
from zaxbygraph.graphql import GraphQLSource
from zaxbygraph.sync import sync_repo


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


if __name__ == "__main__":
    unittest.main()
