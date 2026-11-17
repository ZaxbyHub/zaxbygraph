"""Issue #6 timeline flow: GraphQL mapping, bounded continuation, cap
replacement (every request <= 250), and the sync-side fetch_log trail."""
from __future__ import annotations

import json
import re
import unittest
from unittest.mock import patch

from fixtures import REPO, FakeGitHubSource, TempDBTest, issue, pull
from zaxbygraph.graphql import (
    _MAX_TIMELINE_EVENTS,
    _MAX_QUERY_CHARS,
    _TIMELINE_CONTINUATION_PAGE,
    _TIMELINE_PAGE_MAX,
    GitHubError,
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


def _filtered_pr_node(events: list[dict] | None = None) -> dict:
    """The issue #75 shape, PRODUCTION-shaped (round 7): a GraphQL
    chunk/response node as _PULL_FIELDS actually renders it — baseRefName /
    headRefName carry the PR-ness, and the REST-only ``pull_request``
    LISTING marker is deliberately ABSENT (that key exists on listing rows,
    never on chunk nodes). On a PR, GitHub's timelineItems.totalCount
    IGNORES the itemTypes filter, so a COMPLETE last page reports the
    COLLECTION's count (42) beside the filtered page's own filteredCount
    (len(events)) and exactly that many rendered nodes. Direct-call checks
    pass the kind explicitly: ``_children_from_node(node, kind="pr")`` —
    the contract the fix threads from the chunk builder; marker inference
    is not relied on."""
    if events is None:
        events = [_closed_event(), _cross_event(9)]
    return {
        "number": 63,
        "id": "PR_63",
        "baseRefName": "main",
        "headRefName": "feat/63",
        "timelineItems": {
            "totalCount": 42,
            "filteredCount": len(events),
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": events,
        },
    }


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

    def __call__(self, query: str, aliases: dict[str, int] | None = None) -> dict:
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


class FilteredCountTests(unittest.TestCase):
    """Issue #75: a PR's timelineItems.totalCount ignores the itemTypes
    filter, so a COMPLETE filtered last page reports totalCount 42 beside
    filteredCount 2 and exactly 2 rendered nodes. Base compares totalCount
    against the rendered count, misjudges the page unfaithful, flags it
    truncated, and burns an extra `last: 250` completion request on a
    timeline that was never incomplete. The faithfulness comparison must be
    filteredCount-aware (the filtered page's own count) and kind-aware."""

    def test_filtered_pr_timeline_page_is_complete(self) -> None:
        child = GraphQLSource(*SRC)._children_from_node(
            _filtered_pr_node(), kind="pr"
        )
        self.assertNotIn(
            "timeline_incomplete",
            child,
            "a complete filtered PR page must not be flagged truncated",
        )
        self.assertNotIn("timeline_cursor", child)
        self.assertEqual(
            [e["type"] for e in child["timeline"]],
            ["closed", "cross_referenced"],
            "the filtered page's events must survive unflagged",
        )

    def test_filtered_pr_timeline_page_costs_no_extra_request(self) -> None:
        class _FilteredPageSource(GraphQLSource):
            """Answers every query with the C1 page and RECORDS every query
            string handed to _graphql (round 7): the bulk children chunk,
            and — at base — the extra `last: 250` completion request this
            test counts. The captured query texts are the query-rendering
            proof: every timeline-bearing query the production path renders
            must SELECT filteredCount (issue #75: the faithfulness comparison
            consumes it), or the fix only patched the comparison against a
            field the queries never fetch."""

            def __init__(self, *args) -> None:
                super().__init__(*args)
                self.captured_queries: list[str] = []
                # The real `_graphql` initializes these; the stub replaces it.
                self.last_dropped_sections: dict[int, list] = {}

            def _graphql(self, query: str, aliases: dict | None = None) -> dict:
                self.captured_queries.append(query)
                self.last_dropped_sections = {}
                return {"repository": {"p63": _filtered_pr_node()}}

        source = _FilteredPageSource(*SRC)
        listing = [
            {
                "id": 2,
                "number": 63,
                "title": "pr sixty-three",
                "pull_request": {"url": "x"},
            }
        ]
        # The counting wrapper also partitions the captured queries: the
        # ones issued INSIDE a _timeline_page call are the extra completion
        # requests (the base `last: 250` leg) — arm (b) below pins their
        # rendered text whenever they fire.
        timeline_page_queries: list[str] = []
        real_page = source._timeline_page

        def _counting_page(*args, **kwargs):
            before = len(source.captured_queries)
            result = real_page(*args, **kwargs)
            timeline_page_queries.extend(source.captured_queries[before:])
            return result

        with patch.object(source, "_timeline_page", side_effect=_counting_page) as page:
            children = source.fetch_children(listing)
        self.assertIn(63, children)
        self.assertEqual(
            [e["type"] for e in children[63]["timeline"]],
            ["closed", "cross_referenced"],
            "the chunk path must have processed the PR node",
        )
        self.assertTrue(
            source.captured_queries,
            "fetch_children must have issued at least one query",
        )
        # (b) WHEN the extra completion request fires (it does at base —
        # that is the bug), its rendered query must select filteredCount
        # too: the continuation path shares the timeline selection, and a
        # comparison against a field only the bulk query fetches would
        # misjudge every continuation page. This conditional leg is checked
        # FIRST so it is exercised at base, where the extra request still
        # fires (post-fix it never fires and the leg is skipped).
        if page.call_count:
            self.assertTrue(
                timeline_page_queries,
                "fixture sanity: _timeline_page fired but captured no"
                " queries",
            )
            for q in timeline_page_queries:
                self.assertIn(
                    "filteredCount",
                    q,
                    "a timeline completion query must SELECT filteredCount"
                    f" (rendered: {q[:400]!r})",
                )
        # (a) the production bulk children query selects filteredCount.
        # The bulk chunk query is the one NOT issued inside a _timeline_page
        # call (the chunk render; at base it is the first captured query).
        bulk_queries = [
            q for q in source.captured_queries if q not in timeline_page_queries
        ]
        self.assertTrue(
            bulk_queries,
            "fixture sanity: a bulk children chunk query must have been"
            f" captured (all: {source.captured_queries[:1]})",
        )
        self.assertTrue(
            any("filteredCount" in q for q in bulk_queries),
            "the production bulk children query must SELECT filteredCount"
            f" (rendered: {bulk_queries[0][:400]!r})",
        )
        # The discriminator stays: a complete filtered PR page must cost
        # NO extra timeline request at all.
        self.assertEqual(
            page.call_count,
            0,
            "a complete filtered PR page must cost no extra timeline request"
            f" (calls: {page.call_args_list})",
        )

    def test_pr_page_without_filtered_count_is_not_flagged_on_totalcount(self) -> None:
        node = _filtered_pr_node()
        del node["timelineItems"]["filteredCount"]
        child = GraphQLSource(*SRC)._children_from_node(node, kind="pr")
        self.assertNotIn(
            "timeline_incomplete",
            child,
            "a PR page's totalCount ignores the itemTypes filter, so without"
            " filteredCount it must not be judged truncated on totalCount",
        )
        self.assertEqual(len(child["timeline"]), 2)

    def test_pr_filtered_count_overflow_preserves_truncation_flag(self) -> None:
        """C20 (round 7): filteredCount OVERFLOW preservation. The fix makes
        the faithfulness comparison filteredCount-aware — so the protection
        must survive on the NEW field, not only on totalCount: a page whose
        filteredCount exceeds its rendered node count (5 > 2) while claiming
        to be last (hasNextPage false) is a REAL filtered truncation and must
        stay flagged with NO cursor (the dropped-nodes contract, issue #53
        PRR-004, carried onto the filtered count). And the field must
        actually be REQUESTED: both selection builders (the bulk page
        template and the continuation/single-item selection) must render
        filteredCount, or the comparison reads a field no query fetches.

        RED at base on the selection arm: neither builder renders
        filteredCount at 5b41510 (the base flag on this shape would fire
        through the totalCount arm — for the wrong reason — so the honest
        discriminator is the query selection itself). Post-fix both arms
        are green."""
        from zaxbygraph.graphql import (
            _TIMELINE_TYPES_ALL,
            _q,
            _timeline_nodes,
            _timeline_selection,
        )

        source = GraphQLSource(*SRC)

        # ---- arm 1: the query selection carries filteredCount ----------
        # Both timeline selection builders: _timeline_nodes renders the bulk
        # page query's timelineItems section (_TIMELINE_NODES rides
        # _PULL_FIELDS/_ISSUE_FIELDS into the chunk query C2 captures);
        # _timeline_selection renders the continuation and single-item page
        # queries.
        bulk_selection = _timeline_nodes(_TIMELINE_TYPES_ALL)
        self.assertIn(
            "filteredCount",
            bulk_selection,
            "the bulk page query's timelineItems selection must SELECT"
            " filteredCount (rendered:"
            f" {bulk_selection[:200]!r})",
        )
        continuation_selection = _timeline_selection(
            f"first: {_TIMELINE_CONTINUATION_PAGE} after: {_q('c1')}"
        )
        self.assertIn(
            "filteredCount",
            continuation_selection,
            "the continuation timeline selection must SELECT filteredCount"
            f" (rendered: {continuation_selection[:200]!r})",
        )

        # ---- arm 2: filteredCount overflow flags and emits no cursor ----
        # Production-shaped PR node (explicit kind; no listing marker), a
        # REAL filtered truncation: filteredCount 5 against 2 rendered
        # nodes on a page claiming to be last. The dropped/filtered-nodes
        # protection must fire on filteredCount itself.
        events = [_closed_event(), _cross_event(9)]
        node = {
            "number": 63,
            "id": "PR_63",
            "baseRefName": "main",
            "headRefName": "feat/63",
            "timelineItems": {
                "totalCount": 5,
                "filteredCount": 5,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": events,
            },
        }
        child = source._children_from_node(node, kind="pr")
        self.assertIn(
            "timeline_incomplete",
            child,
            "a page whose filteredCount (5) exceeds its rendered nodes (2)"
            " is a REAL filtered truncation — the flag must stay set on the"
            " filteredCount signal itself",
        )
        self.assertNotIn(
            "timeline_cursor",
            child,
            "a filtered-truncated page emits no cursor: _complete_timeline"
            " must not drain past a prefix whose missing events are"
            " unknowable",
        )
        self.assertEqual(
            [e["type"] for e in child["timeline"]],
            ["closed", "cross_referenced"],
            "the rendered events themselves survive the flag",
        )


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


class FilteredCountSyncTests(TempDBTest):
    """Issue #75 end-to-end through the REAL sync ingest path: the reopen
    event on a complete filtered PR page must reach item_events, and the
    page must not be logged as a truncated timeline."""

    def test_filtered_pr_reopen_event_is_stored(self) -> None:
        reopened = {
            "__typename": "ReopenedEvent",
            "createdAt": "2026-02-01T00:00:00Z",
        }
        node = _filtered_pr_node([reopened, _closed_event(), _cross_event(9)])

        class _StubbedTransport(GraphQLSource):
            """Answers every query with the filtered PR page: the children
            chunk, and — at base — the extra completion request the fix
            removes."""

            def __init__(self) -> None:
                super().__init__("probe", "clinic")
                # The real `_graphql` initializes these; the stub replaces it.
                self.last_dropped_sections: dict[int, list] = {}

            def _graphql(self, query: str, aliases: dict | None = None) -> dict:
                self.last_dropped_sections = {}
                return {"repository": {"p63": node}}

        class _SeamSource(FakeGitHubSource):
            def __init__(self) -> None:
                super().__init__()
                self._gql = _StubbedTransport()

            def fetch_children(self, items) -> dict[int, dict]:
                return self._gql.fetch_children(items)

        src = _SeamSource()
        src.add_pr(
            issue(63, title="pr with a filtered timeline", kind="pr"),
            pull(63),
        )
        result = sync_repo(self.conn, src, REPO)
        self.assertIsNone(result["last_error"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM item_events WHERE repo = ?"
                " AND number = 63 AND type = 'reopened'",
                (REPO,),
            ).fetchone()[0],
            1,
            "the complete filtered page's reopen event must be stored",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM fetch_log WHERE resource = 'item'"
                " AND resource_id = '63' AND note LIKE 'timeline truncated%'"
            ).fetchone()[0],
            0,
            "a complete filtered PR page must not be logged truncated",
        )

    def test_filtered_count_unsupported_host_retries_without_the_field(self) -> None:
        """AC22 (plan-critic round 7): a host whose schema lacks
        filteredCount answers a GraphQL validation error naming the field;
        the source must cache the absence, retry the affected query ONCE
        with the field removed, and omit the field from every later query.
        The retry's payload genuinely lacks filteredCount, and a PR page
        whose payload lacks the field is never flagged on totalCount (AC5)."""
        node = _filtered_pr_node([_closed_event(), _cross_event(9)])
        # The degrade payload genuinely lacks the field (the retry strips
        # it from the query, so the answer cannot carry it).
        del node["timelineItems"]["filteredCount"]

        queries: list[str] = []
        failures = {"n": 0}

        class _SeamSource(FakeGitHubSource):
            def __init__(self) -> None:
                super().__init__()
                self._gql = GraphQLSource(*SRC)

            def fetch_children(self, items) -> dict[int, dict]:
                return self._gql.fetch_children(items)

            def _timeline_page(self, number: int, kind: str, args: str):
                return self._gql._timeline_page(number, kind, args)

        src = _SeamSource()
        src.add_pr(issue(63, title="degrading host", kind="pr"), pull(63))

        # The degrading host answers at the TRANSPORT level, so the
        # production _graphql — and therefore its shared retry seam — runs
        # for real: while any rendered query selects filteredCount the host
        # answers a payload-level validation error naming the field; once
        # the query omits it, the canned page is served.
        import subprocess as _sp

        real_run = _sp.run

        def _degrading_run(cmd, **kwargs):
            # Only the GraphQL legs degrade; REST listing legs keep the
            # fixture's canned answers (the fixture never shells out, so an
            # empty JSON is the right shape for them).
            is_graphql = "graphql" in cmd
            body = kwargs.get("input") or ""
            if is_graphql and isinstance(body, str) and body:
                try:
                    queries.append(json.loads(body)["query"])
                except Exception:
                    queries.append(body)
            if is_graphql and "filteredCount" in body:
                failures["n"] += 1
                out = {
                    "errors": [
                        {
                            "message": (
                                "Field 'filteredCount' doesn't exist on type"
                                " 'IssueTimelineItemsConnection'"
                            ),
                            "type": "VALIDATION_ERROR",
                        }
                    ],
                    "data": None,
                }
            elif is_graphql:
                out = {"data": {"repository": {"p63": node}}}
            else:
                out = {}
            return _sp.CompletedProcess(cmd, 0, json.dumps(out), "")

        patcher = patch("subprocess.run", _degrading_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        result = sync_repo(self.conn, src, REPO)
        self.assertIsNone(
            result["last_error"],
            "the unsupported-field retry must complete the sync"
            f" (last_error: {result['last_error']!r})",
        )
        self.assertTrue(queries, "the transport must have been called")
        self.assertNotIn(
            "filteredCount", queries[-1],
            "the retried query must omit the field",
        )
        # AC22's full contract (plan-critic rounds 8+9): the FIRST query
        # selects the field, exactly ONE stripped retry immediately follows
        # it, the absence is CACHED, and EVERY later query (bulk AND
        # continuation) omits the field without a second validation failure.
        # A stateless per-query retry, a bulk-only cache, two retries, or a
        # continuation that still selects the field cannot pass.
        self.assertIn(
            "filteredCount", queries[0],
            "the initial query must select filteredCount",
        )
        self.assertEqual(
            failures["n"], 1,
            "exactly ONE validation failure may fire (the first query"
            " before the cache)",
        )
        self.assertNotIn(
            "filteredCount", queries[1],
            "exactly one stripped retry must immediately follow the failed"
            " initial query",
        )
        self.assertEqual(
            len(queries), 2,
            "the initial failure exchange must be EXACTLY one failed query"
            " plus one stripped retry (plan-critic round 10: a second"
            " successful stripped call is not the contract)",
        )
        second_bulk_start = len(queries)
        src._gql.fetch_children(
            [{"number": 63, "id": "PR_63", "pull_request": {"url": "x"}}]
        )
        self.assertGreater(
            len(queries), second_bulk_start,
            "the second bulk fetch must issue its own query",
        )
        child = {
            "number": 63,
            "id": "PR_63",
            "timeline": [],
            "timeline_incomplete": True,
            "timeline_cursor": "c1",
        }
        continuation_start = len(queries)
        src._gql._complete_timeline(63, "pr", child)
        continuation_qs = queries[continuation_start:]
        self.assertTrue(
            continuation_qs,
            "the continuation must issue its own query",
        )
        for q in queries[1:]:
            self.assertNotIn(
                "filteredCount", q,
                "every query after the single stripped retry must omit the"
                " field (cache consulted)",
            )
        self.assertEqual(
            failures["n"], 1,
            "no second validation failure may fire (the cache was not"
            " consulted if one did)",
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM fetch_log WHERE resource = 'item'"
                " AND resource_id = '63' AND note LIKE 'timeline truncated%'"
            ).fetchone()[0],
            0,
            "a PR page whose payload lacks filteredCount is never flagged"
            " on totalCount (AC5)",
        )


if __name__ == "__main__":
    unittest.main()
