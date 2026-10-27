"""Supporting checks for issue #27 (schema v10, GitHub-reported links).

Complements the frozen acceptance checks (tests/test_link_stream.py): the
degradation/persistence surfaces the plan promises that the frozen set does
not pin — REST-preservation, the ownership invariant, retained-commit
re-derivation, evidence simultaneity, boolean/list overflow normalization,
capability-enum round trips, mapping shapes, migration, and the consolidation
drift guard. Authored by the implementer.
"""

from __future__ import annotations

import json
import sqlite3
import unittest

from fixtures import REPO, TempDBTest, issue, pull, ts
from zaxbygraph.db import CURRENT_USER_VERSION, init_schema
from zaxbygraph.extract import (
    RELATIONS,
    edges_from_item,
    edges_from_pr_state,
)
from zaxbygraph.graphql import GraphQLSource, _timeline_event
from zaxbygraph.store import delete_owned_edges, ingest_item
from zaxbygraph.sync import _resolve_children


class _BulkSource:
    """Minimal bulk fake: list_issues yields one page, fetch_children returns
    the v10 children dicts the tests set per item."""

    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self.children: dict[int, dict] = {}

    def add_issue(self, rec: dict) -> None:
        self.issues[int(rec["number"])] = rec

    def list_issues(self, since: str | None):
        page = [
            dict(rec)
            for rec in sorted(
                self.issues.values(), key=lambda r: (r["updated_at"], r["number"])
            )
            if since is None or rec["updated_at"] >= since
        ]
        if page:
            yield page

    def fetch_children(self, items) -> dict[int, dict]:
        return {
            int(raw["number"]): dict(self.children.get(int(raw["number"]), {}))
            for raw in items
            if isinstance(raw, dict) and "number" in raw
        }

    def get_pull(self, number: int) -> dict:
        return {}

    def list_issue_comments(self, number: int):
        return []

    def list_review_comments(self, number: int):
        return []

    def list_reviews(self, number: int):
        return []

    def list_pr_files(self, number: int):
        return []

    def list_releases(self):
        return []


def _bump(rec: dict, n: int) -> dict:
    out = dict(rec)
    out["updated_at"] = ts(n)
    return out


class RestPreservationTests(TempDBTest):
    def test_rest_reingest_preserves_events_commits_and_derived_edges(self) -> None:
        """A plain-REST re-ingest (children None everywhere) must leave
        stored item_events, pr_commits rows, link edges AND the commit-derived
        closes_keyword edges untouched — presence-None is never deletion
        evidence. Killed by: gating the writers on the completeness map
        instead of presence (the round-2 critic's erase path)."""
        src = _BulkSource()
        src.add_issue(issue(3, title="one", updated_at=ts(10)))
        src.children[3] = {
            "timeline": [
                {"type": "reopened", "created_at": "2026-01-02T00:00:00Z"},
            ],
            "links": {"blocked_by": [{"number": 1, "repo": None}]},
            "pr_commits": [{"sha": "abc123", "message": "fixes #1"}],
        }
        self.sync_full(src)

        self.assertEqual(
            self.count("SELECT COUNT(*) FROM item_events WHERE number = 3"), 1
        )
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM edges WHERE rel = 'blocked_by'"), 1
        )
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM pr_commits WHERE number = 3"), 1
        )
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM edges WHERE rel = 'closes_keyword'"
                " AND evidence LIKE 'commit %'"
            ),
            1,
            "COMMIT-EDGE-MISSING: commit message did not derive a keyword edge",
        )

        # REST re-ingest: no bulk context at all.
        src2 = _BulkSource()
        src2.add_issue(issue(3, title="one", updated_at=ts(20)))
        self.sync_full(src2)

        self.assertEqual(
            self.count("SELECT COUNT(*) FROM item_events WHERE number = 3"),
            1,
            "REST-ERASED-EVENTS: the plain-REST re-ingest deleted item_events",
        )
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM edges WHERE rel = 'blocked_by'"),
            1,
            "REST-ERASED-LINKS: the plain-REST re-ingest deleted link edges",
        )
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM pr_commits WHERE number = 3"),
            1,
            "REST-ERASED-COMMITS: the plain-REST re-ingest deleted pr_commits",
        )
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM edges WHERE rel = 'closes_keyword'"
                " AND evidence LIKE 'commit %'"
            ),
            1,
            "REST-ERASED-COMMIT-EDGES: keyword edges must re-derive from the"
            " retained rows, not vanish",
        )

    def sync_full(self, src: _BulkSource) -> None:
        from zaxbygraph.sync import sync_repo

        sync_repo(self.conn, src, REPO)


