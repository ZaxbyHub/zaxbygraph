"""Issue #24 supplementary pins: behavior adjacent to the frozen C1-C9 checks
that those checks' shapes cannot reach (the repo's `_supp` convention, #23).

RED/GREEN contract: these run GREEN on the fixed tree; each docstring names
the exact mutant that would fail it. The frozen checks in
test_payload_signals.py remain the acceptance record — this module hardens
their flanks, it does not amend them.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, issue, pr_file, pull, review
from zaxbygraph import db, doctor
from zaxbygraph import graphql as graphql_module
from zaxbygraph.extract import extract_listing_signals, extract_pull_owned
from zaxbygraph.query import search, status
from zaxbygraph.store import ingest_item, upsert_item_row

# A genuine v6-shaped store: the items/comments DDL exactly as v6 wrote them
# (no v7 columns, 3-column items_fts, no reviews_fts). migrate_v6_to_v7 must
# ALTER, backfill, and rebuild on THIS shape — the frozen C2 rewind exercises
# a store that already has the v7 columns (user_version rewound), which
# cannot see a wrong ALTER.
_V6_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actors (login TEXT PRIMARY KEY, html_url TEXT);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('issue', 'pr')), node_id TEXT,
    title TEXT NOT NULL, body TEXT, labels_text TEXT, state TEXT NOT NULL,
    state_reason TEXT, author TEXT, created_at TEXT, updated_at TEXT,
    closed_at TEXT, merged_at TEXT, merge_commit TEXT,
    draft INTEGER NOT NULL DEFAULT 0, locked INTEGER NOT NULL DEFAULT 0,
    base_ref TEXT, head_ref TEXT, additions INTEGER, deletions INTEGER,
    changed_files INTEGER, commits INTEGER, html_url TEXT, api_url TEXT,
    raw_json TEXT NOT NULL, UNIQUE (repo, number)
);
CREATE TABLE IF NOT EXISTS labels (
    repo TEXT NOT NULL, number INTEGER NOT NULL, name TEXT NOT NULL,
    color TEXT, PRIMARY KEY (repo, number, name)
);
CREATE TABLE IF NOT EXISTS comments (
    pk INTEGER PRIMARY KEY AUTOINCREMENT, github_id INTEGER NOT NULL,
    repo TEXT NOT NULL, number INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('issue_comment', 'review_comment')),
    author TEXT, created_at TEXT, updated_at TEXT, body TEXT, html_url TEXT,
    in_reply_to INTEGER, raw_json TEXT NOT NULL,
    UNIQUE (repo, kind, github_id)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL,
    author TEXT, state TEXT, submitted_at TEXT, body TEXT, html_url TEXT,
    raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pr_files (
    repo TEXT NOT NULL, number INTEGER NOT NULL, path TEXT NOT NULL,
    status TEXT, additions INTEGER, deletions INTEGER, changes INTEGER,
    sha TEXT, patch TEXT, PRIMARY KEY (repo, number, path)
);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, body, labels_text, tokenize = 'porter unicode61',
    content='items', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5(
    body, tokenize = 'porter unicode61', content='comments', content_rowid='pk'
);
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, body, labels_text)
    VALUES (new.id, new.title, new.body, new.labels_text);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, labels_text)
    VALUES ('delete', old.id, old.title, old.body, old.labels_text);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, labels_text)
    VALUES ('delete', old.id, old.title, old.body, old.labels_text);
    INSERT INTO items_fts(rowid, title, body, labels_text)
    VALUES (new.id, new.title, new.body, new.labels_text);
END;
CREATE TRIGGER IF NOT EXISTS comments_ai AFTER INSERT ON comments BEGIN
    INSERT INTO comments_fts(rowid, body) VALUES (new.pk, new.body);
END;
CREATE TRIGGER IF NOT EXISTS comments_ad AFTER DELETE ON comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body)
    VALUES ('delete', old.pk, old.body);
END;
CREATE TRIGGER IF NOT EXISTS comments_au AFTER UPDATE ON comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body)
    VALUES ('delete', old.pk, old.body);
    INSERT INTO comments_fts(rowid, body) VALUES (new.pk, new.body);
END;
CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL,
    resource TEXT NOT NULL, resource_id TEXT, fetched_at TEXT NOT NULL,
    http_status INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS sync_state (
    repo TEXT PRIMARY KEY, issues_since TEXT, last_full_sync_at TEXT,
    last_incr_sync_at TEXT, last_error TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    comment_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    include_patches INTEGER NOT NULL DEFAULT 0,
    full_sync_pending INTEGER NOT NULL DEFAULT 0,
    rate_limit_remaining INTEGER, rate_limit_reset_at TEXT
);
"""


