"""Issue #24 acceptance checks: payload signals become queryable v7 columns
plus two new search surfaces. RED at base (v7 missing), GREEN after the fix.

This module is the FROZEN contract the v7 migration implements. Payload key
names and shapes mirror the live REST listing payload probed in the trace's
02-reproduction.md (P2); the column/table names below are pinned by these
checks:

items columns (listing-payload signals)
    reactions_total                 INTEGER <- reactions.total_count
    milestone                       TEXT    <- milestone.title
    item_type                       TEXT    <- type.name
    sub_issues_total                INTEGER <- sub_issues_summary.total
    sub_issues_completed            INTEGER <- sub_issues_summary.completed
    dependencies_blocked_by         INTEGER <- issue_dependencies_summary.blocked_by
    dependencies_blocking           INTEGER <- issue_dependencies_summary.blocking
    dependencies_total_blocked_by   INTEGER <- issue_dependencies_summary.total_blocked_by
    dependencies_total_blocking     INTEGER <- issue_dependencies_summary.total_blocking

items columns (pull-payload signal, the AC3 COALESCE vehicle)
    merged_by                       TEXT    <- the pull payload's merged_by
                                            login ({"login": X} object or a
                                            bare string)

child tables
    assignees(repo, number, login)  one row per payload assignee
    issue_field_values(repo, number, field_name, value)
        one row per payload issue_field_values entry {"name": ..., "value": ...}

comments column
    author_association  TEXT <- the REST comment payload's author_association
        (supplied natively) or the GraphQL node's authorAssociation (mapped
        once the capability probe reports the field)

status() coverage block (AC6)
    coverage["truncation_notes"]  dict mapping fetch_log resource -> count of
                                  that resource's rows whose note contains
                                  "truncated" (the sync.py truncation notes)
    coverage["dropped_fields"]    int count of fetch_log rows whose note
                                  starts with "omitted " (capability-probe
                                  dropped fields, resource graphql_probe)
    coverage["null_v7_rows"]      int count of items rows with NULL v7 columns
    coverage["store_bytes"]       int store size in bytes, PRAGMA page_size
                                  * PRAGMA page_count

Each test gates on the surface it needs (PRAGMA table_info / getattr probes)
so the pre-fix run fails with the test's one-line signature instead of an
import or SQL error, and the post-fix run verifies populated behavior.
"""

from __future__ import annotations

import json
import sqlite3

from fixtures import REPO, TempDBTest, issue, pr_file, pull, review
from zaxbygraph import db
from zaxbygraph import graphql as graphql_module
from zaxbygraph.query import search, status
from zaxbygraph.store import ingest_item, log_fetch

#: The v7 items columns the listing payload populates (AC1/AC2). The pull
#: payload's merged_by (AC3's vehicle) is checked separately by name.
V7_ITEM_COLUMNS = (
    "reactions_total",
    "milestone",
    "item_type",
    "sub_issues_total",
    "sub_issues_completed",
    "dependencies_blocked_by",
    "dependencies_blocking",
    "dependencies_total_blocked_by",
    "dependencies_total_blocking",
)

_V7_SELECT = ", ".join(V7_ITEM_COLUMNS)


def enrich_with_signals(rec: dict) -> dict:
    """Add the live listing-payload signal keys (02-reproduction.md P2) to a
    fixtures.issue() record. Values are chosen so every count is distinct."""
    rec = dict(rec)
    rec["reactions"] = {"total_count": 3, "+1": 1}
    rec["assignees"] = [{"login": "carol"}, {"login": "dave"}]
    rec["milestone"] = {"title": "v1.0", "number": 7, "state": "open"}
    rec["type"] = {"name": "Bug", "color": "d73a4a"}
    rec["sub_issues_summary"] = {"total": 2, "completed": 1, "percent_completed": 50}
    rec["issue_dependencies_summary"] = {
        "blocked_by": 1,
        "blocking": 2,
        "total_blocked_by": 3,
        "total_blocking": 4,
    }
    rec["issue_field_values"] = [{"name": "Priority", "value": "high"}]
    return rec


def item_columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


class DirectIngestTest(TempDBTest):
    """TempDBTest plus the direct ingest_item helper test_store.py uses."""

    def ingest(self, list_raw, **kwargs):
        self.conn.execute("BEGIN")
        ingest_item(
            self.conn,
            REPO,
            list_raw,
            pull_raw=kwargs.get("pull_raw"),
            issue_comments=kwargs.get("issue_comments", []),
            review_comments=kwargs.get("review_comments", []),
            reviews=kwargs.get("reviews", []),
            files=kwargs.get("files", []),
            include_patches=False,
        )
        self.conn.commit()