class OwnershipTests(TempDBTest):
    def test_issue_ingest_never_deletes_pr_owned_edges(self) -> None:
        """The reverse branch pass is INSERT-ONLY: re-ingesting the target
        issue re-derives the same branch_ref and deletes nothing owned by the
        PR. Killed by: any cross-owner DELETE in the issue ingest path."""
        src = _BulkSource()
        src.add_issue(issue(2, title="target", updated_at=ts(10)))
        self.sync_full(src)
        src_pr = _BulkSource()
        src_pr.add_issue(
            _pr(5, head_ref="issue-2-fix", updated_at=ts(11))
        )
        self.sync_full(src_pr)
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM edges WHERE rel = 'branch_ref'"),
            1,
            "BRANCH-MISSING: the forward pass should have emitted 5->2",
        )
        # Re-ingest the ISSUE (its own payload changed): the PR's edge stays.
        src_issue = _BulkSource()
        src_issue.add_issue(issue(2, title="target renamed", updated_at=ts(30)))
        self.sync_full(src_issue)
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM edges WHERE rel = 'branch_ref'"),
            1,
            "OWNERSHIP-VIOLATION: an issue ingest deleted a PR-owned edge",
        )
        # Re-ingest an UNRELATED issue: a reverse pass that deletes
        # non-matching candidates' PR-owned edges dies here (the deletion
        # mutant MUT10).
        src_other = _BulkSource()
        src_other.add_issue(issue(9, title="unrelated", updated_at=ts(40)))
        self.sync_full(src_other)
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM edges WHERE rel = 'branch_ref'"),
            1,
            "OWNERSHIP-VIOLATION-UNRELATED: an unrelated issue ingest touched"
            " the PR's branch_ref edge",
        )

    def sync_full(self, src: _BulkSource) -> None:
        from zaxbygraph.sync import sync_repo

        sync_repo(self.conn, src, REPO)


def _pr(number: int, *, head_ref: str, updated_at: str) -> dict:
    rec = issue(number, title="pr", kind="pr", updated_at=updated_at)
    rec["base"] = {"ref": "main"}
    rec["head"] = {"ref": head_ref}
    return rec


class CompletenessBoundaryTests(unittest.TestCase):
    def _resolved(self, children: dict) -> tuple[dict[str, bool], object]:
        src = _BulkSource()
        list_raw = issue(1, title="t", updated_at=ts(0))
        children = dict(children)
        children.setdefault("issue_comments", [])
        children.setdefault("review_comments", [])
        children.setdefault("reviews", [])
        children.setdefault("files", [])
        src.issues[1] = list_raw
        src.children[1] = children
        pages = list(src.list_issues(None))
        provided = src.fetch_children(pages[0])[1]
        result = _resolve_children(src, pages[0][0], "issue", False, provided=provided)
        return result[7], result[9]

    def test_boolean_overflow_means_all_connections(self) -> None:
        """The legacy boolean links_incomplete form (recorded bundles,
        fixtures) normalizes to every connection overflowing."""
        complete, links_incomplete = self._resolved(
            {
                "links": {"blocked_by": [{"number": 2, "repo": None}]},
                "links_incomplete": True,
            }
        )
        self.assertFalse(complete["links_blocked_by"])
        self.assertIsNone(complete.get("nothing"))
        # The other three connections were not carried at all: absent, and
        # the overflow normalization is the transport shape's business.
        self.assertEqual(links_incomplete, True)

    def test_list_overflow_is_per_connection(self) -> None:
        """A named-overflow list marks exactly that connection incomplete;
        carried complete connections (open_prs) read complete and may replace."""
        complete, links_incomplete = self._resolved(
            {
                "links": {
                    "blocked_by": [{"number": 2, "repo": None}],
                    "open_prs": [{"number": 9, "repo": None}],
                },
                "links_incomplete": ["blocked_by"],
            }
        )
        self.assertFalse(complete["links_blocked_by"])
        self.assertTrue(complete["links_open_prs"])
        self.assertEqual(links_incomplete, ["blocked_by"])


