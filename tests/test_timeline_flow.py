"""Issue #6 timeline flow: GraphQL mapping, bounded continuation, cap
replacement (every request <= 250), and the sync-side fetch_log trail."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from fixtures import REPO, TempDBTest, issue
from zaxbygraph.graphql import (
    _MAX_TIMELINE_EVENTS,
    _MAX_QUERY_CHARS,
    _TIMELINE_CONTINUATION_PAGE,
    _TIMELINE_PAGE_MAX,
    GraphQLSource,
)
from zaxbygraph.sync import sync_repo

SRC = "zaxbyhub/zaxbygraph".split("/")


def _closed_event() -> dict:
    return {
        "__typename": "ClosedEvent",
        "createdAt": "2026-01-01T00:00:00Z",
        "actor": {"login": "amy"},
        "closer": {"__typename": "PullRequest", "number": 7},
    }


def _cross_event(number: int, repo: str = "ZaxbyHub/zaxbygraph") -> dict:
    return {
        "__typename": "CrossReferencedEvent",
        "createdAt": "2026-01-01T00:01:00Z",
        "actor": {"login": "bob"},
        "isCrossRepository": repo != "ZaxbyHub/zaxbygraph",
        "willCloseTarget": False,
        "source": {
            "__typename": "PullRequest",
            "number": number,
            "repository": {"nameWithOwner": repo},
        },
    }


def _timeline_page_payload(events: list[dict], info: dict) -> dict:
    # The data-level dict: GraphQLSource._graphql unwraps to this shape.
    return {
        "rateLimit": {"remaining": 900, "resetAt": "2026-10-04T13:00:00Z"},
        "repository": {
                "i1": {
                    "timelineItems": {
                        "totalCount": info.get("totalCount", len(events)),
                        "pageInfo": {
                            "hasNextPage": info.get("hasNextPage", False),
                            "hasPreviousPage": info.get("hasPreviousPage", False),
                            "startCursor": info.get("startCursor", "s"),
                            "endCursor": info.get("endCursor", "e"),
                        },
                        "nodes": events,
                    }
                }
            },
        }


def _child_with_timeline(events: list[dict], cursor: str | None) -> dict:
    source = GraphQLSource(*SRC)
    node = {
        "databaseId": 1,
        "comments": {"totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []},
        "timelineItems": {
            "totalCount": len(events) + (1 if cursor else 0),
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
            "nodes": events,
        },
    }
    child = source._children_from_node(node)
    child.pop("issue_comments", None)
    return child


class TimelineMappingTests(unittest.TestCase):
    def test_closed_event_maps_commit_and_pr_closers(self) -> None:
        source = GraphQLSource(*SRC)
        commit_node = {
            "__typename": "ClosedEvent",
            "createdAt": "2026-01-01T00:00:00Z",
            "actor": {"login": "amy"},
            "closer": {"__typename": "Commit", "oid": "abc1234"},
        }
        self.assertEqual(
            source._children_from_node(
                {"databaseId": 1, "timelineItems": {"totalCount": 1, "pageInfo": {}, "nodes": [commit_node]}}
            )["timeline"],
            [
                {
                    "type": "closed",
                    "created_at": "2026-01-01T00:00:00Z",
                    "actor_login": "amy",
                    "commit_id": "abc1234",
                    "closer_type": "commit",
                    "closer_number": None,
                }
            ],
        )
        self.assertEqual(
            source._children_from_node(
                {"databaseId": 1, "timelineItems": {"totalCount": 1, "pageInfo": {}, "nodes": [_closed_event()]}}
            )["timeline"][0]["closer_number"],
            7,
        )

    def test_cross_referenced_event_maps_source_repo_and_flag(self) -> None:
        source = GraphQLSource(*SRC)
        child = source._children_from_node(
            {
                "databaseId": 1,
                "timelineItems": {
                    "totalCount": 1,
                    "pageInfo": {},
                    "nodes": [_cross_event(42, "Other/Repo")],
                },
            }
        )
        self.assertEqual(
            child["timeline"],
            [
                {
                    "type": "cross_referenced",
                    "created_at": "2026-01-01T00:01:00Z",
                    "actor_login": "bob",
                    "source_typename": "PullRequest",
                    "source_number": 42,
                    "source_repo": "Other/Repo",
                    "is_cross_repository": True,
                }
            ],
        )

    def test_page_query_chunk_accounting_stays_under_the_char_cap(self) -> None:
        # A full GRAPHQL_CHILDREN_PAGE chunk of PRs must still render under
        # the Windows argv guard with the timeline fields included.
        from zaxbygraph.graphql import _PULL_FIELDS

        chunk = [{"number": n, "pull_request": {"url": "x"}} for n in range(1, 9)]
        self.assertLessEqual(
            GraphQLSource(*SRC)._chunk_query_len(chunk),
            _MAX_QUERY_CHARS,
            "an 8-PR chunk must fit; the source flushes earlier otherwise",
        )
        self.assertIn("timelineItems", _PULL_FIELDS)


class FakeTimelineApi:
    """Scripted _graphql replacement recording every query and answering
    timeline pages in sequence."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = list(pages)
        self.queries: list[str] = []

    def __call__(self, query: str) -> dict:
        self.queries.append(query)
        return self.pages.pop(0)


