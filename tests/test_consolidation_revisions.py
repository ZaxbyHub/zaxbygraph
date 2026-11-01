"""Consolidation revision-preservation acceptance checks (issue #40).

New file for the 40-48-36-followups trace (tests/test_revisions.py is over
the new-check file cap at 1121 lines). The DISCRIMINATING checks for
40-AC1/AC2 (``test_empty_candidate_keeps_stored_history``,
``test_partial_candidate_replaces_only_observed_items``) and the 40-AC4
report test (``test_adoption_reports_preserved_revision_counts``) are
authored by the fix, not at freeze time: at the frozen base this module
carries only the 40-AC6 check (``test_consolidation_twice_is_row_neutral``,
amended per plan-critic round 1 from count-only row neutrality to an
id-stability oracle), so the absence legs observe ``NO TESTS RAN`` (exit 5
on py -3.14).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zaxbygraph.db import connect, init_schema

REPO = "acme/forgegate"


class ConsolidationRowNeutralTests(unittest.TestCase):
    """40-AC6: consolidating the SAME candidate twice writes no new
    revision rows and deletes none after the first adoption."""

    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        self.store_db = Path(td.name) / "store.db"
        self.candidate_db = Path(td.name) / "candidate.db"

    def test_consolidation_twice_is_row_neutral(self) -> None:
        """RED at base, GREEN after the fix ships the same-candidate no-op
        path. The frozen contract is operation-neutrality - after the first
        adoption a second consolidation of the SAME candidate must write no
        new revision rows and delete none - asserted as (number, id) pair
        identity. At base the covered item's rows are deleted+reinserted;
        ``revisions.id`` is a plain rowid (max+1, no AUTOINCREMENT), so the
        rewrite can coincide with the old ids, and a sentinel other-repo
        row with a high explicit id is planted between the two adoptions to
        block rowid reuse and make the reassignment observable. A no-op
        second adoption keeps the pairs byte-identical whatever the
        sentinel does; a delete+reinsert reassigns ids above the sentinel
        and fails with REVISION-ROWIDS-CHANGED."""
        from zaxbygraph.doctor import consolidate

        items = [
            (1000 + n, REPO, n, "issue", f"item {n}", "open", "{}")
            for n in (1, 2, 3)
        ]

        store = connect(self.store_db)
        init_schema(store)
        store.executemany(
            "INSERT INTO items(id, repo, number, kind, title, state, raw_json)"
            " VALUES (?,?,?,?,?,?,?)",
            items,
        )
        store.executemany(
            "INSERT INTO revisions(repo, number, entity, origin, content,"
            " valid_from, valid_to) VALUES (?, ?, 'item_body', 'edit', ?, ?, ?)",
            [
                (REPO, 2, "STORE-A", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
                (REPO, 2, "STORE-B", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"),
            ],
        )
        store.commit()

        cand = connect(self.candidate_db)
        init_schema(cand)
        cand.executemany(
            "INSERT INTO items(id, repo, number, kind, title, state, raw_json)"
            " VALUES (?,?,?,?,?,?,?)",
            items,
        )
        cand.executemany(
            "INSERT INTO revisions(id, repo, number, entity, origin, content,"
            " valid_from, valid_to) VALUES (?, ?, ?, 'item_body', 'edit', ?, ?, ?)",
            [
                (1, REPO, 2, "CAND-A", "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z"),
                (2, REPO, 2, "CAND-B", "2026-02-02T00:00:00Z", "2026-02-03T00:00:00Z"),
            ],
        )
        cand.commit()
        cand.close()

        # consolidate() opens its own connections; the candidate's is closed
        # around the calls (fixture style of tests/test_revisions.py).
        consolidate(self.store_db, self.candidate_db, REPO)
        after_first = store.execute(
            "SELECT COUNT(*) FROM revisions WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertEqual(
            after_first,
            2,
            f"first adoption must leave exactly the candidate's 2 revision rows"
            f" (fresh ids re-assigned); got {after_first}",
        )
        pairs_after_first = [
            tuple(r)
            for r in store.execute(
                "SELECT number, id FROM revisions WHERE repo = ? ORDER BY id",
                (REPO,),
            )
        ]

        # Sentinel: another repo's revision row with a high explicit id,
        # planted BETWEEN the adoptions. Slug-scoped operations must never
        # touch it, and it makes any delete+reinsert of the slug's rows
        # reassign ids above it (rowid = max+1), so a rewrite cannot hide
        # behind coincidental id reuse.
        store.execute(
            "INSERT INTO revisions(id, repo, number, entity, origin, content,"
            " valid_from, valid_to) VALUES (?, 'other/repo', 1, 'item_body',"
            " 'edit', 'SENTINEL', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')",
            (1_000_000,),
        )
        store.commit()

        consolidate(self.store_db, self.candidate_db, REPO)
        after_second = store.execute(
            "SELECT COUNT(*) FROM revisions WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertEqual(
            after_second,
            2,
            f"second adoption of the SAME candidate must leave the row count"
            f" unchanged (no new rows, none deleted); got {after_second}",
        )
        pairs_after_second = [
            tuple(r)
            for r in store.execute(
                "SELECT number, id FROM revisions WHERE repo = ? ORDER BY id",
                (REPO,),
            )
        ]
        self.assertEqual(
            pairs_after_second,
            pairs_after_first,
            f"REVISION-ROWIDS-CHANGED: second adoption of the SAME candidate"
            f" rewrote the slug's revision rows - after first adoption"
            f" {pairs_after_first}, after second {pairs_after_second}"
            f" (the contract is no new rows and no deletes, not just a"
            f" net-neutral count)",
        )
        sentinel = store.execute(
            "SELECT COUNT(*) FROM revisions WHERE repo = 'other/repo'"
            " AND content = 'SENTINEL'"
        ).fetchone()[0]
        self.assertEqual(
            sentinel,
            1,
            "REVISION-ROWIDS-CHANGED: the other-repo sentinel row did not"
            " survive the second adoption (cross-repo isolation broken)",
        )
        store.close()


class RevisionPreservationTests(unittest.TestCase):
    """40-AC1/AC2: a candidate's empty or partial revisions table is an
    observation, not the truth - items it never observed keep their
    stored history."""

    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        self.store_db = Path(td.name) / "store.db"
        self.candidate_db = Path(td.name) / "candidate.db"

    def _seed(self, db_path, rev_numbers):
        items = [
            (1000 + n, REPO, n, "issue", f"item {n}", "open", "{}")
            for n in (1, 2, 3)
        ]
        c = connect(db_path)
        init_schema(c)
        c.executemany(
            "INSERT INTO items(id, repo, number, kind, title, state, raw_json)"
            " VALUES (?,?,?,?,?,?,?)",
            items,
        )
        seq = 1
        for number in rev_numbers:
            c.execute(
                "INSERT INTO revisions(repo, number, entity, origin, content,"
                " valid_from, valid_to) VALUES (?, ?, 'item_body', 'edit', ?,"
                " ?, ?)",
                (REPO, number, f"STORE-{seq}",
                 "2026-01-0%dT00:00:00Z" % min(seq, 9),
                 "2026-01-1%dT00:00:00Z" % min(seq, 9)),
            )
            seq += 1
        c.commit()
        return c

    def test_empty_candidate_keeps_stored_history(self):
        """40-AC1, RED at base (base leaves 0 rows), GREEN after the fix.
        The candidate carries zero revision rows (a pre-v8 candidate
        migrated in by _MigratedCopy is exactly this shape, empty by
        construction), so nothing it never observed may be deleted."""
        from zaxbygraph.doctor import consolidate

        store = self._seed(self.store_db, (2, 2))
        cand = self._seed(self.candidate_db, ())
        before = store.execute(
            "SELECT number, id FROM revisions WHERE repo = ? ORDER BY id",
            (REPO,),
        ).fetchall()
        cand.close()

        consolidate(self.store_db, self.candidate_db, REPO)
        after = store.execute(
            "SELECT number, id FROM revisions WHERE repo = ? ORDER BY id",
            (REPO,),
        ).fetchall()
        self.assertEqual(
            [tuple(r) for r in after],
            [tuple(r) for r in before],
            "EMPTY-CANDIDATE-HISTORY-LOST: adopting a zero-revision candidate"
            f" changed stored revision rows from {before} to {after}",
        )
        store.close()

    def test_partial_candidate_replaces_only_observed_items(self):
        """40-AC2, RED at base (base wipes item 3's rows), GREEN after the
        fix. The candidate observes item 1 only: item 1's history is
        replaced by the candidate's rows (a real observation), item 2
        (covered, no rows either side) is untouched, and item 3's history -
        which the candidate never observed - is preserved."""
        from zaxbygraph.doctor import consolidate

        store = self._seed(self.store_db, (3, 3))
        item3_before = store.execute(
            "SELECT id FROM revisions WHERE repo = ? AND number = 3"
            " ORDER BY id",
            (REPO,),
        ).fetchall()
        cand = self._seed(self.candidate_db, ())
        cand.execute(
            "INSERT INTO revisions(repo, number, entity, origin, content,"
            " valid_from, valid_to) VALUES (?, 1, 'item_body', 'edit', ?,"
            " ?, ?)",
            (REPO, "CAND-1", "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z"),
        )
        cand.commit()
        cand.close()

        consolidate(self.store_db, self.candidate_db, REPO)
        numbers = [
            r[0] for r in store.execute(
                "SELECT DISTINCT number FROM revisions WHERE repo = ?",
                (REPO,),
            )
        ]
        item3_after = store.execute(
            "SELECT id FROM revisions WHERE repo = ? AND number = 3"
            " ORDER BY id",
            (REPO,),
        ).fetchall()
        self.assertIn(
            3, numbers,
            "PARTIAL-CANDIDATE-HISTORY-LOST: the candidate never observed"
            f" item 3, yet its revision rows vanished (numbers left: {numbers})",
        )
        self.assertEqual(
            [tuple(r) for r in item3_after],
            [tuple(r) for r in item3_before],
            "item 3's preserved rows must keep their ids",
        )
        self.assertIn(
            1, numbers, "the covered item's candidate rows must adopt"
        )
        store.close()

    def test_adoption_reports_preserved_revision_counts(self):
        """40-AC4, NEW-SURFACE (the doctor result payload carries no
        revision fields at base). The doctor envelope reports the adopted
        and preserved counts; a post-v11 candidate's recorded fetch_source
        adopts with its sync_state row (issue #48 carry-through)."""
        from zaxbygraph.doctor import doctor

        store = self._seed(self.store_db, (3, 3))
        store_total = store.execute(
            "SELECT COUNT(*) FROM revisions WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        store.close()
        cand = self._seed(self.candidate_db, ())
        cand.execute(
            "INSERT INTO revisions(repo, number, entity, origin, content,"
            " valid_from, valid_to) VALUES (?, 1, 'item_body', 'edit', ?,"
            " ?, ?)",
            (REPO, "CAND-1", "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z"),
        )
        cand.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches,"
            " full_sync_pending, fetch_source)"
            " VALUES (?, '2026-03-01T00:00:00Z', '2026-03-01T00:00:00Z',"
            " '2026-03-01T00:00:00Z', NULL, 0, 0, 'rest')",
            (REPO,),
        )
        cand.commit()
        cand.close()

        result = doctor(
            None, REPO, self.store_db,
            extra_scans=[self.candidate_db], consolidate_flag=True,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertIn(
            "revisions_adopted", result,
            "REPORT-FIELD-MISSING: the doctor result payload must report the"
            f" adopted revision count; keys: {sorted(result)}",
        )
        self.assertIn(
            "revisions_preserved", result,
            "REPORT-FIELD-MISSING: the doctor result payload must report the"
            f" preserved revision count; keys: {sorted(result)}",
        )
        self.assertEqual(result["revisions_adopted"], 1)
        # Preserved = stored rows for numbers the candidate never observed
        # (item 3's two rows); the candidate's covered item-1 row is adopted.
        self.assertEqual(result["revisions_preserved"], store_total)
        # fetch_source carry-through (issue #48): the candidate recorded rest.
        s2 = connect(self.store_db)
        try:
            self.assertEqual(
                s2.execute(
                    "SELECT fetch_source FROM sync_state WHERE repo = ?",
                    (REPO,),
                ).fetchone()[0],
                "rest",
                "a post-v11 candidate's recorded fetch_source must adopt with"
                " its sync_state row",
            )
        finally:
            s2.close()

    def test_zero_revision_candidate_reports_full_preservation(self):
        """40-AC4 zero-case: adopting a zero-revision candidate reports
        preserved=<store total>, adopted=0."""
        from zaxbygraph.doctor import doctor

        store = self._seed(self.store_db, (2, 2))
        store_total = store.execute(
            "SELECT COUNT(*) FROM revisions WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        store.close()
        cand = self._seed(self.candidate_db, ())
        cand.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches,"
            " full_sync_pending)"
            " VALUES (?, '2026-03-01T00:00:00Z', '2026-03-01T00:00:00Z',"
            " '2026-03-01T00:00:00Z', NULL, 0, 0)",
            (REPO,),
        )
        cand.commit()
        cand.close()
        result = doctor(
            None, REPO, self.store_db,
            extra_scans=[self.candidate_db], consolidate_flag=True,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["revisions_adopted"], 0)
        self.assertEqual(result["revisions_preserved"], store_total)

    def test_covered_item_differing_only_in_valid_to_is_replaced(self):
        """Reviewer round 1, Finding 2: the payload comparison spans all
        seven columns (entity, entity_id, kind, origin, content,
        valid_from, valid_to). A covered item whose candidate rows differ
        from the stored rows ONLY in valid_to is REPLACED, not skipped -
        narrowing the comparison to content alone survives the suite
        (oracle-breadth mutant of record in 08b) and this test kills it."""
        from zaxbygraph.doctor import consolidate

        store = connect(self.store_db)
        init_schema(store)
        store.executemany(
            "INSERT INTO items(id, repo, number, kind, title, state, raw_json)"
            " VALUES (?,?,?,?,?,?,?)",
            [(1001, REPO, 1, "issue", "item 1", "open", "{}")],
        )
        store.execute(
            "INSERT INTO revisions(repo, number, entity, origin, content,"
            " valid_from, valid_to) VALUES (?, 1, 'item_body', 'edit', ?,"
            " ?, ?)",
            (REPO, "STORE-A", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
        )
        store.commit()

        cand = connect(self.candidate_db)
        init_schema(cand)
        cand.executemany(
            "INSERT INTO items(id, repo, number, kind, title, state, raw_json)"
            " VALUES (?,?,?,?,?,?,?)",
            [(1001, REPO, 1, "issue", "item 1", "open", "{}")],
        )
        # Identical except valid_to (2026-09-09 vs the store's 2026-01-02).
        cand.execute(
            "INSERT INTO revisions(repo, number, entity, origin, content,"
            " valid_from, valid_to) VALUES (?, 1, 'item_body', 'edit', ?,"
            " ?, ?)",
            (REPO, "STORE-A", "2026-01-01T00:00:00Z", "2026-09-09T00:00:00Z"),
        )
        cand.commit()
        cand.close()

        consolidate(self.store_db, self.candidate_db, REPO)
        valid_to = store.execute(
            "SELECT valid_to FROM revisions WHERE repo = ? AND number = 1",
            (REPO,),
        ).fetchone()[0]
        self.assertEqual(
            valid_to, "2026-09-09T00:00:00Z",
            "COMPARISON-NARROWED: a covered item differing only in valid_to"
            " must be replaced (the seven-column payload comparison was"
            " narrowed; content-only mutants survive without this pin)",
        )
        store.close()


if __name__ == "__main__":
    unittest.main()