class EnumCapabilityTests(unittest.TestCase):
    def test_populated_enum_round_trip(self) -> None:
        source = GraphQLSource("o", "r")
        source.attach_capabilities(
            {
                "Issue": ["databaseId", "parent"],
                "PullRequest": ["databaseId"],
                "timeline_event_types": [
                    "CLOSED_EVENT",
                    "CROSS_REFERENCED_EVENT",
                    "REOPENED_EVENT",
                ],
            }
        )
        self.assertEqual(source._timeline_item_types(), _all_types())

    def test_absent_enum_fails_open(self) -> None:
        """The serializer stores the enum list only when known: a cached row
        without the key (empty introspection) must fail open to the full
        list — and the persisted JSON shape never carries a null."""
        source = GraphQLSource("o", "r")
        source.attach_capabilities({"Issue": ["databaseId"]})
        self.assertEqual(source._timeline_item_types(), _all_types())
        # The persistence shape: only list values are sorted; the enum list
        # never serializes as null (round-2 critic: sorted(None) aborts).
        caps = {
            "Issue": {"databaseId"},
            "timeline_event_types": {"CLOSED_EVENT", "REOPENED_EVENT"},
        }
        payload = json.dumps(
            {
                k: (sorted(v) if isinstance(v, (set, frozenset, list)) else v)
                for k, v in caps.items()
            }
        )
        self.assertNotIn("null", payload)
        source2 = GraphQLSource("o", "r")
        source2.attach_capabilities(json.loads(payload))
        self.assertEqual(
            source2._timeline_item_types(),
            "[CLOSED_EVENT, REOPENED_EVENT]",
            "ENUM-DEGRADATION-BROKEN: an enum missing CROSS_REFERENCED_EVENT"
            " must degrade to the intersection",
        )


def _all_types() -> str:
    from zaxbygraph.graphql import _TIMELINE_TYPES_ALL

    return _TIMELINE_TYPES_ALL


class ChildrenMappingTests(unittest.TestCase):
    def test_links_children_with_singular_nodes(self) -> None:
        children = GraphQLSource("o", "r")._children_from_node(
            {
                "blockedBy": {
                    "totalCount": 1,
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [{"number": 2, "repository": {"nameWithOwner": REPO}}],
                },
                "parent": None,
                "duplicateOf": {
                    "number": 50,
                    "repository": {"nameWithOwner": "Other/Repo"},
                },
                "closedByPullRequestsReferences": {
                    "totalCount": 0,
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [],
                },
            }
        )
        self.assertEqual(
            children["links"],
            {
                "blocked_by": [{"number": 2, "repo": REPO}],
                "parent": None,
                "duplicate_of": {"number": 50, "repo": "Other/Repo"},
                "open_prs": [],
            },
        )
        self.assertNotIn("links_incomplete", children)

    def test_capability_masked_fields_stay_absent(self) -> None:
        """A node that does not carry the link fields (probe-dropped) yields
        NO links key at all — ABSENT-by-omission, never empty-complete."""
        children = GraphQLSource("o", "r")._children_from_node({"comments": None})
        self.assertNotIn("links", children)
        self.assertNotIn("pr_commits", children)

    def test_commits_nodes_become_pr_commits(self) -> None:
        children = GraphQLSource("o", "r")._children_from_node(
            {
                "commits": {
                    "totalCount": 1,
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "commit": {
                                "oid": "abc123",
                                "messageHeadline": "fixes #2",
                                "messageBody": None,
                            }
                        }
                    ],
                }
            }
        )
        self.assertEqual(
            children["pr_commits"],
            [{"sha": "abc123", "message": "fixes #2"}],
        )

    def test_reopened_event_maps(self) -> None:
        self.assertEqual(
            _timeline_event({"__typename": "ReopenedEvent", "createdAt": ts(1)}),
            {"type": "reopened", "created_at": ts(1)},
        )