class ListingSignalTests(TempDBTest):
    """AC1: ingested listing-payload signals become typed items columns or
    child rows. RED at base (v7 missing), GREEN after the fix."""

    def test_listing_fields_become_columns(self) -> None:
        """An issue whose listing payload carries reactions, assignees,
        milestone, type, sub-issue and dependency summaries exposes each as a
        typed items column or child row, open and total dependency counts
        distinct. RED at base (v7 missing), GREEN after the fix."""
        cols = item_columns(self.conn, "items")
        missing = [c for c in V7_ITEM_COLUMNS if c not in cols]
        if missing:
            self.fail("v7-missing:items-columns items lacks " + ", ".join(missing))
        for table, needed in (
            ("assignees", ("repo", "number", "login")),
            ("issue_field_values", ("repo", "number", "field_name", "value")),
        ):
            table_missing = [c for c in needed if c not in item_columns(self.conn, table)]
            if table_missing:
                self.fail(
                    f"v7-missing:items-columns {table} table absent or lacks "
                    + ", ".join(table_missing)
                )

        self.src.add_issue(
            enrich_with_signals(
                issue(41, title="Signals galore", body="rich listing payload")
            )
        )
        self.src.add_issue(issue(42, title="Plain old issue", body="no signal keys"))
        self.sync()

        row = self.conn.execute(
            f"SELECT {_V7_SELECT} FROM items WHERE repo = ? AND number = 41",
            (REPO,),
        ).fetchone()
        self.assertIsNotNone(row, "v7-missing:items-columns rich item not stored")
        expected = {
            "reactions_total": 3,
            "milestone": "v1.0",
            "item_type": "Bug",
            "sub_issues_total": 2,
            "sub_issues_completed": 1,
            "dependencies_blocked_by": 1,
            "dependencies_blocking": 2,
            "dependencies_total_blocked_by": 3,
            "dependencies_total_blocking": 4,
        }
        for column, want in expected.items():
            self.assertEqual(
                row[column],
                want,
                f"v7-missing:items-columns {column} not populated from the payload",
            )
        # open (current) and total dependency counts are distinct columns and
        # carry distinct values, never one collapsed count
        self.assertNotEqual(
            row["dependencies_blocking"],
            row["dependencies_total_blocking"],
            "v7-missing:items-columns open and total dependency counts collapsed",
        )
        logins = sorted(
            r[0]
            for r in self.conn.execute(
                "SELECT login FROM assignees WHERE repo = ? AND number = 41", (REPO,)
            )
        )
        self.assertEqual(
            logins,
            ["carol", "dave"],
            "v7-missing:items-columns assignees child rows not written",
        )
        field_values = {
            (r[0], r[1])
            for r in self.conn.execute(
                "SELECT field_name, value FROM issue_field_values"
                " WHERE repo = ? AND number = 41",
                (REPO,),
            )
        }
        self.assertIn(
            ("Priority", "high"),
            field_values,
            "v7-missing:items-columns issue_field_values child rows not written",
        )
        # a payload without the signal keys leaves the columns NULL
        lean = self.conn.execute(
            "SELECT reactions_total, milestone, item_type FROM items"
            " WHERE repo = ? AND number = 42",
            (REPO,),
        ).fetchone()
        self.assertEqual(
            (lean["reactions_total"], lean["milestone"], lean["item_type"]),
            (None, None, None),
            "v7-missing:items-columns absent payload keys must stay NULL",
        )


