from __future__ import annotations

from fixtures import (
    REPO,
    TempDBTest,
    comment,
    issue,
    pr_file,
    pull,
    review,
)
from zaxbygraph.store import ingest_item


class StoreTests(TempDBTest):
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
            include_patches=kwargs.get("include_patches", False),
        )
        self.conn.commit()

    def test_upsert_idempotent(self) -> None:
        rec = issue(1, title="one", body="hello")
        self.ingest(rec)
        self.ingest(rec)
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 1)

    def test_fts_hit_title_body_comment(self) -> None:
        rec = issue(1, title="watermark clock", body="inclusive since")
        self.ingest(rec, issue_comments=[comment(1, "resume after 429")])
        titles = self.conn.execute(
            "SELECT items.number FROM items_fts JOIN items ON items.id = items_fts.rowid "
            "WHERE items_fts MATCH ?",
            ('"watermark"',),
        ).fetchall()
        self.assertEqual([r[0] for r in titles], [1])
        bodies = self.conn.execute(
            "SELECT items.number FROM items_fts JOIN items ON items.id = items_fts.rowid "
            "WHERE items_fts MATCH ?",
            ('"inclusive"',),
        ).fetchall()
        self.assertEqual([r[0] for r in bodies], [1])
        comments = self.conn.execute(
            "SELECT comments.number FROM comments_fts JOIN comments ON comments.pk = comments_fts.rowid "
            "WHERE comments_fts MATCH ?",
            ('"resume"',),
        ).fetchall()
        self.assertEqual([r[0] for r in comments], [1])

    def test_fts_rebuild_on_body_edit(self) -> None:
        rec = issue(1, title="t", body="alpha token")
        self.ingest(rec)
        rec2 = dict(rec)
        rec2["body"] = "beta token"
        rec2["updated_at"] = "2026-02-01T00:00:00Z"
        self.ingest(rec2)
        old = self.conn.execute(
            "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH ?", ('"alpha"',)
        ).fetchone()[0]
        new = self.conn.execute(
            "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH ?", ('"beta"',)
        ).fetchone()[0]
        self.assertEqual(old, 0)
        self.assertEqual(new, 1)
        rows = self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_edge_replace_removes_old_file(self) -> None:
        rec = issue(7, title="pr", kind="pr")
        self.ingest(
            rec,
            pull_raw=pull(7, changed_files=1),
            files=[pr_file("src/old.py")],
        )
        self.ingest(
            rec,
            pull_raw=pull(7, changed_files=1),
            files=[pr_file("src/new.py")],
        )
        paths = [r[0] for r in self.conn.execute("SELECT path FROM pr_files").fetchall()]
        self.assertEqual(paths, ["src/new.py"])
        dests = [
            r[0]
            for r in self.conn.execute(
                "SELECT dst_id FROM edges WHERE rel = 'touches'"
            ).fetchall()
        ]
        self.assertEqual(dests, ["src/new.py"])

    def test_two_comments_one_commented_edge(self) -> None:
        rec = issue(1, title="t")
        self.ingest(
            rec,
            issue_comments=[
                comment(11, "first", author="alice"),
                comment(12, "second", author="alice"),
            ],
        )
        self.assertEqual(self.count("SELECT COUNT(*) FROM comments"), 2)
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM edges WHERE rel = 'commented' AND src_id = 'alice'"
            ),
            1,
        )
        evidence = self.conn.execute(
            "SELECT evidence FROM edges WHERE rel = 'commented'"
        ).fetchone()[0]
        self.assertEqual(evidence, "comment:12")

    def test_inbound_mention_survives_destination_upsert(self) -> None:
        five = issue(5, title="src", body="see #12")
        twelve = issue(12, title="dst", body="hello")
        self.ingest(five)
        self.ingest(twelve)
        self.ingest(dict(twelve, body="edited", updated_at="2026-03-01T00:00:00Z"))
        row = self.conn.execute(
            "SELECT COUNT(*) FROM edges WHERE rel = 'mentions' AND src_id = '5' AND dst_id = '12'"
        ).fetchone()[0]
        self.assertEqual(row, 1)

    def test_removed_label_disappears(self) -> None:
        rec = issue(1, labels=[{"name": "bug", "color": "ff0000"}, {"name": "gate", "color": "000"}])
        self.ingest(rec)
        rec2 = issue(1, labels=[{"name": "bug", "color": "ff0000"}])
        self.ingest(rec2)
        names = [r[0] for r in self.conn.execute("SELECT name FROM labels").fetchall()]
        self.assertEqual(names, ["bug"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM edges WHERE rel = 'has_label'"), 1)

    def test_comment_kind_unique_allows_same_github_id(self) -> None:
        rec = issue(3, title="pr", kind="pr")
        shared = comment(777, "issue thread")
        review_c = dict(comment(777, "diff thread"))
        self.ingest(
            rec,
            pull_raw=pull(3),
            issue_comments=[shared],
            review_comments=[review_c],
        )
        self.assertEqual(self.count("SELECT COUNT(*) FROM comments"), 2)
        kinds = sorted(
            r[0] for r in self.conn.execute("SELECT kind FROM comments").fetchall()
        )
        self.assertEqual(kinds, ["issue_comment", "review_comment"])

    def test_review_collapse_keeps_last_state(self) -> None:
        rec = issue(4, title="pr", kind="pr")
        self.ingest(
            rec,
            pull_raw=pull(4, changed_files=0),
            reviews=[
                review(1, "COMMENTED", author="bob"),
                review(2, "APPROVED", author="bob"),
            ],
        )
        self.assertEqual(self.count("SELECT COUNT(*) FROM reviews"), 2)
        ev = self.conn.execute(
            "SELECT evidence FROM edges WHERE rel = 'reviewed'"
        ).fetchone()[0]
        self.assertEqual(ev, "review:2:APPROVED")

    def test_pr_keeps_issue_id_and_list_watermark(self) -> None:
        rec = issue(7, title="pr", kind="pr", updated_at="2026-06-01T00:00:00Z")
        p = pull(7)
        p["updated_at"] = "2020-01-01T00:00:00Z"
        p["id"] = 20_007
        self.ingest(rec, pull_raw=p, files=[pr_file("src/x.py")])
        row = self.conn.execute(
            "SELECT id, updated_at, additions FROM items WHERE number = 7"
        ).fetchone()
        self.assertEqual(row[0], rec["id"])
        self.assertEqual(row[1], "2026-06-01T00:00:00Z")
        self.assertEqual(row[2], 1)
        wm = self.conn.execute("SELECT issues_since FROM sync_state").fetchone()[0]
        self.assertEqual(wm, "2026-06-01T00:00:00Z")

    def test_pull_id_does_not_replace_other_issue(self) -> None:
        other = issue(1, title="keep me")
        other["id"] = 20_007
        self.ingest(other)
        rec = issue(7, title="pr", kind="pr")
        self.ingest(rec, pull_raw=pull(7))
        titles = {
            r[0]: r[1] for r in self.conn.execute("SELECT number, title FROM items")
        }
        self.assertEqual(titles[1], "keep me")
        self.assertEqual(titles[7], "pr")
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 2)