class ContinuationTests(unittest.TestCase):
    def test_drained_continuation_clears_the_incomplete_flag(self) -> None:
        source = GraphQLSource(*SRC)
        events = [_closed_event()] * 60
        child = _child_with_timeline(events, cursor="c1")
        api = FakeTimelineApi(
            [_timeline_page_payload([_cross_event(3)] * 20, {"hasNextPage": False})]
        )
        with patch.object(GraphQLSource, "_graphql", api):
            source._complete_timeline(1, "issue", child)
        self.assertNotIn("timeline_incomplete", child)
        self.assertEqual(len(child["timeline"]), 80)
        self.assertNotIn("timeline_cursor", child)
        self.assertIn("first: 100", api.queries[0])
        self.assertLessEqual(
            _TIMELINE_CONTINUATION_PAGE, _TIMELINE_PAGE_MAX, "cap discipline"
        )

    def test_capped_timeline_retains_the_newest_500_within_request_limits(self) -> None:
        source = GraphQLSource(*SRC)
        child = _child_with_timeline([_closed_event()] * _MAX_TIMELINE_EVENTS, cursor="c1")
        newest_new = [_cross_event(900 + n) for n in range(_TIMELINE_PAGE_MAX)]
        newest_old = [_cross_event(600 + n) for n in range(_TIMELINE_PAGE_MAX)]
        api = FakeTimelineApi(
            [
                _timeline_page_payload(
                    newest_new,
                    {
                        "hasNextPage": False,
                        "hasPreviousPage": True,
                        "startCursor": "newest-start",
                    },
                ),
                _timeline_page_payload(newest_old, {"hasNextPage": False}),
            ]
        )
        with patch.object(GraphQLSource, "_graphql", api):
            source._complete_timeline(1, "issue", child)
        self.assertTrue(child["timeline_incomplete"], "a capped timeline stays flagged")
        self.assertEqual(len(child["timeline"]), 2 * _TIMELINE_PAGE_MAX)
        self.assertEqual(child["timeline"][0]["source_number"], 600)
        self.assertEqual(child["timeline"][-1]["source_number"], 900 + _TIMELINE_PAGE_MAX - 1)
        # Every timeline request stays inside the GitHub per-request cap, and
        # the older page walks back through the newest page's startCursor.
        for query in api.queries:
            self.assertIn("timelineItems(", query)
            self.assertNotIn("last: 500", query)
            self.assertNotIn("first: 500", query)
        self.assertIn("last: 250", api.queries[0])
        self.assertIn('before: "newest-start"', api.queries[1])


class FakeTimelineSource:
    """Minimal GitHubSource over a temp DB: one listing page whose bulk
    children carry timeline events (the GraphQL-only path)."""

    def __init__(self, child: dict, numbers: list[int]) -> None:
        self.child = child
        self.numbers = numbers

    def list_issues(self, since=None):
        page = []
        for n in self.numbers:
            rec = issue(n, title=f"item {n}")
            page.append(rec)
        yield page

    def get_pull(self, number):
        raise AssertionError("timeline-only fixture must not REST-fallback")

    def list_issue_comments(self, number):
        return []

    def list_reviews(self, number):
        return []

    def list_review_comments(self, number):
        return []

    def list_pr_files(self, number):
        return []

    def list_releases(self):
        return []

    def fetch_children(self, items):
        return {int(r["number"]): dict(self.child) for r in items}


class SyncTrailTests(TempDBTest):
    def rows(self, sql: str) -> list[tuple]:
        return [tuple(r) for r in self.conn.execute(sql).fetchall()]

    def test_capped_timeline_logs_the_fetch_note(self) -> None:
        child = _child_with_timeline([_closed_event()] * 3, cursor="c1")
        child["timeline_incomplete"] = True
        child["closing_refs_incomplete"] = True
        source = FakeTimelineSource(child, [1, 2])
        result = sync_repo(self.conn, source, REPO)
        self.assertIsNone(result["last_error"])
        notes = self.rows(
            "SELECT resource_id, note FROM fetch_log "
            "WHERE note LIKE 'timeline truncated%' OR note LIKE 'closing references truncated%'"
        )
        self.assertEqual(
            sorted(notes),
            [
                ("1", "closing references truncated: retained first 100"),
                ("1", "timeline truncated: retained newest 500 events"),
                ("2", "closing references truncated: retained first 100"),
                ("2", "timeline truncated: retained newest 500 events"),
            ],
        )
        # The events became timeline edges: items 1 and 2 each closed by
        # PR 7 (three identical events collapse to one edge per item).
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM edges WHERE source = 'timeline'"), 2
        )


if __name__ == "__main__":
    unittest.main()