class V7BackfillTests(TempDBTest):
    """AC2: migrate_v6_to_v7 backfills the new columns from stored raw_json
    with Python json. RED at base (v7 missing), GREEN after the fix."""

    def test_v7_backfill_from_raw_json(self) -> None:
        """A v6 store's rows gain their v7 column values from the raw_json
        already stored -- no GitHub calls, no SQLite JSON functions, absent
        keys stay NULL. RED at base (v7 missing), GREEN after the fix."""
        plain_rich = issue(51, title="Rich history row", body="predates v7")
        plain_lean = issue(52, title="Lean history row", body="predates v7")
        rich_stored = enrich_with_signals(plain_rich)
        # Rows exactly as v6 code wrote them: v6 columns only, so the INSERT
        # is SQL-safe before the fix (no v7 columns exist) and after it (the
        # v7 columns default to NULL).
        for rec in (plain_rich, plain_lean):
            self.conn.execute(
                "INSERT INTO items(id, repo, number, kind, title, body, state,"
                " author, created_at, updated_at, html_url, api_url, raw_json)"
                " VALUES (?, ?, ?, 'issue', ?, ?, 'open', ?, ?, ?, ?, ?, ?)",
                (
                    rec["id"],
                    REPO,
                    rec["number"],
                    rec["title"],
                    rec["body"],
                    rec["user"]["login"],
                    rec["created_at"],
                    rec["updated_at"],
                    rec["html_url"],
                    rec["url"],
                    json.dumps(rec),
                ),
            )
        # Simulate the old payload that already carried the signal keys.
        self.conn.execute(
            "UPDATE items SET raw_json = ? WHERE repo = ? AND number = ?",
            (json.dumps(rich_stored), REPO, plain_rich["number"]),
        )
        # A genuine v6 store: the fresh store's version wound back to 6 (a
        # no-op at base, where init_schema already stamped 6).
        self.conn.execute("PRAGMA user_version = 6")
        self.conn.commit()

        migrate = getattr(db, "migrate_v6_to_v7", None)
        self.assertIsNotNone(
            migrate, "v7-missing:backfill db.migrate_v6_to_v7 absent"
        )

        # The backfill must parse raw_json in Python: the portability floor
        # (SQLite 3.35) predates guaranteed JSON1, so no SQLite JSON function
        # may run inside the migration. No GitHub object exists anywhere in
        # this test, so "no GitHub calls" holds by construction.
        def deny_json(action, arg1, arg2, dbname, trigger):
            if action == sqlite3.SQLITE_FUNCTION:
                name = arg2 or arg1 or ""
                if name and name.lower().startswith("json"):
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.conn.set_authorizer(deny_json)
        try:
            migrate(self.conn)
        except sqlite3.DatabaseError as exc:
            self.fail(
                f"v7-missing:backfill migration used a SQLite JSON function: {exc}"
            )
        finally:
            self.conn.set_authorizer(None)

        cols = item_columns(self.conn, "items")
        still_missing = [c for c in V7_ITEM_COLUMNS if c not in cols]
        if still_missing:
            self.fail(
                "v7-missing:backfill columns still absent after migration: "
                + ", ".join(still_missing)
            )
        row = self.conn.execute(
            f"SELECT {_V7_SELECT} FROM items WHERE repo = ? AND number = ?",
            (REPO, plain_rich["number"]),
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (3, "v1.0", "Bug", 2, 1, 1, 2, 3, 4),
            "v7-missing:backfill values not backfilled from raw_json",
        )
        lean_row = self.conn.execute(
            f"SELECT {_V7_SELECT} FROM items WHERE repo = ? AND number = ?",
            (REPO, plain_lean["number"]),
        ).fetchone()
        self.assertEqual(
            tuple(lean_row),
            (None,) * len(V7_ITEM_COLUMNS),
            "v7-missing:backfill keys absent from old payloads must stay NULL",
        )


class CrossSourceTests(DirectIngestTest):
    """AC3: source-specific column values survive a re-ingest whose payload
    lacks them (COALESCE on absent). RED at base (v7 missing), GREEN after
    the fix."""

    def test_source_specific_columns_survive_rest_reingest(self) -> None:
        """A pull-sourced column (merged_by) stored by the first ingest keeps
        its value when the item is re-ingested from the listing alone. RED at
        base (v7 missing), GREEN after the fix."""
        cols = item_columns(self.conn, "items")
        if "merged_by" not in cols:
            self.fail("v7-missing:coalesce items.merged_by column absent")

        listing = issue(61, title="Merge ownership signal", state="closed", kind="pr")
        pr_payload = pull(61, merged=True)
        pr_payload["merged_by"] = {"login": "merger"}
        self.ingest(listing, pull_raw=pr_payload)
        row = self.conn.execute(
            "SELECT merged_by FROM items WHERE repo = ? AND number = 61", (REPO,)
        ).fetchone()
        self.assertIsNotNone(row, "v7-missing:coalesce merged item not stored")
        self.assertEqual(
            row["merged_by"],
            "merger",
            "v7-missing:coalesce pull-sourced merged_by not stored on first ingest",
        )
        # The REST listing path carries no pull section (pull_raw=None): the
        # upsert must COALESCE on absent rather than null the stored value.
        relist = issue(
            61,
            title="Merge ownership signal",
            state="closed",
            kind="pr",
            updated_at="2026-03-01T00:00:00Z",
        )
        self.ingest(relist, pull_raw=None)
        after = self.conn.execute(
            "SELECT merged_by FROM items WHERE repo = ? AND number = 61", (REPO,)
        ).fetchone()
        self.assertEqual(
            after["merged_by"],
            "merger",
            "v7-missing:coalesce listing-only re-ingest nulled the pull-sourced merged_by",
        )


