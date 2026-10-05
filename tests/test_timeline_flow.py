"""Issue #6 timeline flow: GraphQL mapping, bounded continuation, cap
replacement (every request <= 250), and the sync-side fetch_log trail."""
from __future__ import annotations

import json
import re
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

    def test_timeline_selection_is_brace_balanced(self) -> None:
        """Live-sync regression (PR #14 review comment): the _timeline_selection
        template shipped with one extra closing brace, so every continuation
        query failed GitHub's parser with RCURLY while the mocked tests —
        which parse with the same template — stayed green. A static balance
        check catches this offline; canned fakes never can."""
        from zaxbygraph.graphql import _q, _timeline_selection

        source = GraphQLSource(*SRC)
        for args in (
            "first: 50",
            f"first: {_TIMELINE_CONTINUATION_PAGE} after: {_q('c1')}",
            f"last: {_TIMELINE_PAGE_MAX}",
            f"last: {_TIMELINE_PAGE_MAX} before: {_q('s1')}",
        ):
            selection = _timeline_selection(args)
            balance = selection.count("{") - selection.count("}")
            self.assertEqual(
                balance,
                0,
                f"brace-unbalanced timeline selection for args {args!r}",
            )
            full_query = (
                "query { rateLimit { remaining resetAt } "
                f"repository(owner: {_q(source.owner)}, name: {_q(source.repo)}) {{ "
                f"i1: issue(number: 1) {{ {_timeline_selection(args)} }} }} }}"
            )
            self.assertEqual(
                full_query.count("{") - full_query.count("}"),
                0,
                "the full rendered query must be brace-balanced",
            )


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
        # Every timeline request stays inside the GitHub per-request cap —
        # asserted NUMERICALLY per query so `last: 251`-style mutants fail —
        # and the older page walks back through the newest page's startCursor.
        for query in api.queries:
            self.assertIn("timelineItems(", query)
            for size in re.findall(r"(?:first|last): (\d+)", query):
                self.assertLessEqual(
                    int(size),
                    _TIMELINE_PAGE_MAX,
                    f"per-request cap violated: {query}",
                )
        self.assertIn("last: 250", api.queries[0])
        self.assertIn('before: "newest-start"', api.queries[1])
        self.assertIn("last: 250", api.queries[1])


class DispatchAndMappingTests(unittest.TestCase):
    """PRR-028: fetch_children really dispatches into _complete_timeline with
    the right kind, and the closing-refs overflow mapping raises its flag."""

    def test_fetch_children_dispatches_flagged_timelines_with_kind(self) -> None:
        """The real fetch_children dispatch loop: only flagged children are
        completed, and `kind` is derived from the listing payload
        (pull_request present → 'pr')."""
        source = GraphQLSource(*SRC)
        flagged = _child_with_timeline([_closed_event()] * 60, cursor="c1")
        complete = _child_with_timeline([_closed_event()], cursor=None)
        calls: list[tuple[int, str]] = []

        def fake_complete(self, number, kind, child):
            calls.append((number, kind))

        listing = [
            {"id": 1, "number": 1, "title": "issue one"},
            {"id": 2, "number": 2, "title": "pr two", "pull_request": {"url": "x"}},
        ]
        with patch.object(GraphQLSource, "_fetch_children_chunk", return_value={1: complete, 2: dict(flagged)}), patch.object(
            GraphQLSource, "_complete_timeline", fake_complete
        ):
            source.fetch_children(listing)
        self.assertEqual(calls, [(2, "pr")])

    def test_closing_refs_overflow_sets_the_incomplete_flag(self) -> None:
        source = GraphQLSource(*SRC)
        node = {
            "databaseId": 1,
            "baseRefName": "main",
            "headRefName": "feat/1",
            "mergeCommit": {"oid": "abc"},
            "closingIssuesReferences": {
                "totalCount": 5,
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {"number": 1, "repository": {"nameWithOwner": "ZaxbyHub/zaxbygraph"}}
                ],
            },
        }
        child = source._children_from_node(node)
        self.assertTrue(child["closing_refs_incomplete"])
        self.assertEqual(
            child["pull"]["closing_issues_references"],
            [{"number": 1, "repo": "ZaxbyHub/zaxbygraph"}],
        )

    def test_pull_section_survives_a_masked_files_section(self) -> None:
        """PRR-008: the pull block is gated on its own fields, not on
        `files`, so a masked files connection no longer silently drops
        mergedBy / closing refs."""
        source = GraphQLSource(*SRC)
        node = {
            "databaseId": 1,
            "baseRefName": "main",
            "headRefName": "feat/1",
            "mergedAt": "2026-01-01T00:00:00Z",
            "mergeCommit": {"oid": "abc"},
            "mergedBy": {"login": "zaxbysauce"},
            "closingIssuesReferences": {
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {"number": 5, "repository": {"nameWithOwner": "ZaxbyHub/zaxbygraph"}}
                ],
            },
            # deliberately NO "files" key
        }
        child = source._children_from_node(node)
        self.assertNotIn("files", child)
        self.assertEqual(child["pull"]["merged_by"], "zaxbysauce")
        self.assertEqual(
            child["pull"]["closing_issues_references"],
            [{"number": 5, "repo": "ZaxbyHub/zaxbygraph"}],
        )


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
        child["pull"] = {
            "closing_issues_references": [
                {"number": 9, "repo": "ZaxbyHub/zaxbygraph"},
            ],
        }
        source = FakeTimelineSource(child, [1, 2])
        result = sync_repo(self.conn, source, REPO)
        self.assertIsNone(result["last_error"])
        notes = self.rows(
            "SELECT resource_id, note FROM fetch_log "
            "WHERE note LIKE 'timeline truncated%' OR note LIKE 'closing references truncated%'"
        )
        # Note counts are DERIVED from what was actually retained (the review
        # round 2 fix), not hardcoded constants.
        self.assertEqual(
            sorted(notes),
            [
                ("1", "closing references truncated: retained first 1"),
                ("1", "timeline truncated: retained newest 3 events"),
                ("2", "closing references truncated: retained first 1"),
                ("2", "timeline truncated: retained newest 3 events"),
            ],
        )
        # The events became timeline edges: items 1 and 2 each closed by
        # PR 7 (three identical events collapse to one edge per item).
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM edges WHERE source = 'timeline'"), 2
        )


if __name__ == "__main__":
    unittest.main()