def _v6_store(path: Path) -> sqlite3.Connection:
    conn = db.connect(path)
    conn.executescript(_V6_DDL)
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    return conn


def _v6_item(number: int, title: str, raw: dict) -> tuple:
    return (
        raw["id"],
        REPO,
        number,
        raw.get("pull_request") and "pr" or "issue",
        title,
        raw.get("body"),
        raw.get("state") or "open",
        (raw.get("user") or {}).get("login"),
        raw.get("created_at"),
        raw.get("updated_at"),
        raw.get("html_url"),
        raw.get("url"),
        json.dumps(raw),
    )


def _signals(rec: dict) -> dict:
    rec = dict(rec)
    rec["reactions"] = {"total_count": 3, "+1": 1}
    rec["assignees"] = [{"login": "carol"}, {"login": "dave"}]
    rec["milestone"] = {"title": "v1.0", "number": 7, "state": "open"}
    rec["type"] = {"name": "Bug"}
    rec["sub_issues_summary"] = {"total": 2, "completed": 1, "percent_completed": 50}
    rec["issue_dependencies_summary"] = {
        "blocked_by": 1,
        "blocking": 2,
        "total_blocked_by": 3,
        "total_blocking": 4,
    }
    rec["issue_field_values"] = [{"name": "Priority", "value": "high"}]
    rec["author_association"] = "MEMBER"
    return rec


class GenuineV6MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.conn = _v6_store(Path(self._td.name) / "v6.db")

    def tearDown(self) -> None:
        self.conn.close()
        self._td.cleanup()

    def test_genuine_v6_alters_backfills_and_makes_reviews_searchable(self) -> None:
        """The genuine v6 -> v7 path: guarded ALTERs fire on a store that
        truly lacks the columns, the backfill populates them from raw_json,
        and a review stored BEFORE the migration is searchable after it
        WITHOUT any re-ingest (mutant: reviews_fts created but never
        populated; or backfill that only works on pre-created columns)."""
        raw = _signals(issue(301, title="Genuine v6 row", body="historical body"))
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " author, created_at, updated_at, html_url, api_url, raw_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _v6_item(301, "Genuine v6 row", raw),
        )
        self.conn.execute(
            "INSERT INTO reviews(id, repo, number, author, state, submitted_at,"
            " body, html_url, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                60301,
                REPO,
                301,
                "bob",
                "APPROVED",
                "2026-01-01T00:00:00Z",
                "the mammothdrift landed here first",
                f"https://github.com/{REPO}/pull/301#review-60301",
                "{}",
            ),
        )
        self.conn.execute(
            "INSERT INTO comments(github_id, repo, number, kind, author,"
            " created_at, updated_at, body, html_url, in_reply_to, raw_json)"
            " VALUES (?, ?, ?, 'issue_comment', ?, ?, ?, ?, ?, NULL, ?)",
            (
                70301,
                REPO,
                301,
                "carol",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                "plain comment",
                f"https://github.com/{REPO}/issues/301#comment-70301",
                json.dumps({"author_association": "CONTRIBUTOR"}),
            ),
        )
        self.conn.commit()

        db.migrate_v6_to_v7(self.conn)
        # The chain stamps the version after the migration body (db.py:621);
        # a direct call mirrors that stamp, exactly what init_schema would do.
        self.conn.execute(f"PRAGMA user_version = {db.CURRENT_USER_VERSION}")
        self.conn.commit()

        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(items)")}
        self.assertTrue(
            {"reactions_total", "milestone", "merged_by", "paths_text"} <= cols,
            "supp-v7: genuine-v6 ALTERs did not fire",
        )
        row = self.conn.execute(
            "SELECT reactions_total, milestone, item_type, author_association,"
            " comment_count FROM items WHERE number = 301"
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (3, "v1.0", "Bug", "MEMBER", 1),
            "supp-v7: genuine-v6 backfill did not populate from raw_json",
        )
        assoc = self.conn.execute(
            "SELECT author_association FROM comments WHERE github_id = 70301"
        ).fetchone()[0]
        self.assertEqual(
            assoc, "CONTRIBUTOR", "supp-v7: comment association not backfilled"
        )
        data = search(self.conn, "mammothdrift", repo=REPO)
        self.assertEqual(
            data["total_matches"],
            1,
            "supp-v7: pre-migration review invisible after migration "
            "(reviews_fts created but not populated)",
        )
        self.assertEqual(data["items"][0]["number"], 301)

    def test_pre_v7_reads_degrade_and_coverage_reports_null(self) -> None:
        """On a store that stays at v6, search still answers (legacy arms,
        non-empty snippet hydration) and status still emits coverage with
        null_v7_rows null (mutant: an unguarded v7-object reference raising
        OperationalError on the read path)."""
        raw = issue(302, title="Legacy readable", body="legacybodytext")
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " author, created_at, updated_at, html_url, api_url, raw_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _v6_item(302, "Legacy readable", raw),
        )
        self.conn.commit()
        self.conn.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")

        data = search(self.conn, "legacybodytext", repo=REPO)
        self.assertEqual(
            data["total_matches"], 1, "supp-v7: legacy search arm broke"
        )
        self.assertTrue(
            data["items"][0]["snippet"], "supp-v7: legacy hydration empty"
        )
        self.assertTrue(data["index_stale"])
        cover = status(self.conn, REPO)["coverage"]
        self.assertIsNone(
            cover["null_v7_rows"], "supp-v7: null_v7_rows must be null pre-v7"
        )
        self.assertIsInstance(cover["store_bytes"], int)