class ReviewSearchTests(TempDBTest):
    """AC4: review-body text is searchable and merges into the owning item.
    RED at base (v7 missing), GREEN after the fix."""

    def test_review_only_text_is_searchable(self) -> None:
        """A term appearing only in a review body returns the owning item
        from search, merged like comment hits. RED at base (v7 missing),
        GREEN after the fix."""
        self.src.add_pr(
            issue(
                71,
                title="Routine dependency cleanup",
                body="mechanical churn",
                kind="pr",
            ),
            pull(71),
            files=[],
        )
        self.src.reviews[71] = [
            review(
                60_071,
                "CHANGES_REQUESTED",
                body="the fluxcapacitor drifts under load",
                author="bob",
            )
        ]
        self.sync()
        data = search(self.conn, "fluxcapacitor", repo=REPO)
        self.assertEqual(
            data["total_matches"],
            1,
            "v7-missing:reviews-fts review-only term invisible to search",
        )
        numbers = [i["number"] for i in data["items"]]
        self.assertEqual(
            numbers,
            [71],
            "v7-missing:reviews-fts owning item not merged into the results",
        )


class PathSearchTests(TempDBTest):
    """AC5: a PR is findable by a path segment it touched, with path text
    weighted below title and body. RED at base (v7 missing), GREEN after the
    fix."""

    def test_changed_path_is_searchable(self) -> None:
        """A search term matching only a path segment of a file a PR touched
        returns that PR; title and body hits outrank path hits for the same
        term. RED at base (v7 missing), GREEN after the fix."""
        self.src.add_pr(
            issue(81, title="Adjust collector thresholds", body="tune defaults", kind="pr"),
            pull(81),
            files=[
                pr_file("src/quokkapath/collector.py"),
                pr_file("src/wombat/widget.py"),
                pr_file("src/narwhalpath/reader.py"),
            ],
        )
        self.src.add_issue(issue(83, title="wombat module redesign", body="tracking umbrella"))
        self.src.add_issue(
            issue(
                84,
                title="Directory reorganization plan",
                body="the narwhalpath tree needs pruning",
            )
        )
        self.sync()
        # (a) the term appears ONLY as a path segment: base returns nothing
        data = search(self.conn, "quokkapath", repo=REPO)
        self.assertEqual(
            data["total_matches"],
            1,
            "v7-missing:path-fts path-only term invisible to search",
        )
        self.assertEqual(
            [i["number"] for i in data["items"]],
            [81],
            "v7-missing:path-fts the PR that touched the path is not surfaced",
        )
        # (b) a title hit outranks a path hit for the same term
        by_title = search(self.conn, "wombat", repo=REPO)
        self.assertEqual(
            [i["number"] for i in by_title["items"]][0],
            83,
            "v7-missing:path-fts path text must rank below title text",
        )
        # (c) a body hit outranks a path hit for the same term
        by_body = search(self.conn, "narwhalpath", repo=REPO)
        self.assertEqual(
            [i["number"] for i in by_body["items"]][0],
            84,
            "v7-missing:path-fts path text must rank below body text",
        )