class ItemEventGateTests(TempDBTest):
    def test_events_replace_only_when_complete(self) -> None:
        """A complete timeline replaces (a vanished event is retracted); an
        incomplete timeline leaves stored rows; a complete-empty timeline
        clears."""
        base = [{"type": "reopened", "created_at": ts(1)}]
        self._ingest_with_timeline(base, complete=True)
        self.assertEqual(self.count("SELECT COUNT(*) FROM item_events"), 1)
        self._ingest_with_timeline(
            [
                {"type": "reopened", "created_at": ts(1)},
                {"type": "reopened", "created_at": ts(5)},
            ],
            complete=False,
        )
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM item_events"),
            1,
            "INCOMPLETE-TIMELINE-ERASED: a truncated timeline must not replace",
        )
        self._ingest_with_timeline([], complete=True)
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM item_events"),
            0,
            "COMPLETE-EMPTY-NOT-APPLIED: a complete empty timeline must clear",
        )

    def _ingest_with_timeline(
        self, timeline: list[dict], *, complete: bool
    ) -> None:
        self.conn.execute("BEGIN")
        ingest_item(
            self.conn,
            REPO,
            issue(4, title="t", updated_at=ts(10)),
            pull_raw=None,
            issue_comments=[],
            review_comments=[],
            reviews=[],
            files=[],
            include_patches=False,
            timeline=timeline,
            children_complete={"timeline": complete},
        )
        self.conn.commit()


class ReplayTimelinePresenceTests(TempDBTest):
    def test_replayed_rest_bundle_preserves_stored_events(self) -> None:
        """A REST-recorded bundle (timeline null) replayed over a store that
        HOLDS item_events leaves them intact (review round 1, Finding 2):
        the reader carries presence, never fabricating a present-complete
        timeline from a null."""
        import json as _json
        from pathlib import Path as _Path

        from fixtures import ts as _ts
        from replay import JsonlReplaySource
        from zaxbygraph.sync import sync_repo

        # Seed a store with one event-bearing item via the bulk path.
        src = _BulkSource()
        src.add_issue(issue(8, title="events", updated_at=_ts(10)))
        src.children[8] = {
            "timeline": [{"type": "reopened", "created_at": _ts(1)}],
        }
        src.list_releases = lambda: []
        from zaxbygraph.sync import sync_repo as _sync

        _sync(self.conn, src, REPO)
        self.assertEqual(self.count("SELECT COUNT(*) FROM item_events"), 1)

        # A later REST recording: timeline null, completeness recorded True.
        rec = {
            "resource": "item_bundle",
            "payload": {
                "number": 8,
                "item": issue(8, title="events", updated_at=_ts(30)),
                "pull": None,
                "issue_comments": [],
                "review_comments": [],
                "reviews": [],
                "files": [],
                "timeline": None,
                "children_complete": {"timeline": True},
                "gone": False,
                "gone_status": None,
            },
        }
        sidecar = _Path(self._td.name) / "rec" / "events.jsonl"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(_json.dumps(rec) + chr(10), encoding="utf-8")
        replay_src = JsonlReplaySource(sidecar)
        sync_repo(self.conn, replay_src, REPO)
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM item_events WHERE number = 8"),
            1,
            "REPLAY-ERASED-EVENTS: a null-timeline bundle must not erase"
            " stored item_events",
        )