class MapperAbsenceTests(unittest.TestCase):
    """The GraphQL mapper must preserve key absence (round 3/4 blockers)."""

    def _source(self) -> object:
        return graphql_module.GraphQLSource(*REPO.split("/"))

    def test_comment_association_absent_when_not_selected(self) -> None:
        """A node whose comment dict lacks authorAssociation maps to a
        payload WITHOUT the author_association key (mutant: .get() default
        emitting the key with None)."""
        src = self._source()
        node = {
            "comments": {
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {
                        "databaseId": 4242,
                        "body": "no association selected",
                        "createdAt": "2026-02-01T00:00:00Z",
                        "updatedAt": "2026-02-01T00:00:00Z",
                        "url": f"https://github.com/{REPO}/issues/9#issuecomment-4242",
                        "author": {"login": "bob", "url": "https://github.com/bob"},
                    }
                ],
            }
        }
        mapped = src._children_from_node(node)["issue_comments"][0]
        self.assertNotIn(
            "author_association",
            mapped,
            "supp-v7: mapper fabricated author_association for an unselected node",
        )

    def test_pull_masked_fields_stay_absent(self) -> None:
        """A pull node whose mergedAt/mergeCommit/mergedBy selections were
        capability-dropped maps to a pull payload WITHOUT those keys — the
        store's ABSENT-merge then preserves stored values (mutant: emitting
        the keys with None, converting masked into clearing)."""
        src = self._source()
        node = {
            "baseRefName": "main",
            "headRefName": "feat/x",
            "additions": 1,
            "deletions": 0,
            "changedFiles": 1,
            "commits": {"totalCount": 1},
            "isDraft": False,
            "closingIssuesReferences": {"totalCount": 0, "pageInfo": {}, "nodes": []},
        }
        mapped = src._children_from_node(node)["pull"]
        for absent_key in ("merged_at", "merge_commit_sha", "merged_by"):
            self.assertNotIn(
                absent_key,
                mapped,
                f"supp-v7: masked {absent_key} emitted with null",
            )

    def test_same_id_two_kinds_keep_own_associations(self) -> None:
        """github_id 777 in BOTH comment kinds keeps each kind's own
        association through mapping + ingest (per-kind snapshot maps; mutant:
        one shared github_id-keyed map cross-restoring values)."""
        node = {
            "comments": {
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {
                        "databaseId": 777,
                        "body": "issue comment 777",
                        "createdAt": "2026-02-01T00:00:00Z",
                        "updatedAt": "2026-02-01T00:00:00Z",
                        "url": f"https://github.com/{REPO}/issues/41#issuecomment-777",
                        "authorAssociation": "MEMBER",
                        "author": {"login": "alice", "url": ""},
                    }
                ],
            },
            "reviews": {
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {
                        "databaseId": 55,
                        "state": "APPROVED",
                        "body": "review body",
                        "submittedAt": "2026-02-01T00:00:00Z",
                        "url": f"https://github.com/{REPO}/pull/41#review-55",
                        "author": {"login": "bob"},
                        "comments": {
                            "totalCount": 1,
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [
                                {
                                    "databaseId": 777,
                                    "body": "review comment 777",
                                    "createdAt": "2026-02-01T00:00:00Z",
                                    "updatedAt": "2026-02-01T00:00:00Z",
                                    "url": f"https://github.com/{REPO}/pull/41#rc-777",
                                    "authorAssociation": "CONTRIBUTOR",
                                    "replyTo": None,
                                    "author": {"login": "bob", "url": ""},
                                }
                            ],
                        },
                    }
                ],
            },
        }
        children = self._source()._children_from_node(node)
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            db.init_schema(conn)
            conn.execute("BEGIN")
            ingest_item(
                conn,
                REPO,
                issue(41, title="Two kinds", body=""),
                pull_raw=None,
                issue_comments=children["issue_comments"],
                review_comments=children["review_comments"],
                reviews=[],
                files=[],
                include_patches=False,
            )
            conn.commit()
            rows = {
                kind: assoc
                for kind, assoc in conn.execute(
                    "SELECT kind, author_association FROM comments"
                    " WHERE github_id = 777"
                )
            }
            conn.close()
        self.assertEqual(
            rows.get("issue_comment"),
            "MEMBER",
            "supp-v7: issue comment lost its own association",
        )
        self.assertEqual(
            rows.get("review_comment"),
            "CONTRIBUTOR",
            "supp-v7: review comment lost its own association",
        )

    def test_degraded_reingest_preserves_association(self) -> None:
        """Two complete ingests: the first carries authorAssociation, the
        second (probe-degraded) node lacks it — the stored value survives the
        child-replacement delete via the snapshot (mutant: snapshot keyed by
        the wrong seam or dropped entirely)."""
        rich = {
            "id": 4243,
            "body": "with association",
            "user": {"login": "bob"},
            "created_at": "2026-02-01T00:00:00Z",
            "updated_at": "2026-02-01T00:00:00Z",
            "html_url": f"https://github.com/{REPO}/issues/42#issuecomment-4243",
            "in_reply_to_id": None,
            "author_association": "CONTRIBUTOR",
        }
        degraded = {k: v for k, v in rich.items() if k != "author_association"}
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            db.init_schema(conn)
            for payload in (rich, degraded):
                conn.execute("BEGIN")
                ingest_item(
                    conn,
                    REPO,
                    issue(42, title="Preserve association", body=""),
                    pull_raw=None,
                    issue_comments=[payload],
                    review_comments=[],
                    reviews=[],
                    files=[],
                    include_patches=False,
                )
                conn.commit()
            assoc = conn.execute(
                "SELECT author_association FROM comments WHERE github_id = 4243"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(
            assoc,
            "CONTRIBUTOR",
            "supp-v7: probe-degraded re-ingest cleared the stored association",
        )


class ChildTableTests(unittest.TestCase):
    def test_assignees_and_field_values_replace_on_reingest(self) -> None:
        """The item-scoped children invariant extends to the new tables
        (mutant: replace_item_children missing the new deletes)."""
        listing_one = _signals(issue(61, title="Child replace", body=""))
        listing_two = dict(listing_one)
        listing_two["assignees"] = [{"login": "dave"}, {"login": "erin"}]
        listing_two["issue_field_values"] = [{"name": "Priority", "value": "low"}]
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            db.init_schema(conn)
            for listing in (listing_one, listing_two):
                conn.execute("BEGIN")
                ingest_item(
                    conn,
                    REPO,
                    listing,
                    pull_raw=None,
                    issue_comments=[],
                    review_comments=[],
                    reviews=[],
                    files=[],
                    include_patches=False,
                )
                conn.commit()
            logins = sorted(
                r[0]
                for r in conn.execute(
                    "SELECT login FROM assignees WHERE number = 61"
                )
            )
            fields = [
                tuple(r)
                for r in conn.execute(
                    "SELECT field_name, value FROM issue_field_values"
                    " WHERE number = 61"
                )
            ]
            conn.close()
        self.assertEqual(logins, ["dave", "erin"], "supp-v7: assignees not replaced")
        self.assertEqual(
            fields, [("Priority", "low")], "supp-v7: field values not replaced"
        )

    def test_comment_count_is_exact_under_duplicates_and_idless(self) -> None:
        """comment_count counts STORED rows: duplicate pagination collapses
        and ID-less records are skipped (mutant: len(incoming-lists))."""
        dup = {
            "id": 9001,
            "body": "dup",
            "user": {"login": "a"},
            "created_at": "2026-02-01T00:00:00Z",
            "updated_at": "2026-02-01T00:00:00Z",
            "html_url": "",
            "in_reply_to_id": None,
        }
        idless = {"body": "no id", "user": {"login": "a"}}
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            db.init_schema(conn)
            conn.execute("BEGIN")
            ingest_item(
                conn,
                REPO,
                issue(62, title="Count exactness", body=""),
                pull_raw=None,
                issue_comments=[dup, dict(dup), idless],
                review_comments=[],
                reviews=[],
                files=[],
                include_patches=False,
            )
            conn.commit()
            count = conn.execute(
                "SELECT comment_count FROM items WHERE number = 62"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(
            count, 1, "supp-v7: comment_count is not the stored-row count"
        )


class MergeSemanticsTests(unittest.TestCase):
    #: (column, payload key path) for the parametric ABSENT/present-null
    #: guardrail. Listing-owned keys ride the listing payload; pull-owned ride
    #: the pull payload.
    LISTING_CASES = (
        ("reactions_total", "reactions", {"total_count": 5}, 5),
        ("milestone", "milestone", {"title": "v2"}, "v2"),
        ("item_type", "type", {"name": "Task"}, "Task"),
        ("sub_issues_total", "sub_issues_summary", {"total": 4}, 4),
        ("sub_issues_completed", "sub_issues_summary", {"completed": 2}, 2),
        ("author_association", "author_association", "MEMBER", "MEMBER"),
    )
    PULL_CASES = (
        ("merged_by", "merged_by", {"login": "merger"}, "merger"),
        ("merge_commit", "merge_commit_sha", "abc123", "abc123"),
        ("merged_at", "merged_at", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )

    def test_absent_preserves_and_explicit_null_clears(self) -> None:
        """Parametric guardrail over both source groups (Phase 4.2): for
        every payload-sourced v7 column, a re-ingest without the key keeps
        the stored value and a re-ingest with explicit JSON null clears it
        (mutants: wholesale excluded assignment; SQL COALESCE that treats
        null as absent)."""
        for column, key, payload_value, stored in self.LISTING_CASES:
            with tempfile.TemporaryDirectory() as td:
                conn = db.connect(Path(td) / "t.db")
                try:
                    db.init_schema(conn)
                    rich = issue(71, title="guardrail", body="")
                    rich[key] = payload_value
                    lean = issue(71, title="guardrail", body="")
                    lean.pop(key, None)
                    cleared = issue(71, title="guardrail", body="")
                    cleared[key] = None
                    conn.execute("BEGIN")
                    ingest_item(conn, REPO, rich, pull_raw=None, issue_comments=[],
                                review_comments=[], reviews=[], files=[],
                                include_patches=False)
                    conn.commit()
                    conn.execute("BEGIN")
                    ingest_item(conn, REPO, lean, pull_raw=None, issue_comments=[],
                                review_comments=[], reviews=[], files=[],
                                include_patches=False)
                    conn.commit()
                    kept = conn.execute(
                        f"SELECT {column} FROM items WHERE number = 71"
                    ).fetchone()[0]
                    conn.execute("BEGIN")
                    ingest_item(conn, REPO, cleared, pull_raw=None, issue_comments=[],
                                review_comments=[], reviews=[], files=[],
                                include_patches=False)
                    conn.commit()
                    wiped = conn.execute(
                        f"SELECT {column} FROM items WHERE number = 71"
                    ).fetchone()[0]
                finally:
                    conn.close()
            self.assertEqual(
                kept, stored, f"supp-v7: {column} lost on absent-key re-ingest"
            )
            self.assertIsNone(
                wiped, f"supp-v7: {column} did not clear on explicit null"
            )
        for column, key, payload_value, stored in self.PULL_CASES:
            with tempfile.TemporaryDirectory() as td:
                conn = db.connect(Path(td) / "t.db")
                try:
                    db.init_schema(conn)
                    listing = issue(72, title="pull guardrail", body="", kind="pr")
                    pull_payload = pull(72, merged=True)
                    pull_payload[key] = payload_value
                    conn.execute("BEGIN")
                    ingest_item(conn, REPO, listing, pull_raw=pull_payload,
                                issue_comments=[], review_comments=[], reviews=[],
                                files=[], include_patches=False)
                    conn.commit()
                    conn.execute("BEGIN")
                    ingest_item(conn, REPO, listing, pull_raw=None,
                                issue_comments=[], review_comments=[], reviews=[],
                                files=[], include_patches=False)
                    conn.commit()
                    kept = conn.execute(
                        f"SELECT {column} FROM items WHERE number = 72"
                    ).fetchone()[0]
                finally:
                    conn.close()
            self.assertEqual(
                kept, stored,
                f"supp-v7: pull-owned {column} lost on listing-only re-ingest "
                "(the original P5 defect shape)",
            )

    def test_listing_owns_conflicting_keys_over_pull(self) -> None:
        """A pull payload carrying the same-named listing key cannot
        overwrite the listing's value (mutant: extraction from merged)."""
        listing = issue(73, title="ownership", body="")
        listing["reactions"] = {"total_count": 5}
        pull_payload = pull(73)
        pull_payload["reactions"] = {"total_count": 9}
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            db.init_schema(conn)
            conn.execute("BEGIN")
            ingest_item(conn, REPO, listing, pull_raw=pull_payload,
                        issue_comments=[], review_comments=[], reviews=[],
                        files=[], include_patches=False)
            conn.commit()
            total = conn.execute(
                "SELECT reactions_total FROM items WHERE number = 73"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(
            total, 5, "supp-v7: pull payload overwrote a listing-owned column"
        )

    def test_id_conflict_arm_preserves_pull_values(self) -> None:
        """The ON CONFLICT(id) arm (repo/number changed) resolves the stored
        row by id and preserves ABSENT pull values (mutant: single-identity
        lookup)."""
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            db.init_schema(conn)
            first = issue(80, title="same id", body="")
            first["id"] = 555
            first_pull = pull(80, merged=True)
            first_pull["merge_commit_sha"] = "cafe001"
            conn.execute("BEGIN")
            ingest_item(conn, REPO, first, pull_raw=first_pull,
                        issue_comments=[], review_comments=[], reviews=[],
                        files=[], include_patches=False)
            conn.commit()
            moved = issue(81, title="same id moved", body="")
            moved["id"] = 555
            conn.execute("BEGIN")
            ingest_item(conn, REPO, moved, pull_raw=None,
                        issue_comments=[], review_comments=[], reviews=[],
                        files=[], include_patches=False)
            conn.commit()
            kept = conn.execute(
                "SELECT merge_commit FROM items WHERE id = 555"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(
            kept, "cafe001", "supp-v7: id-conflict arm cleared a pull value"
        )

    def test_paths_text_deterministic_and_trigger_propagates(self) -> None:
        """paths_text is ORDER BY path deterministic (mutant: query-order
        serialization) and an unrelated items UPDATE re-rotates the FTS row
        so path search keeps working after any items write (trigger pin)."""
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            db.init_schema(conn)
            listing = issue(82, title="paths", body="", kind="pr")
            files = [
                pr_file("src/zebra/last.py"),
                pr_file("src/alpha/first.py"),
                pr_file("src/middle/wy.py"),
            ]
            conn.execute("BEGIN")
            ingest_item(conn, REPO, listing, pull_raw=pull(82),
                        issue_comments=[], review_comments=[], reviews=[],
                        files=files, include_patches=False)
            conn.commit()
            text = conn.execute(
                "SELECT paths_text FROM items WHERE number = 82"
            ).fetchone()[0]
            conn.execute("UPDATE items SET title = 'paths renamed' WHERE number = 82")
            conn.commit()
            found = search(conn, "alpha", repo=REPO)["total_matches"]
            conn.close()
        self.assertEqual(
            text, "src/alpha/first.py src/middle/wy.py src/zebra/last.py",
            "supp-v7: paths_text not path-ordered",
        )
        self.assertEqual(found, 1, "supp-v7: unrelated UPDATE broke path search")

    def test_reviews_fts_porter_parity(self) -> None:
        """reviews_fts carries the same porter tokenizer as the other FTS
        tables on a fresh store (mutant: drift to the default tokenizer)."""
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            db.init_schema(conn)
            names = [
                "items_fts",
                "comments_fts",
                "reviews_fts",
            ]
            ddls = {
                name: conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
                ).fetchone()[0]
                for name in names
            }
            conn.close()
        for name, ddl in ddls.items():
            self.assertIn(
                "porter", ddl.lower(), f"supp-v7: {name} lost the porter tokenizer"
            )


class ConsolidationTests(unittest.TestCase):
    def test_consolidation_adopts_child_tables(self) -> None:
        """doctor --consolidate's delete+copy inventory includes assignees and
        issue_field_values: candidate rows arrive and stale destination rows
        do not survive (mutant: tables missing from _COPY_TABLES)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = tmp / "store.db"
            candidate = tmp / "candidate.db"
            conn = db.connect(store)
            db.init_schema(conn)
            conn.close()
            conn = db.connect(candidate)
            db.init_schema(conn)
            listing = _signals(issue(91, title="adopted", body=""))
            listing["assignees"] = [{"login": "carol"}]
            listing["issue_field_values"] = [{"name": "Priority", "value": "high"}]
            conn.execute("BEGIN")
            ingest_item(conn, REPO, listing, pull_raw=None, issue_comments=[],
                        review_comments=[], reviews=[], files=[],
                        include_patches=False)
            conn.commit()
            conn.close()
            # stale destination rows under the same slug must not survive
            conn = db.connect(store)
            conn.execute("BEGIN")
            ingest_item(conn, REPO, issue(92, title="stale dest", body=""),
                        pull_raw=None, issue_comments=[], review_comments=[],
                        reviews=[], files=[], include_patches=False)
            conn.commit()
            conn.execute(
                "INSERT INTO assignees(repo, number, login) VALUES (?, 92, 'ghost')",
                (REPO,),
            )
            conn.commit()
            conn.close()
            doctor.consolidate(store, candidate, REPO)
            conn = db.connect(store)
            logins = sorted(
                r[0] for r in conn.execute("SELECT login FROM assignees")
            )
            fields = [
                r[0]
                for r in conn.execute("SELECT field_name FROM issue_field_values")
            ]
            numbers = sorted(
                r[0] for r in conn.execute("SELECT number FROM items")
            )
            conn.close()
        self.assertEqual(logins, ["carol"], "supp-v7: consolidation child rows wrong")
        self.assertEqual(fields, ["Priority"])
        self.assertEqual(numbers, [91], "supp-v7: stale destination rows survived")


class VersionLiteralTests(unittest.TestCase):
    def test_version_literals_derive_from_constants(self) -> None:
        """The version-literal class is eradicated: the four files that
        carried `== 6` / `"6"` schema assertions now import the constants
        (mutant: a future bump re-breaks the suite)."""
        tests_dir = Path(__file__).resolve().parent
        literal = re.compile(
            r"""(?:fetchone\(\)\[0\],\s*6\b|fetchone\(\)\[0\]\)\s*,\s*6\b|"
            'schema_version'"\s*\)?\s*,?\s*\n?\s*"6")""",
        )
        for name in (
            "test_migrations.py",
            "test_closes_edge_uniqueness.py",
            "test_feedback_hardening.py",
            "test_doctor.py",
        ):
            source = (tests_dir / name).read_text(encoding="utf-8")
            self.assertIsNone(
                literal.search(source),
                f"supp-v7: {name} still asserts a literal schema version",
            )
        for name in (
            "test_migrations.py",
            "test_closes_edge_uniqueness.py",
            "test_feedback_hardening.py",
            "test_doctor.py",
        ):
            source = (tests_dir / name).read_text(encoding="utf-8")
            self.assertIn(
                "CURRENT_USER_VERSION",
                source,
                f"supp-v7: {name} no longer references CURRENT_USER_VERSION",
            )


if __name__ == "__main__":
    unittest.main()