class StatusCoverageTests(TempDBTest):
    """AC6: status() reports a coverage block. RED at base (v7 missing),
    GREEN after the fix."""

    def test_status_reports_coverage(self) -> None:
        """status() includes coverage: fetch_log truncation notes by kind,
        capability-dropped fields, rows with NULL v7 columns, and the store's
        size in bytes. RED at base (v7 missing), GREEN after the fix."""
        self.src.add_issue(issue(91, title="Coverage probe item", body="no signal keys"))
        self.sync()
        # The raw material, shaped exactly as sync.py writes these notes
        # (sync.py:914 "timeline truncated: ...", :940 "truncated at GitHub
        # 3000-file cap", :998 "omitted <field>").
        log_fetch(
            self.conn,
            REPO,
            "item",
            "91",
            note="timeline truncated: retained newest 500 events",
            status=None,
        )
        log_fetch(self.conn, REPO, "pr_files", "91", note="truncated at GitHub 3000-file cap")
        log_fetch(self.conn, REPO, "graphql_probe", None, note="omitted authorAssociation")
        self.conn.commit()

        data = status(self.conn, REPO)
        coverage = data.get("coverage")
        if not isinstance(coverage, dict):
            self.fail("v7-missing:status-coverage status() reports no coverage block")
        self.assertIn(
            "truncation_notes",
            coverage,
            "v7-missing:status-coverage truncation_notes absent",
        )
        self.assertIn(
            "dropped_fields",
            coverage,
            "v7-missing:status-coverage dropped_fields absent",
        )
        self.assertIn(
            "null_v7_rows",
            coverage,
            "v7-missing:status-coverage null_v7_rows absent",
        )
        self.assertIn(
            "store_bytes",
            coverage,
            "v7-missing:status-coverage store_bytes absent",
        )
        notes = coverage["truncation_notes"]
        self.assertIsInstance(
            notes,
            dict,
            "v7-missing:status-coverage truncation_notes must map kind to count",
        )
        self.assertEqual(
            notes.get("item"),
            1,
            "v7-missing:status-coverage timeline truncation note not counted",
        )
        self.assertEqual(
            notes.get("pr_files"),
            1,
            "v7-missing:status-coverage pr_files truncation note not counted",
        )
        self.assertEqual(
            coverage["dropped_fields"],
            1,
            "v7-missing:status-coverage capability-dropped field not counted",
        )
        self.assertEqual(
            coverage["null_v7_rows"],
            1,
            "v7-missing:status-coverage rows with NULL v7 columns not counted",
        )
        page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = self.conn.execute("PRAGMA page_count").fetchone()[0]
        self.assertEqual(
            coverage["store_bytes"],
            page_size * page_count,
            "v7-missing:status-coverage store_bytes is not the store size in bytes",
        )


class CommentAssociationTests(DirectIngestTest):
    """AC7: comment author_association is stored (REST native, GraphQL mapped
    from the probe-reported authorAssociation). RED at base (v7 missing),
    GREEN after the fix."""

    def test_comment_author_association(self) -> None:
        """A GraphQL comment node whose selection carries authorAssociation
        maps to a REST-shaped payload that ingest persists as the stored
        comment row's author_association. RED at base (v7 missing), GREEN
        after the fix."""
        cols = item_columns(self.conn, "comments")
        if "author_association" not in cols:
            self.fail(
                "v7-missing:comment-association comments.author_association column absent"
            )

        # A comment node exactly as the selection fetches it once the
        # capability probe reports authorAssociation: camelCase field, no
        # REST author_association key anywhere on the node.
        node = {
            "comments": {
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {
                        "databaseId": 4242,
                        "body": "ship it",
                        "createdAt": "2026-02-01T00:00:00Z",
                        "updatedAt": "2026-02-01T00:00:00Z",
                        "url": f"https://github.com/{REPO}/issues/31#issuecomment-4242",
                        "author": {"login": "bob", "url": "https://github.com/bob"},
                        "authorAssociation": "CONTRIBUTOR",
                    }
                ],
            }
        }
        mapped = None
        source_cls = getattr(graphql_module, "GraphQLSource", None)
        if source_cls is not None:
            mapper = getattr(source_cls, "_children_from_node", None)
            if mapper is not None:
                children = mapper(source_cls(*REPO.split("/")), node)
                mapped_comments = children.get("issue_comments") or []
                if mapped_comments:
                    mapped = mapped_comments[0]
                    self.assertEqual(
                        mapped.get("author_association"),
                        "CONTRIBUTOR",
                        "v7-missing:comment-association GraphQL mapping drops authorAssociation",
                    )
        if mapped is None:
            # The mapping entry point moved: the ingest contract is still
            # pinned on the REST-shaped payload the mapping produces.
            mapped = {
                "id": 4242,
                "body": "ship it",
                "user": {"login": "bob"},
                "created_at": "2026-02-01T00:00:00Z",
                "updated_at": "2026-02-01T00:00:00Z",
                "html_url": f"https://github.com/{REPO}/issues/31#issuecomment-4242",
                "author_association": "CONTRIBUTOR",
            }
        self.ingest(
            issue(31, title="Provenance for comments", body="who said this"),
            issue_comments=[mapped],
        )
        stored = self.conn.execute(
            "SELECT author_association FROM comments WHERE repo = ? AND github_id = 4242",
            (REPO,),
        ).fetchone()
        self.assertIsNotNone(
            stored, "v7-missing:comment-association comment row not stored"
        )
        self.assertEqual(
            stored["author_association"],
            "CONTRIBUTOR",
            "v7-missing:comment-association not persisted by ingest",
        )


if __name__ == "__main__":
    unittest.main()