class ConsolidationDriftGuardTests(unittest.TestCase):
    def test_copy_tables_covers_every_item_scoped_child(self) -> None:
        """Every table in sqlite_master whose PK leads with (repo, number)
        must appear in doctor._COPY_TABLES — consolidation can never silently
        drop an item-scoped child table again. Killed by: adding a child
        table without registering it (the v9/v10 drift the plan-critic
        flagged)."""
        from zaxbygraph.doctor import _COPY_TABLES

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_schema(conn)
        copied = {name for name, _pk in _COPY_TABLES}
        missing: list[str] = []
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts'"
        ):
            pk_cols = [
                r["name"]
                for r in conn.execute(f"PRAGMA table_info({row['name']})")
                if r["pk"]
            ]
            pk_cols.sort(key=lambda n: [
                r["pk"]
                for r in conn.execute(f"PRAGMA table_info({row['name']})")
                if r["name"] == n
            ][0])
            if pk_cols[:2] == ["repo", "number"] and row["name"] not in copied:
                missing.append(row["name"])
        self.assertEqual(
            missing,
            [],
            "COPY-TABLES-DRIFT: item-scoped child tables missing from"
            " doctor._COPY_TABLES",
        )


class CollapseScopeTests(unittest.TestCase):
    def test_closes_keyword_collisions_merge_other_relations_last_write(self) -> None:
        """The pre-collapse evidence merge applies ONLY to closes_keyword;
        actor evidence keeps the pinned last-write contracts (and edge
        identity never changes)."""
        from zaxbygraph.extract import collapse_edges

        edges = [
            ("item", "1", "closes_keyword", "item", "2", "body@0 closing keyword", "keyword"),
            ("item", "1", "closes_keyword", "item", "2", "commit abc closing keyword", "keyword"),
            ("actor", "a", "commented", "item", "1", "comment:11", "payload"),
            ("actor", "a", "commented", "item", "1", "comment:12", "payload"),
        ]
        out = collapse_edges(edges)
        by_rel = {(e[0], e[1], e[2], e[4]): e[5] for e in out}
        self.assertEqual(
            by_rel[("item", "1", "closes_keyword", "2")],
            "body@0 closing keyword; commit abc closing keyword",
        )
        self.assertEqual(by_rel[("actor", "a", "commented", "1")], "comment:12")


class RegistryPartitionTests(unittest.TestCase):
    def test_new_relations_classified(self) -> None:
        self.assertTrue(
            {
                "branch_ref",
                "blocked_by",
                "child_of",
                "duplicate_of",
                "linked_pr",
                "duplicate_keyword",
            }
            <= RELATIONS
        )


class ForwardBranchLookupTests(unittest.TestCase):
    def test_forward_pass_uses_issue_lookup(self) -> None:
        """The forward branch_ref pass consults the stored-issue lookup; a
        PR-numbered match is not an issue target (kind='issue' filter)."""
        pr = issue(7, title="pr", kind="pr", updated_at=ts(0))
        pr["base"] = {"ref": "main"}
        pr["head"] = {"ref": "issue-7-fix"}
        edges = edges_from_pr_state(REPO, pr, None, lambda numbers: {7})
        self.assertNotIn(
            "branch_ref",
            {e[2] for e in edges},
            "a PR number must not satisfy the stored-ISSUE lookup",
        )


class BodyCommitSimultaneityTests(unittest.TestCase):
    def test_body_and_commit_close_union_their_evidence(self) -> None:
        """One payload whose body AND a retained commit both close #N yields
        ONE closes_keyword edge carrying BOTH provenances (the collapse-scope
        merge), with the 7-column identity unchanged."""
        raw = {
            "number": 6,
            "title": "t",
            "body": "fixes #2",
            "user": {"login": "a"},
        }
        from zaxbygraph.extract import (
            collapse_edges,
            edges_from_commit_messages,
        )

        collected = edges_from_item(REPO, raw)
        collected.extend(
            edges_from_commit_messages(
                REPO, 6, [{"sha": "abc1234", "message": "also fixes #2"}]
            )
        )
        out = collapse_edges(collected)
        keyword = [e for e in out if e[2] == "closes_keyword"]
        self.assertEqual(len(keyword), 1, "SIMULTANEITY-SPLIT: two rows per pair")
        self.assertIn("body@", keyword[0][5])
        self.assertIn("commit abc1234", keyword[0][5])


if __name__ == "__main__":
    unittest.main()
