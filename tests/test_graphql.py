"""GraphQLSource unit tests (issue #5): bulk-children mapping, batching,
incomplete flags, the deletion oracle, and rate-limit error enrichment."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fixtures import REPO, BulkNestedSource, TempDBTest, add_bulk_prs, pr_file
from zaxbygraph.github import GitHubError
from zaxbygraph.graphql import GRAPHQL_CHILDREN_PAGE, GraphQLSource
from zaxbygraph.sync import sync_repo

#: One GraphQL page response covering aliases i1..i25 / p26..p40 (chunk 1).
_CHUNK1_ITEMS = 40
_CHUNK2_ITEMS = 10  # total 50 -> ceil(50/40) == 2 graphql calls


def _issue_node(number: int) -> dict:
    return {
        "databaseId": 10_000 + number,
        "comments": {
            "totalCount": 1,
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    "databaseId": 900_000 + number,
                    "body": f"comment {number}",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:01Z",
                    "url": f"https://github.com/{REPO}/issues/{number}#issuecomment-{number}",
                    "author": {"login": "alice", "url": "https://github.com/alice"},
                }
            ],
        },
    }


def _pull_node(number: int, *, comments_overflow: bool = False) -> dict:
    node = _issue_node(number)
    node.update(
        {
            "additions": 3,
            "deletions": 1,
            "changedFiles": 1,
            "commits": {"totalCount": 2},
            "mergedAt": None,
            "mergeCommit": {"oid": "abc123"},
            "baseRefName": "main",
            "headRefName": f"feat/{number}",
            "isDraft": False,
            "reviews": {
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {
                        "databaseId": 800_000 + number,
                        "state": "APPROVED",
                        "body": "ok",
                        "submittedAt": "2026-01-02T00:00:00Z",
                        "url": f"https://github.com/{REPO}/pull/{number}#review",
                        "author": {"login": "bob"},
                        "comments": {
                            "totalCount": 1,
                            "pageInfo": {"hasNextPage": number == 26},
                            "nodes": [
                                {
                                    "databaseId": 700_000 + number,
                                    "body": "inline",
                                    "createdAt": "2026-01-02T00:00:00Z",
                                    "updatedAt": "2026-01-02T00:00:01Z",
                                    "url": f"https://github.com/{REPO}/pull/{number}#discussion",
                                    "replyTo": {"databaseId": 900_000 + number},
                                    "author": {"login": "bob", "url": "https://github.com/bob"},
                                }
                            ],
                        },
                    }
                ],
            },
            "files": {
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {
                        "path": f"src/mod{number}.py",
                        "additions": 3,
                        "deletions": 1,
                        "changeType": "MODIFIED",
                    }
                ],
            },
        }
    )
    if number == 26:
        node["isDraft"] = True  # after update(): the literal above sets False
    if comments_overflow:
        node["comments"]["pageInfo"]["hasNextPage"] = True
    return node


def _graphql_payload(chunk_index: int) -> dict:
    repository: dict = {}
    for i in range(_CHUNK1_ITEMS * chunk_index, min(_CHUNK1_ITEMS * (chunk_index + 1), 50)):
        n = i + 1
        alias = "p" if n > 25 else "i"
        node = _pull_node(n) if n > 25 else _issue_node(n)
        repository[f"{alias}{n}"] = node
    return {
        "data": {
            "rateLimit": {"remaining": 4321, "resetAt": "2026-10-03T13:00:00Z"},
            "repository": repository,
        }
    }


class _FakeGraphQLHost:
    """A fake `gh` for GraphQLSource: the extensionless `api` script answers
    `graphql` calls from numbered response files and `rate_limit` from a
    fixed budget file; a mode file can force a failing exit."""

    def __init__(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cwd = Path(self._td.name)
        script = "\n".join([
            "import json, pathlib, sys",
            "d = pathlib.Path(__file__).parent",
            "argv = sys.argv[1:]",
            "with (d / 'calls.log').open('a', encoding='utf-8') as fh:",
            "    fh.write(json.dumps(argv) + '\\n')",
            "mode = (d / 'mode.txt').read_text(encoding='utf-8').strip()",
            "if mode == 'fail403':",
            "    print('gh: HTTP 403 Forbidden (SSO)', file=sys.stderr)",
            "    raise SystemExit(1)",
            "if argv[0] == 'graphql':",
            "    counter = d / 'graphql_calls.txt'",
            "    n = int(counter.read_text()) if counter.exists() else 0",
            "    counter.write_text(str(n + 1))",
            "    response = d / f'graphql_response_{n + 1}.json'",
            "    print(response.read_text(encoding='utf-8'))",
            "elif argv[0] == 'rate_limit':",
            "    print((d / 'rate_limit_response.json').read_text(encoding='utf-8'))",
            "else:",
            "    raise SystemExit(2)",
        ])
        (self.cwd / "api").write_text(script, encoding="utf-8")
        (self.cwd / "mode.txt").write_text("ok", encoding="utf-8")
        (self.cwd / "rate_limit_response.json").write_text(
            json.dumps({"resources": {"core": {"remaining": 0, "reset": 1799043600}}}),
            encoding="utf-8",
        )
        self._prev_cwd: Path | None = None

    def write_graphql_responses(self, payloads: list[dict]) -> None:
        for i, payload in enumerate(payloads, start=1):
            (self.cwd / f"graphql_response_{i}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

    def graphql_calls(self) -> list[list[str]]:
        log = self.cwd / "calls.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def __enter__(self) -> "_FakeGraphQLHost":
        self._prev_cwd = Path.cwd()
        os.chdir(self.cwd)
        return self

    def __exit__(self, *exc) -> None:
        os.chdir(self._prev_cwd)
        self._td.cleanup()

    def source(self) -> GraphQLSource:
        return GraphQLSource("acme", "forgegate", gh_bin=sys.executable)


def _listing_items(count: int) -> list[dict]:
    items: list[dict] = []
    for n in range(1, count + 1):
        item = {
            "id": 10_000 + n,
            "number": n,
            "title": f"item {n}",
            "state": "open",
            "user": {"login": "alice"},
            "labels": [],
            "comments": 1,
            "updated_at": f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}Z",
            "html_url": f"https://github.com/{REPO}/issues/{n}",
            "url": f"https://api.github.com/repos/{REPO}/issues/{n}",
        }
        if n > 25:
            item["pull_request"] = {"url": "x"}
        items.append(item)
    return items


class GraphQLChildrenTests(unittest.TestCase):
    def test_fetch_children_batches_and_maps_rest_shapes(self) -> None:
        host = _FakeGraphQLHost()
        host.write_graphql_responses([_graphql_payload(0), _graphql_payload(1)])
        with host:
            src = host.source()
            children = src.fetch_children(_listing_items(50))
            graphql_calls = [c for c in host.graphql_calls() if c[0] == "graphql"]
        self.assertEqual(
            len(graphql_calls),
            -(-50 // GRAPHQL_CHILDREN_PAGE),
            "one aliased query per children page",
        )
        self.assertEqual(src.rate_limit_remaining, 4321)
        self.assertEqual(src.rate_limit_reset_at, "2026-10-03T13:00:00Z")
        self.assertEqual(set(children), set(range(1, 51)))
        issue_children = children[1]
        self.assertEqual(issue_children["issue_comments"][0]["id"], 900_001)
        self.assertEqual(issue_children["issue_comments"][0]["user"]["login"], "alice")
        self.assertNotIn("pull", issue_children)
        pull_children = children[26]
        # the pull alias must surface PR issue comments AND the draft flag
        self.assertEqual(pull_children["issue_comments"][0]["id"], 900_026)
        self.assertIs(pull_children["pull"]["draft"], True)
        # a review whose comments page overflows flags the fallback
        self.assertIn("review_comments_incomplete", pull_children)
        self.assertEqual(pull_children["pull"]["changed_files"], 1)
        self.assertEqual(pull_children["pull"]["merge_commit_sha"], "abc123")
        self.assertEqual(pull_children["pull"]["base"], {"ref": "main"})
        review = pull_children["reviews"][0]
        self.assertEqual(review["state"], "APPROVED")
        review_comment = pull_children["review_comments"][0]
        self.assertEqual(review_comment["in_reply_to_id"], 900_026)
        graphql_file = pull_children["files"][0]
        self.assertEqual(graphql_file["filename"], "src/mod26.py")
        self.assertEqual(graphql_file["status"], "modified")
        self.assertEqual(graphql_file["changes"], 4)

    def test_pull_query_matches_the_real_graphql_schema(self) -> None:
        """Live-schema regression pin (issue #5 smoke): PullRequest has
        isDraft (not draft) and no top-level reviewComments connection -
        review comments ride under each review's comments connection."""
        from zaxbygraph.graphql import _PULL_FIELDS

        self.assertIn("isDraft", _PULL_FIELDS)
        self.assertNotIn("reviewComments", _PULL_FIELDS)

        # Build the actual query text for one PR alias and pin its shape.
        from zaxbygraph.graphql import _q

        alias_query = (
            "query { rateLimit { remaining resetAt } "
            f"repository(owner: {_q('acme')}, name: {_q('forgegate')}) {{ "
            f"p1: pullRequest(number: 1) {{{_PULL_FIELDS}}} }} }}"
        )
        self.assertIn("isDraft", alias_query)
        self.assertNotIn("reviewComments", alias_query)
        self.assertIn("reviews(first: 50)", alias_query)
        # PR issue comments must ride the same query (REST gets them from
        # /issues/{n}/comments; the pull alias must not drop them). TWO
        # comments connections must be requested: the PR's own issue
        # comments AND the per-review comments nested under reviews - a
        # single hit proves nothing because the nested one always matches.
        self.assertGreaterEqual(alias_query.count("comments(first: 100)"), 2)

    def test_overflowing_connection_is_flagged_for_rest_fallback(self) -> None:
        host = _FakeGraphQLHost()
        payload = _graphql_payload(0)
        node = payload["data"]["repository"]["i1"]
        node["comments"]["pageInfo"]["hasNextPage"] = True
        host.write_graphql_responses([payload])
        with host:
            src = host.source()
            children = src.fetch_children(_listing_items(1))
        self.assertTrue(children[1]["issue_comments_incomplete"])
        self.assertNotIn("issue_comments_incomplete", children.get(2, {}))

    def test_check_deleted_reports_numbers_missing_from_both_nodes(self) -> None:
        host = _FakeGraphQLHost()
        repository = {
            "i1": {"number": 1},
            "p1": None,
            "i2": None,
            "p2": None,
        }
        host.write_graphql_responses([{"data": {"repository": repository}}])
        with host:
            src = host.source()
            gone = src.check_deleted([1, 2, 3])
        self.assertEqual(gone, {2: "deleted", 3: "deleted"})

    def test_rate_limited_error_enriched_when_budget_exhausted(self) -> None:
        host = _FakeGraphQLHost()
        host.write_graphql_responses(
            [{"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}]
        )
        with host:
            src = host.source()
            with self.assertRaises(GitHubError) as ctx:
                src.check_deleted([1])
        exc = ctx.exception
        self.assertEqual(getattr(exc, "rate_limit_remaining", None), 0)
        self.assertEqual(
            getattr(exc, "rate_limit_reset", None),
            datetime.fromtimestamp(1799043600, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        )

    def test_error_with_budget_left_stays_unenriched(self) -> None:
        host = _FakeGraphQLHost()
        (host.cwd / "rate_limit_response.json").write_text(
            json.dumps({"resources": {"core": {"remaining": 4931, "reset": 1799043600}}}),
            encoding="utf-8",
        )
        host.write_graphql_responses(
            [{"errors": [{"type": "RATE_LIMITED", "message": "secondary limit"}]}]
        )
        with host:
            src = host.source()
            with self.assertRaises(GitHubError) as ctx:
                src.check_deleted([1])
        self.assertIsNone(getattr(ctx.exception, "rate_limit_remaining", None))

    def test_plain_403_fails_fast_without_enrichment(self) -> None:
        host = _FakeGraphQLHost()
        (host.cwd / "mode.txt").write_text("fail403", encoding="utf-8")
        with host:
            src = host.source()
            with self.assertRaises(GitHubError) as ctx:
                src.check_deleted([1])
        self.assertEqual(ctx.exception.status, 403)
        self.assertIsNone(getattr(ctx.exception, "rate_limit_remaining", None))


class SentQueryAndEdgeCaseTests(unittest.TestCase):
    """Pins from the PR-13 review: the query ACTUALLY SENT must carry the
    pinned schema shape (not just the constant), null sections must be
    omitted (so sync falls back to REST), NOT_FOUND-typed errors degrade to
    partial data, reviews>50 flags review comments incomplete, and a
    whole-chunk-null deletion probe refuses to mass-mark."""

    def test_sent_query_carries_the_pinned_shape(self) -> None:
        host = _FakeGraphQLHost()
        host.write_graphql_responses([_graphql_payload(0)])
        with host:
            src = host.source()
            src.fetch_children(_listing_items(26))  # 26 is a PR in the fixture
            calls = host.graphql_calls()
        sent = [a for a in calls if a[0] == "graphql" for a in [next(
            frag[6:] for frag in a if frag.startswith("query="))]][0]
        self.assertIn("isDraft", sent)
        self.assertGreaterEqual(sent.count("comments(first: 100)"), 2)

    def test_null_section_is_omitted_not_empty(self) -> None:
        host = _FakeGraphQLHost()
        payload = _graphql_payload(0)
        node = payload["data"]["repository"]["i1"]
        del node["comments"]  # permission mask / partial error shape
        host.write_graphql_responses([payload])
        with host:
            src = host.source()
            children = src.fetch_children(_listing_items(1))
        self.assertNotIn("issue_comments", children[1])

    def test_not_found_errors_degrade_to_partial_data(self) -> None:
        host = _FakeGraphQLHost()
        payload = _graphql_payload(0)
        payload["errors"] = [
            {"type": "NOT_FOUND",
             "message": "Could not resolve to an issue with the number of 99."}
        ]
        host.write_graphql_responses([payload])
        with host:
            src = host.source()
            children = src.fetch_children(_listing_items(1))
        self.assertIn(1, children)  # partial data survived; nulls -> REST fallback

    def test_non_not_found_errors_still_raise(self) -> None:
        host = _FakeGraphQLHost()
        payload = _graphql_payload(0)
        payload["errors"] = [{"type": "SOME_OTHER", "message": "boom"}]
        host.write_graphql_responses([payload])
        with host:
            src = host.source()
            with self.assertRaises(GitHubError):
                src.fetch_children(_listing_items(1))

    def test_mixed_not_found_and_rate_limited_raises_429(self) -> None:
        """PR-13 review pin: a payload mixing a per-alias NOT_FOUND with a
        RATE_LIMITED error must surface the rate-limit error (status 429) so
        sync's bounded sleep-retry sees it, instead of failing fast on the
        NOT_FOUND's None status."""
        host = _FakeGraphQLHost()
        payload = _graphql_payload(0)
        payload["errors"] = [
            {"type": "NOT_FOUND",
             "message": "Could not resolve to an issue with the number of 1."},
            {"type": "RATE_LIMITED", "message": "API rate limit exceeded"},
        ]
        host.write_graphql_responses([payload])
        with host:
            src = host.source()
            with self.assertRaises(GitHubError) as ctx:
                src.fetch_children(_listing_items(1))
        self.assertEqual(ctx.exception.status, 429)

    def test_not_found_on_requested_alias_omits_it_sibling_survives(self) -> None:
        """The degrade path with the error on a REQUESTED alias: the nulled
        item is omitted (per-item REST fallback) while its sibling survives."""
        host = _FakeGraphQLHost()
        payload = _graphql_payload(0)
        payload["data"]["repository"]["i1"] = None  # requested alias nulled
        payload["errors"] = [
            {"type": "NOT_FOUND",
             "message": "Could not resolve to an issue with the number of 1."}
        ]
        host.write_graphql_responses([payload])
        with host:
            src = host.source()
            children = src.fetch_children(_listing_items(2))
        self.assertNotIn(1, children)
        self.assertIn(2, children)

    def test_reviews_over_first_page_flags_review_comments_incomplete(self) -> None:
        host = _FakeGraphQLHost()
        payload = _graphql_payload(0)
        node = payload["data"]["repository"]["p26"]
        node["reviews"]["pageInfo"]["hasNextPage"] = True
        host.write_graphql_responses([payload])
        with host:
            src = host.source()
            children = src.fetch_children(_listing_items(26))
        self.assertIn("reviews_incomplete", children[26])
        self.assertIn("review_comments_incomplete", children[26])

    def test_totalcount_overflow_flags_incomplete_without_hasnextpage(self) -> None:
        host = _FakeGraphQLHost()
        payload = _graphql_payload(0)
        node = payload["data"]["repository"]["i1"]
        node["comments"]["totalCount"] = 5  # hasNextPage stays False
        host.write_graphql_responses([payload])
        with host:
            src = host.source()
            children = src.fetch_children(_listing_items(1))
        self.assertIn("issue_comments_incomplete", children[1])

    def test_message_based_rate_limit_detection(self) -> None:
        host = _FakeGraphQLHost()
        payload = {"errors": [{"message": "you have exceeded the rate limit"}]}
        host.write_graphql_responses([payload])
        with host:
            src = host.source()
            with self.assertRaises(GitHubError) as ctx:
                src.check_deleted([1])
        self.assertEqual(ctx.exception.status, 429)

    def test_all_null_chunk_refuses_mass_marking(self) -> None:
        host = _FakeGraphQLHost()
        payload = {"data": {"repository": {"i1": None, "p1": None}}}
        host.write_graphql_responses([payload])
        with host:
            src = host.source()
            with self.assertRaises(GitHubError):
                src.check_deleted([1])


class IncludePatchesForcesRestTests(TempDBTest):
    """--include-patches must come from REST pulls/{n}/files even when the
    bulk payload carried the files (GraphQL has no patch field)."""

    def test_include_patches_fetches_files_over_rest(self) -> None:
        self.src = BulkNestedSource()
        add_bulk_prs(self.src, 5)
        result = self.sync(include_patches=True)
        self.assertEqual(result["ingested"], 5)
        self.assertEqual(self.src.files_fallback_numbers, [1, 2, 3, 4, 5])
        stored = self.count(
            "SELECT COUNT(*) FROM pr_files WHERE patch IS NOT NULL"
        )
        self.assertEqual(stored, 10)
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM pr_files"),
            10,
        )

    def test_without_patches_no_files_fallback(self) -> None:
        self.src = BulkNestedSource()
        add_bulk_prs(self.src, 5)
        self.sync()
        self.assertEqual(self.src.files_fallback_numbers, [])


if __name__ == "__main__":
    unittest.main()
