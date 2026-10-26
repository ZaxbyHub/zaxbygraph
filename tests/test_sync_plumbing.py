"""Issue #23 acceptance checks: sync survives schema drift, records section
completeness, refreshes exact items, and writes a replayable sidecar.

Every check here is authored arm's-length against the issue's acceptance
criteria, not against any implementation. RED at base means the check fails
or errors for the SEMANTIC reason named in its docstring; GREEN after the
fix means the same assertions hold. The module imports only symbols that
exist at base, so NEW-SURFACE gaps surface inside the test methods
(AttributeError / TypeError / ModuleNotFoundError), never at import.

Offline discipline: no network, no live `gh`. GraphQL responses are injected
through the same seam tests/test_graphql.py uses - a fake extensionless
`api` script executed as `sys.executable api ...` with the working directory
swapped into a temp dir - extended here to be transport-agnostic: the fake
records argv AND any `--input -` stdin body, answers introspection probes
from a schema.json file (so a capability probe can be answered without
freezing its query text), and answers REST paths from numbered files so a
whole sync can run against the real GraphQLSource end to end.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fixtures import (
    REPO,
    FakeGitHubSource,
    TempDBTest,
    comment,
    issue,
    pr_file,
    pull,
    ts,
)
from fixtures_bulk import PageShapedBulkSource, add_page_prs
from zaxbygraph.db import connect, init_schema
from zaxbygraph.github import GitHubError
from zaxbygraph.graphql import GraphQLSource
from zaxbygraph.store import ingest_item as real_ingest_item
from zaxbygraph.sync import sync_repo

#: Fields the fake introspection schema reports as present when a check needs
#: the probe to change nothing (fail-open shape: every selected field exists).
_ALL_PULL_TYPE_FIELDS = [
    "databaseId",
    "comments",
    "additions",
    "deletions",
    "changedFiles",
    "commits",
    "mergedAt",
    "mergeCommit",
    "baseRefName",
    "headRefName",
    "isDraft",
    "mergedBy",
    "closingIssuesReferences",
    "reviews",
    "files",
    "timelineItems",
]
_FULL_SCHEMA = {
    "Issue": ["databaseId", "comments", "timelineItems"],
    "PullRequest": _ALL_PULL_TYPE_FIELDS,
}

#: A fake `gh` binary. Invoked as `<python> api graphql ...` - the script's
#: own filename plays the `gh api` prefix, so the child's sys.argv[1:]
#: begins at the API path exactly like `gh api graphql ...` arguments do and
#: GraphQLSource(gh_bin=sys.executable) talks to it. Each call is logged as
#: one JSON line carrying the argv, the stdin body (when `--input` was
#: used), the extracted query text, and whether the query looked like an
#: introspection probe. Answers: `graphql` pages from numbered files (an
#: introspection-shaped query is instead answered from schema.json), and any
#: `repos/...` REST path from numbered rest files.
_FAKE_API_SCRIPT = r'''import json, pathlib, re, sys

d = pathlib.Path(__file__).parent
argv = sys.argv[1:]
if argv and argv[0] == "--version":
    print("gh version 9.9.9 (acceptance fake)")
    raise SystemExit(0)
if not argv:
    raise SystemExit(2)
stdin_text = ""
if "--input" in argv:
    i = argv.index("--input")
    where = argv[i + 1] if i + 1 < len(argv) else "-"
    if where == "-":
        stdin_text = sys.stdin.read()
    else:
        stdin_text = pathlib.Path(where).read_text(encoding="utf-8")
sub = argv[0]
query = ""
if sub == "graphql":
    if stdin_text:
        try:
            body = json.loads(stdin_text)
            query = body.get("query", "") if isinstance(body, dict) else stdin_text
        except ValueError:
            query = stdin_text
    else:
        for frag in argv:
            if frag.startswith("query="):
                query = frag[len("query="):]
probe = sub == "graphql" and ("__type(" in query or "IntrospectionQuery" in query or "__schema" in query)
with (d / "calls.log").open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": argv, "stdin": stdin_text, "query": query, "probe": probe}) + "\n")
if sub == "graphql":
    if probe:
        schema = {}
        sp = d / "schema.json"
        if sp.exists():
            schema = json.loads(sp.read_text(encoding="utf-8"))
        data = {}
        for m in re.finditer(r'(\w+):\s*__type\(name:\s*"(\w+)"\)', query):
            data[m.group(1)] = {"fields": [{"name": n} for n in schema.get(m.group(2), [])]}
        if not data:
            m2 = re.search(r'__type\(name:\s*"(\w+)"\)', query)
            if m2:
                data["__type"] = {"fields": [{"name": n} for n in schema.get(m2.group(1), [])]}
        if "__schema" in query:
            data["__schema"] = {"types": [
                {"name": t, "fields": [{"name": n} for n in ns]}
                for t, ns in schema.items()
            ]}
        print(json.dumps({"data": data}))
    else:
        counter = d / "graphql_calls.txt"
        n = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(n + 1))
        print((d / ("graphql_response_%d.json" % (n + 1))).read_text(encoding="utf-8"))
elif sub == "rate_limit":
    print((d / "rate_limit_response.json").read_text(encoding="utf-8"))
elif sub.startswith("repos/"):
    counter = d / "rest_calls.txt"
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    print((d / ("rest_response_%d.json" % (n + 1))).read_text(encoding="utf-8"))
else:
    raise SystemExit(2)
'''


class _FakeGhHost:
    """The test_graphql.py seam, transport-agnostic: fake `gh` in a temp
    directory, cwd swapped while active, GraphQLSource built over
    sys.executable."""

    def __init__(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cwd = Path(self._td.name)
        (self.cwd / "api").write_text(_FAKE_API_SCRIPT, encoding="utf-8")
        (self.cwd / "rate_limit_response.json").write_text(
            json.dumps({"resources": {"core": {"remaining": 4931, "reset": 1799043600}}}),
            encoding="utf-8",
        )
        self._prev_cwd = None  # the cwd to restore on exit

    def write_schema(self, schema: dict) -> None:
        (self.cwd / "schema.json").write_text(json.dumps(schema), encoding="utf-8")

    def write_graphql_responses(self, payloads: list[dict]) -> None:
        for i, payload in enumerate(payloads, start=1):
            (self.cwd / f"graphql_response_{i}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

    def write_rest_responses(self, payloads: list) -> None:
        for i, payload in enumerate(payloads, start=1):
            (self.cwd / f"rest_response_{i}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

    def calls(self) -> list[dict]:
        log = self.cwd / "calls.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def graphql_calls(self) -> list[dict]:
        return [c for c in self.calls() if c["argv"][:1] == ["graphql"]]

    def rest_paths(self) -> list[str]:
        return [c["argv"][0] for c in self.calls() if c["argv"][0].startswith("repos/")]

    def __enter__(self) -> "_FakeGhHost":
        self._prev_cwd = Path.cwd()
        import os

        os.chdir(self.cwd)
        return self

    def __exit__(self, *exc) -> None:
        import os

        os.chdir(self._prev_cwd)
        self._td.cleanup()

    def source(self) -> GraphQLSource:
        return GraphQLSource("acme", "forgegate", gh_bin=sys.executable)


def _issue_node(number: int) -> dict:
    """One GraphQL issue node with a complete single-comment connection."""
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


def _page_payload(numbers: list[int]) -> dict:
    return {
        "data": {
            "rateLimit": {"remaining": 4321, "resetAt": "2026-10-03T13:00:00Z"},
            "repository": {f"i{n}": _issue_node(n) for n in numbers},
        }
    }


def _listing_item(number: int) -> dict:
    return issue(number, title=f"item {number}", comments=1)


class CapabilityProbeTests(unittest.TestCase):
    """AC1: a cached capability probe (introspection of Issue/PullRequest
    fields) omits schema-absent fields from the built page query."""

    def test_probe_omits_absent_fields(self) -> None:
        """RED at base: GraphQLSource has no capability probe, so
        probe_capabilities raises AttributeError. GREEN after the fix: the
        probe introspects the Issue/PullRequest types once (cached - a
        second probe costs no extra call) and the page query built afterwards
        omits a field the schema no longer has while still requesting the
        fields it does."""
        host = _FakeGhHost()
        host.write_schema(
            {
                "Issue": ["databaseId", "comments"],  # timelineItems is gone
                "PullRequest": _ALL_PULL_TYPE_FIELDS,
            }
        )
        host.write_graphql_responses([_page_payload([1])])
        with host:
            src = host.source()
            src.probe_capabilities()
            src.probe_capabilities()  # cached: no second introspection
            children = src.fetch_children([_listing_item(1)])
            calls = host.calls()
        self.assertIn(1, children)
        probes = [c for c in calls if c["probe"]]
        # Derive pages from the captured call list: the host's temp directory
        # is removed on __exit__, so a post-exit re-read observes nothing
        # (CHECK_WRONG amend, issue #23 trace).
        pages = [c for c in calls if c["argv"][:1] == ["graphql"] and not c["probe"]]
        self.assertEqual(
            len(probes),
            1,
            "the capability probe must be cached, one introspection per source",
        )
        self.assertGreaterEqual(len(pages), 1, "a page query must still be sent")
        for page in pages:
            self.assertNotIn(
                "timelineItems",
                page["query"],
                "a field the probe reported absent must be omitted from the page query",
            )
            self.assertIn(
                "comments(first: 100)",
                page["query"],
                "a field the probe reported present must stay in the page query",
            )


class FieldErrorToleranceTests(TempDBTest):
    """AC2: a runtime per-field GraphQL error with partial data drops only
    that section for the affected items; the sync completes."""

    def test_field_error_drops_section_not_sync(self) -> None:
        """RED at base: any non-NOT_FOUND GraphQL error (here a FORBIDDEN
        with a field path and partial data) aborts the whole sync with
        GitHubError wrapped in SyncError. GREEN after the fix: the error's
        path scopes it to item 2's comments section, that section degrades
        to the per-item REST fallback for item 2 only, and the sync
        completes cleanly with both items stored."""
        host = _FakeGhHost()
        host.write_schema(_FULL_SCHEMA)
        host.write_graphql_responses(
            [
                {
                    "data": {
                        "rateLimit": {
                            "remaining": 4321,
                            "resetAt": "2026-10-03T13:00:00Z",
                        },
                        "repository": {
                            "i1": _issue_node(1),
                            "i2": {"databaseId": 10_002, "comments": None},
                        },
                    },
                    "errors": [
                        {
                            "type": "FORBIDDEN",
                            "message": "mocked per-field FORBIDDEN on i2 comments",
                            "path": ["repository", "i2", "comments"],
                        }
                    ],
                }
            ]
        )
        host.write_rest_responses(
            [
                # the issues listing page (state=all&sort=updated, one page)
                [
                    issue(1, title="one", comments=1, updated_at=ts(10)),
                    issue(2, title="two", comments=1, updated_at=ts(20)),
                ],
                # REST fallback for item 2's dropped comments section
                [comment(50_002, "rest fallback note")],
                # releases
                [],
            ]
        )
        with host:
            src = host.source()
            result = sync_repo(self.conn, src, REPO)
            rest_paths = host.rest_paths()
        self.assertEqual(result["ingested"], 2, "a per-field error must not abort the sync")
        self.assertIsNone(result["last_error"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 2)
        numbers = sorted(r[0] for r in self.conn.execute("SELECT DISTINCT number FROM comments"))
        self.assertEqual(numbers, [1, 2], "both items must end with their comments stored")
        self.assertEqual(
            sum(1 for p in rest_paths if p.startswith("repos/acme/forgegate/issues/2/comments")),
            1,
            "the dropped section must degrade to the per-item REST fallback for the affected item",
        )
        self.assertFalse(
            any(p.startswith("repos/acme/forgegate/issues/1/comments") for p in rest_paths),
            "the unaffected item must keep its GraphQL-provided section",
        )


class StdinTransportTests(unittest.TestCase):
    """AC3: the GraphQL query travels on stdin, no gh argv element over 1 KiB."""

    def test_query_travels_on_stdin(self) -> None:
        """RED at base: the query rides argv as -f query=<full query>, an
        argv element far beyond 1024 chars. GREEN after the fix: every gh
        api graphql invocation reads its request body from stdin via
        --input -, the query text arrives on that body, and no single argv
        element exceeds 1024 chars."""
        host = _FakeGhHost()
        host.write_schema(_FULL_SCHEMA)
        host.write_graphql_responses([_page_payload([1, 2])])
        with host:
            src = host.source()
            src.fetch_children([_listing_item(1), _listing_item(2)])
            calls = host.calls()
        graphql_calls = [c for c in calls if c["argv"][:1] == ["graphql"]]
        self.assertGreaterEqual(len(graphql_calls), 1, "a graphql call must have happened")
        for call in graphql_calls:
            argv = call["argv"]
            carried = [
                a for a in argv[1:] if a.startswith("query=") or "query {" in a
            ]
            self.assertFalse(
                carried,
                "the graphql query must travel on stdin via gh api graphql --input -, not in argv",
            )
            self.assertIn(
                "--input",
                argv,
                "the graphql invocation must read its request body from stdin",
            )
            self.assertIn(
                "repository(owner:",
                call["stdin"],
                "the query text itself must arrive on the stdin body",
            )
            self.assertLessEqual(
                max(len(a) for a in argv),
                1024,
                "no single gh argv element may exceed 1024 chars",
            )


class _CompletenessSource(FakeGitHubSource):
    """AC4 fixture: number 1 (PR) is answered by the bulk path with a
    complete issue_comments section, a files section truncated at the page
    cap (files_incomplete), and no reviews or review_comments answer at all;
    number 2 is answered nowhere and its per-item REST child fetch 404s."""

    def list_issues(self, since):  # since: ISO-Z watermark or None
        items = sorted(self.issues.values(), key=lambda r: (r["updated_at"], r["number"]))
        for rec in items:
            if since is not None and rec["updated_at"] < since:
                continue
            yield deepcopy(rec)

    def fetch_children(self, items) -> dict:
        out: dict[int, dict] = {}
        for raw in items:
            if isinstance(raw, dict) and int(raw["number"]) == 1:
                out[1] = {
                    "pull": deepcopy(self.pulls[1]),
                    "issue_comments": deepcopy(self.issue_comments.get(1, [])),
                    "files": deepcopy(self.files.get(1, []))[:1],
                    "files_incomplete": True,
                }
        return out

    def list_issue_comments(self, number: int) -> list:
        if number == 2:
            raise GitHubError("HTTP 404", status=404)
        return super().list_issue_comments(number)


class ChildrenCompletenessTests(TempDBTest):
    """AC4: ingest_item receives a per-section children_complete map, False
    for the 404 path, unanswered sections, and sections truncated at a cap."""

    def test_children_complete_flags(self) -> None:
        """RED at base: ingest_item takes no children_complete parameter, so
        the direct store call raises TypeError. GREEN after the fix: the
        store boundary accepts the map and the sync path delivers truthful
        values - True for the section the bulk payload answered complete,
        False for the sections it did not answer and the one truncated at a
        cap, and all-False on the 404 path."""
        # 1) the store boundary itself must accept the parameter
        probe_conn = connect(Path(self._td.name) / "probe.db")
        init_schema(probe_conn)
        try:
            probe_conn.execute("BEGIN IMMEDIATE")
            real_ingest_item(
                probe_conn,
                REPO,
                issue(9, title="probe", updated_at=ts(9)),
                pull_raw=None,
                issue_comments=[],
                review_comments=[],
                reviews=[],
                files=[],
                include_patches=False,
                children_complete={"issue_comments": False},
            )
            probe_conn.commit()
        finally:
            probe_conn.close()

        # 2) the sync path must deliver truthful per-section values
        src = _CompletenessSource()
        src.add_pr(
            issue(1, title="pr one", comments=1, kind="pr", updated_at=ts(10)),
            pull(1, changed_files=2),
            files=[pr_file("src/one/a.py"), pr_file("src/one/b.py")],
        )
        src.issue_comments[1].append(comment(50_011, "bulk note"))
        src.issues[1]["comments"] = 1
        src.add_issue(issue(2, title="gone", comments=1, updated_at=ts(20)))

        captured: dict[int, dict] = {}

        def fake_ingest(conn, repo, list_raw, **kw):
            captured[int(list_raw["number"])] = kw.get("children_complete")
            return real_ingest_item(conn, repo, list_raw, **kw)

        with patch("zaxbygraph.sync.ingest_item", side_effect=fake_ingest):
            result = sync_repo(self.conn, src, REPO)
        self.assertEqual(result["ingested"], 2)

        cc1 = captured.get(1)
        self.assertIsNotNone(cc1, "ingest_item must receive a children_complete map")
        for section in ("issue_comments", "review_comments", "reviews", "files"):
            self.assertIn(section, cc1, f"the map must name the {section} section")
        self.assertTrue(
            cc1["issue_comments"],
            "a section the bulk payload answered complete must be True",
        )
        self.assertFalse(
            cc1["reviews"],
            "a section the bulk source did not answer must be False",
        )
        self.assertFalse(
            cc1["review_comments"],
            "a section the bulk source did not answer must be False",
        )
        self.assertFalse(
            cc1["files"],
            "a section truncated at a cap must be False",
        )

        cc2 = captured.get(2)
        self.assertIsNotNone(cc2, "the 404 path must still deliver a children_complete map")
        self.assertTrue(cc2, "the 404 map must name its sections")
        self.assertTrue(
            all(not v for v in cc2.values()),
            "every section on the 404 path must be False",
        )


class _NumberedBulkSource(PageShapedBulkSource):
    """AC5 fixture: the real page-shaped bulk source, recording which item
    numbers each fetch_children bulk call carried."""

    def __init__(self) -> None:
        super().__init__()
        self.children_number_batches: list[list[int]] = []

    def fetch_children(self, items) -> dict:
        numbers = sorted(
            int(r["number"]) for r in items if isinstance(r, dict) and "number" in r
        )
        self.children_number_batches.append(numbers)
        return super().fetch_children(items)


class NumbersResyncTests(TempDBTest):
    """AC5: sync --numbers re-ingests exactly the named items through the
    bulk children path and leaves the watermark alone."""

    def test_sync_numbers_keeps_watermark(self) -> None:
        """RED at base: cmd_sync ignores the numbers attribute, so the whole
        listing is re-ingested and ingest_item advances the watermark past
        items the listing never covered. GREEN after the fix: exactly the
        requested numbers are re-ingested through fetch_children and
        sync_state.issues_since is unchanged."""
        from zaxbygraph.cli import cmd_sync

        src = _NumberedBulkSource()
        for n in range(1, 5):
            src.add_issue(issue(n, title=f"item {n}", updated_at=ts(n * 100)))
        self.conn.execute(
            "INSERT INTO sync_state(repo, issues_since) VALUES (?, ?)",
            (REPO, "2026-01-01T00:00:00Z"),
        )
        self.conn.commit()
        args = argparse.Namespace(
            repo=REPO,
            db=str(self.db_path),
            force=False,
            include_patches=False,
            jsonl=None,
            jsonl_flag=False,
            format="json",
            source="graphql",
            wait=False,
            numbers="2,3",
        )
        with patch("zaxbygraph.cli.GhApiSource", return_value=src):
            code = cmd_sync(args)
        self.assertEqual(code, 0)
        ingested_numbers = [
            r[0] for r in self.conn.execute("SELECT number FROM items ORDER BY number")
        ]
        self.assertEqual(
            ingested_numbers,
            [2, 3],
            "sync --numbers must re-ingest exactly the requested items",
        )
        since = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertEqual(
            since,
            "2026-01-01T00:00:00Z",
            "sync --numbers must leave the watermark unchanged",
        )
        self.assertTrue(
            any(b == [2, 3] for b in src.children_number_batches),
            "sync --numbers must fetch children through the bulk children path",
        )


class _BundleSource(PageShapedBulkSource):
    """AC6/AC7 fixture: the page-shaped bulk source plus a one-event
    timeline per item, so bundles carry every child section and timeline."""

    def fetch_children(self, items) -> dict:
        out = super().fetch_children(items)
        for number, child in out.items():
            child["timeline"] = [
                {
                    "type": "closed",
                    "created_at": ts(900),
                    "actor_login": "amy",
                    "commit_id": f"abc123{number}",
                    "closer_type": "commit",
                    "closer_number": None,
                }
            ]
        return out


class SidecarBundleTests(TempDBTest):
    """AC6: the --jsonl sidecar writes one replayable item_bundle per item."""

    def test_sidecar_item_bundles(self) -> None:
        """RED at base: the sidecar writes per-payload records whose
        resource is item, issue_comment, review_comment, review, pr_file or
        timeline_event - no item_bundle records exist and the child payloads
        carry no owning number. GREEN after the fix: one item_bundle record
        per ingested item carries number, item, pull, every child section
        and the timeline, and the number and item.updated_at pair
        distinguishes every record."""
        src = _BundleSource()
        add_page_prs(src, 3)
        dest = Path(self._td.name) / "sidecar"
        result = sync_repo(self.conn, src, REPO, jsonl_path=dest)
        self.assertEqual(result["ingested"], 3)
        records = [
            json.loads(line)
            for line in (dest / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        bundles = [r for r in records if r.get("resource") == "item_bundle"]
        self.assertEqual(
            len(bundles),
            3,
            "the sidecar must write one item_bundle record per ingested item",
        )
        keys = set()
        for bundle in bundles:
            payload = bundle["payload"]
            number = payload["number"]
            self.assertEqual(
                payload["item"]["number"],
                number,
                "the bundle item section must belong to the bundled number",
            )
            self.assertEqual(
                payload["pull"]["number"],
                number,
                "the bundle pull section must belong to the bundled number",
            )
            for section in (
                "issue_comments",
                "review_comments",
                "reviews",
                "files",
                "timeline",
            ):
                self.assertIn(
                    section, payload, "the bundle must carry every child section and the timeline"
                )
            self.assertEqual(len(payload["issue_comments"]), 2)
            self.assertEqual(len(payload["reviews"]), 1)
            self.assertEqual(len(payload["review_comments"]), 1)
            self.assertEqual(len(payload["files"]), 2)
            self.assertEqual(len(payload["timeline"]), 1)
            keys.add((number, payload["item"]["updated_at"]))
        self.assertEqual(
            len(keys),
            3,
            "bundles must be deduplicable by number and item.updated_at",
        )


#: Content columns compared by the replay round trip (surrogate rowid
#: columns are excluded; identity columns - items.id, reviews.id, comments
#: .github_id - are included).
_ROUND_TRIP_TABLES = [
    (
        "items",
        "id, repo, number, kind, node_id, title, body, labels_text, state, "
        "state_reason, author, created_at, updated_at, closed_at, merged_at, "
        "merge_commit, draft, locked, base_ref, head_ref, additions, "
        "deletions, changed_files, commits, html_url, api_url, raw_json",
        "number",
    ),
    ("labels", "repo, number, name, color", "number, name"),
    (
        "comments",
        "github_id, repo, number, kind, author, created_at, updated_at, "
        "body, html_url, in_reply_to, raw_json",
        "kind, github_id",
    ),
    (
        "reviews",
        "id, repo, number, author, state, submitted_at, body, html_url, raw_json",
        "id",
    ),
    (
        "pr_files",
        "repo, number, path, status, additions, deletions, changes, sha, patch",
        "number, path",
    ),
    (
        "edges",
        "repo, src_type, src_id, rel, dst_type, dst_id, confidence, evidence, source",
        "src_id, rel, dst_id, source",
    ),
]


class SidecarReplayTests(TempDBTest):
    """AC7: a recorded sidecar replays row for row through
    JsonlReplaySource."""

    @staticmethod
    def _rows(conn, table: str, cols: str, order: str) -> list[tuple]:
        return [tuple(r) for r in conn.execute(f"SELECT {cols} FROM {table} ORDER BY {order}")]

    def test_sidecar_replay_round_trip(self) -> None:
        """RED at base: no replay source exists, so importing
        JsonlReplaySource from tests fails. GREEN after the fix: a recorded
        sidecar replayed through JsonlReplaySource in updated_at and number
        order into a fresh store reproduces the original store's items,
        children and edges row for row."""
        src = _BundleSource()
        add_page_prs(src, 3)
        sidecar = Path(self._td.name) / "sidecar"
        original = sync_repo(self.conn, src, REPO, jsonl_path=sidecar)
        self.assertEqual(original["ingested"], 3)

        from replay import JsonlReplaySource  # tests-local, ships with the fix

        replay_conn = connect(Path(self._td.name) / "replay.db")
        init_schema(replay_conn)
        try:
            replay_src = JsonlReplaySource(sidecar / "events.jsonl")
            replayed = sync_repo(replay_conn, replay_src, REPO)
            self.assertEqual(replayed["ingested"], 3)
            for table, cols, order in _ROUND_TRIP_TABLES:
                self.assertEqual(
                    self._rows(self.conn, table, cols, order),
                    self._rows(replay_conn, table, cols, order),
                    f"replay must reproduce {table} row for row",
                )
        finally:
            replay_conn.close()


if __name__ == "__main__":
    unittest.main()
