"""Regression and preservation tests for derived claim observation ages."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import zaxbygraph.cli as cli_mod
from fixtures import REPO, comment, issue, ts
from test_claims_targeted_anchor import (
    CLAIM_BODY,
    CLAIMED,
    CONTROL,
    T0,
    _NumberedSource,
    _TargetedRefreshBase,
)
from test_mcp import McpSession, pin_env_for, tool_envelope
from zaxbygraph.cli import main
from zaxbygraph.claims import (
    _store_claim_rows,
    claims_gate_by_number,
    claims_listing,
    collect_claims,
    load_claims_config,
)
from zaxbygraph.store import mark_sync_finished
from zaxbygraph.sync import sync_repo

HOUR = 3600
LISTING_FINISH = ts(0)
LAGGING_REFRESH_FINISH = ts(300)
LATER_LISTING_FINISH = ts(700)
OBSERVATION_SQL = (
    "SELECT fetched_at FROM fetch_log"
    " WHERE repo = ? AND resource = 'item' AND resource_id = ?"
    " AND http_status = 200 AND note IS NULL ORDER BY id DESC LIMIT 1"
)
FETCH_ROWS_SQL = (
    "SELECT fetched_at FROM fetch_log"
    " WHERE repo = ? AND resource = 'item' AND resource_id = ?"
    " AND http_status = 200 AND note IS NULL ORDER BY id"
)
ISSUE_ROWS = (
    " FROM fetch_log WHERE repo = ? AND resource = 'item' AND resource_id = ?"
    " AND http_status = 200 AND note IS NULL"
)
STAMP_AGE = round(-600 / HOUR, 4)
FETCH_AGE = round(60 / HOUR, 4)
EXPIRED_STAMP_AGE = round(-10 / HOUR, 4)
EXPIRED_FETCH_AGE = round((111 * HOUR + 340) / HOUR, 4)
ITEM_WHERE = (
    "WHERE repo = ? AND resource = 'item' AND resource_id = ?"
    " AND http_status = 200 AND note IS NULL"
)
TRUE_AGE = round((111 * HOUR + 340) / HOUR, 4)
CLAIM_A_AT = ts(600)
CLAIM_B_AT = ts(1100)
MEMO_ISSUE_UPDATED_AT = ts(650)
MEMO_REFRESH_FINISH = ts(660)
AGE_A = round(60 / HOUR, 4)
AGE_B_FROM_STAMP = round(-1100 / HOUR, 4)

SCENARIOS = {
    "ordinary": dict(
        claim_at=ts(600), issue_updated_at=ts(650), refresh_finish=ts(660),
        fetch_after=True, updated_after=True, active=True,
        age=round(60 / HOUR, 4), gated=True,
    ),
    "lag_latest_event": dict(
        claim_at=ts(600), issue_updated_at=ts(600), refresh_finish=ts(300),
        fetch_after=False, updated_after=True, active=True,
        age=round(0 / HOUR, 4), gated=True,
    ),
    "lag_later_modification": dict(
        claim_at=ts(600), issue_updated_at=ts(610), refresh_finish=ts(300),
        fetch_after=False, updated_after=True, active=True,
        age=round(10 / HOUR, 4), gated=True,
    ),
    "race_ordinary_clock": dict(
        claim_at=ts(600), issue_updated_at=ts(590), refresh_finish=ts(660),
        fetch_after=True, updated_after=False, active=True,
        age=round(60 / HOUR, 4), gated=True,
    ),
    "race_under_lag": dict(
        claim_at=ts(600), issue_updated_at=ts(590), refresh_finish=ts(300),
        fetch_after=False, updated_after=False, active=False,
        age=round(-600 / HOUR, 4), gated=False,
    ),
    "expired": dict(
        claim_at=ts(10), issue_updated_at=ts(20),
        refresh_finish=ts(111 * HOUR + 350),
        fetch_after=True, updated_after=True, active=False,
        age=round((111 * HOUR + 340) / HOUR, 4), gated=False,
    ),
}


class CandidateHScenarioTests(_TargetedRefreshBase):
    def _check(self, name: str) -> None:
        s = SCENARIOS[name]
        tag = "H:" + name
        self._build_store(
            claim_at=s["claim_at"],
            issue_updated_at=s["issue_updated_at"],
            refresh_finish=s["refresh_finish"],
        )
        self._assert_claim_stored_and_trusted()
        newest_fetch = self.conn.execute(OBSERVATION_SQL, (REPO, str(CLAIMED))).fetchone()[0]
        claim_created = self._stored_claim_rows()[0]["created_at"]

        # The stored facts, read back from the store.
        self.assertEqual(newest_fetch, s["refresh_finish"], f"{tag}: the logged fetch time")
        self.assertEqual(claim_created, s["claim_at"], f"{tag}: the stored claim time")
        self.assertEqual(self.issue_updated_at, s["issue_updated_at"], f"{tag}: the stored updated_at")
        self.assertGreater(claim_created, self.listing_stamp, f"{tag}: the claim postdates the stamp")
        # The premises the scenario is built to exercise.
        if s["fetch_after"]:
            self.assertLessEqual(claim_created, newest_fetch, f"{tag}: the fetch is at or after the claim")
        else:
            self.assertGreater(claim_created, newest_fetch, f"{tag}: the fetch predates the claim")
        if s["updated_after"]:
            self.assertLessEqual(claim_created, self.issue_updated_at, f"{tag}: updated_at is at or after the claim")
        else:
            self.assertGreater(claim_created, self.issue_updated_at, f"{tag}: updated_at predates the claim")

        entry = self._claim_entry(self._claims_listing(), tag=tag)
        self.assertIs(entry.get("active"), s["active"], f"{tag}: activeness; entry={entry}")
        self.assertAlmostEqual(
            entry.get("age_hours"), s["age"], places=4,
            msg=f"{tag}: the age must be the exact expected one; entry={entry}",
        )
        gate = self._triage_payload().get("gated") or {}
        gated = CLAIMED in ((gate.get("by_gate") or {}).get("claimed") or [])
        self.assertIs(gated, s["gated"], f"{tag}: the claimed gate; gate={gate}")

    def test_ordinary(self) -> None:
        self._check("ordinary")

    def test_lag_latest_event(self) -> None:
        self._check("lag_latest_event")

    def test_lag_later_modification(self) -> None:
        self._check("lag_later_modification")

    def test_race_ordinary_clock(self) -> None:
        self._check("race_ordinary_clock")

    def test_race_under_lag(self) -> None:
        self._check("race_under_lag")

    def test_expired(self) -> None:
        self._check("expired")


class TwoAnchorBoundaryTests(_TargetedRefreshBase):
    CLAIM_AT = ts(600)
    ISSUE_UPDATED_AT = ts(610)

    def test_claim_after_older_fetch_but_not_after_stamp_follows_the_stamp(self) -> None:
        src = _NumberedSource()
        src.add_issue(
            issue(
                CLAIMED,
                title="two-anchor boundary",
                state="open",
                created_at=ts(-7200),
                updated_at=ts(-3600),
                comments=0,
            )
        )
        with patch("zaxbygraph.store.utcnow", return_value=LISTING_FINISH):
            sync_repo(self.conn, src, REPO)
        rec = comment(9001, CLAIM_BODY, author="alice", created_at=self.CLAIM_AT)
        rec["author_association"] = "MEMBER"
        rec["html_url"] = f"https://github.com/{REPO}/issues/{CLAIMED}#issuecomment-9001"
        src.issue_comments.setdefault(CLAIMED, []).append(rec)
        src.issues[CLAIMED]["comments"] = 1
        src.issues[CLAIMED]["updated_at"] = self.ISSUE_UPDATED_AT
        with patch("zaxbygraph.store.utcnow", return_value=LAGGING_REFRESH_FINISH):
            sync_repo(self.conn, src, REPO, numbers=[CLAIMED])
        with patch("zaxbygraph.store.utcnow", return_value=LATER_LISTING_FINISH):
            mark_sync_finished(self.conn, REPO)
        self.conn.commit()
        self._assert_claim_stored_and_trusted()

        stamp_row = self.conn.execute(
            "SELECT last_incr_sync_at, last_full_sync_at FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()
        stamp = stamp_row["last_incr_sync_at"] or stamp_row["last_full_sync_at"]
        newest_fetch = self.conn.execute(OBSERVATION_SQL, (REPO, str(CLAIMED))).fetchone()[0]
        claim_created = self._stored_claim_rows()[0]["created_at"]

        # Premises, read back from the store: newer than the fetch only.
        self.assertEqual(stamp, LATER_LISTING_FINISH)
        self.assertEqual(newest_fetch, LAGGING_REFRESH_FINISH)
        self.assertGreater(claim_created, newest_fetch, "AC-PRE: the claim postdates the fetch")
        self.assertLessEqual(claim_created, stamp, "AC-PRE: the claim does not postdate the stamp")

        entry = self._claim_entry(self._claims_listing(), tag="BOUNDARY")
        self.assertIs(
            entry.get("active"),
            True,
            "BOUNDARY: a claim dated after an older fetch but not after the listing "
            f"stamp is governed by the stamp and must read active; entry={entry}",
        )
        self.assertAlmostEqual(
            entry.get("age_hours"),
            round((700 - 600) / 3600.0, 4),
            places=4,
            msg=f"BOUNDARY: the age must be measured from the stamp; entry={entry}",
        )
        gate = self._triage_payload().get("gated") or {}
        self.assertIn(
            CLAIMED,
            (gate.get("by_gate") or {}).get("claimed") or [],
            "BOUNDARY: the active claim must gate its issue",
        )

FIRST_REFRESH_FINISH = ts(900)
SECOND_REFRESH_FINISH = ts(300)
EXPECTED_AGE = round(10 / HOUR, 4)


class BackwardsClockTests(_TargetedRefreshBase):
    CLAIM_AT = ts(600)
    ISSUE_UPDATED_AT = ts(610)

    def _add_claim(self, src: _NumberedSource) -> None:
        rec = comment(9001, CLAIM_BODY, author="alice", created_at=self.CLAIM_AT)
        rec["author_association"] = "MEMBER"
        rec["html_url"] = f"https://github.com/{REPO}/issues/{CLAIMED}#issuecomment-9001"
        src.issue_comments.setdefault(CLAIMED, []).append(rec)
        src.issues[CLAIMED]["comments"] = 1
        src.issues[CLAIMED]["updated_at"] = self.ISSUE_UPDATED_AT

    def test_backwards_clock_does_not_hide_an_observed_claim(self) -> None:
        src = _NumberedSource()
        src.add_issue(
            issue(
                CLAIMED,
                title="backwards clock",
                state="open",
                created_at=ts(-7200),
                updated_at=ts(-3600),
                comments=0,
            )
        )
        with patch("zaxbygraph.store.utcnow", return_value=LISTING_FINISH):
            sync_repo(self.conn, src, REPO)
        self._add_claim(src)
        for finish in (FIRST_REFRESH_FINISH, SECOND_REFRESH_FINISH):
            with patch("zaxbygraph.store.utcnow", return_value=finish):
                result = sync_repo(self.conn, src, REPO, numbers=[CLAIMED])
            self.assertIsNone(result.get("last_error"), "AC-PRE: refresh must finish cleanly")
        self.conn.commit()
        self._assert_claim_stored_and_trusted()

        stamp_row = self.conn.execute(
            "SELECT last_incr_sync_at, last_full_sync_at FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()
        stamp = stamp_row["last_incr_sync_at"] or stamp_row["last_full_sync_at"]
        rows = [r[0] for r in self.conn.execute(FETCH_ROWS_SQL, (REPO, str(CLAIMED)))]
        newest_fetch = self.conn.execute(OBSERVATION_SQL, (REPO, str(CLAIMED))).fetchone()[0]
        claim_created = self._stored_claim_rows()[0]["created_at"]

        # Premises, read back from the store.
        self.assertEqual(rows[-2:], [FIRST_REFRESH_FINISH, SECOND_REFRESH_FINISH],
                         f"AC-PRE: two targeted fetches in the stated order; rows={rows}")
        self.assertGreater(max(rows), claim_created,
                           "AC-PRE: an EARLIER fetch row carries a greater timestamp than the claim")
        self.assertGreater(claim_created, stamp, "AC-PRE: the claim postdates the listing stamp")
        self.assertEqual(newest_fetch, SECOND_REFRESH_FINISH)
        self.assertGreater(claim_created, newest_fetch,
                           "AC-PRE: the claim postdates the newest fetch row's timestamp")
        self.assertLessEqual(claim_created, self.issue_updated_at,
                             "AC-PRE: the payload's updated_at is at or after the claim")

        entry = self._claim_entry(self._claims_listing(), tag="CLOCK")
        self.assertIs(
            entry.get("active"), True,
            "CLOCK: a claim present in the payload whose updated_at is at or after it is "
            "observed whatever the local clock did, and must read active; "
            f"entry={entry}",
        )
        self.assertAlmostEqual(
            entry.get("age_hours"), EXPECTED_AGE, places=4,
            msg=f"CLOCK: the age is measured from updated_at, never from an older fetch "
            f"row's larger timestamp; entry={entry}",
        )
        gate = self._triage_payload().get("gated") or {}
        self.assertIn(
            CLAIMED, (gate.get("by_gate") or {}).get("claimed") or [],
            "CLOCK: the active claim must gate its issue",
        )

def _trusted_claim(created_at: str) -> dict:
    rec = comment(9001, CLAIM_BODY, author="alice", created_at=created_at)
    rec["author_association"] = "MEMBER"
    rec["html_url"] = f"https://github.com/{REPO}/issues/{CLAIMED}#issuecomment-9001"
    return rec


class ReAnchorPreservationTests(_TargetedRefreshBase):
    # -- states, each built through the production sync path ------------------

    def _proven(self) -> None:
        """The store holds a proof: a claim at T0+600, an issue payload whose
        updated_at is T0+650, a refresh logged at T0+660."""
        self._build_store(
            claim_at=ts(600), issue_updated_at=ts(650), refresh_finish=ts(660)
        )
        self._assert_claim_stored_and_trusted()
        self.cfg = load_claims_config()

    def _unproven(self) -> None:
        """The store holds no proof: a claim at T0+600, an issue payload whose
        updated_at is T0+590, a refresh logged at T0+300 (a clock that lags)."""
        self._build_store(
            claim_at=ts(600), issue_updated_at=ts(590), refresh_finish=ts(300)
        )
        self._assert_claim_stored_and_trusted()
        self.cfg = load_claims_config()

    def _eligible_fetch_rows(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*)" + ISSUE_ROWS, (REPO, str(CLAIMED))
        ).fetchone()[0]

    # -- readers --------------------------------------------------------------

    def _listing(self) -> dict:
        return claims_listing(self.conn, REPO, self.cfg)

    def _entry(self, claims: list, tag: str) -> dict:
        entries = [c for c in claims if c.get("number") == CLAIMED]
        self.assertEqual(
            len(entries), 1, f"{tag}: exactly one entry for the claimed issue; entries={entries}"
        )
        return entries[0]

    def _assert_stamp_governed(self, entry: dict, tag: str) -> None:
        self.assertIs(entry.get("active"), False, f"{tag}: must stay inactive; entry={entry}")
        self.assertAlmostEqual(
            entry.get("age_hours"), STAMP_AGE, places=4,
            msg=f"{tag}: the stamp governs, so the age is the stamp-based one; entry={entry}",
        )

    def _assert_no_age(self, entry: dict, tag: str) -> None:
        self.assertIs(entry.get("active"), False, f"{tag}: must stay inactive; entry={entry}")
        self.assertIsNone(
            entry.get("age_hours"),
            f"{tag}: with no anchor there is no age; the wall clock is never a substitute; entry={entry}",
        )

    # -- an explicit asof keeps the strict contract ---------------------------

    def test_explicit_asof_keeps_the_strict_contract(self) -> None:
        """Kills: a repair that always re-anchors, ignoring the derived-default guard."""
        self._proven()
        # Premise: the derived default does prove the observation.
        self.assertIs(
            self._entry(self._listing()["claims"], "ASOF").get("active"), True,
            "AC-PRE: the derived default must activate this claim",
        )
        self.assertIn(
            CLAIMED, claims_gate_by_number(self.conn, REPO, self.cfg),
            "AC-PRE: the derived default must gate this issue",
        )
        self._assert_stamp_governed(
            self._entry(claims_listing(self.conn, REPO, self.cfg, asof=T0)["claims"], "ASOF"),
            "ASOF",
        )
        self.assertNotIn(
            CLAIMED,
            claims_gate_by_number(self.conn, REPO, self.cfg, asof=T0),
            "ASOF: an explicit asof must not gate the issue",
        )

    def test_null_connection_never_looks_up(self) -> None:
        """Kills: a lookup that runs without the null-connection guard.  On the
        unfixed source this test fails with a TypeError (the keyword does not
        exist), so it is preservation, not proof of the fix."""
        self._proven()
        rows = _store_claim_rows(self.conn, REPO)
        full = collect_claims(None, REPO, rows, self.cfg, T0, reanchor_newer_rows=True)
        self._assert_stamp_governed(self._entry(full, "CONN"), "CONN")

    # -- no proof: the claim stays inactive -----------------------------------

    def test_no_fetch_row_and_no_payload_proof_stays_inactive(self) -> None:
        """Kills: a missing fetch row treated as an observation."""
        self._unproven()
        self.conn.execute(
            "DELETE FROM fetch_log WHERE repo = ? AND resource = 'item' AND resource_id = ?",
            (REPO, str(CLAIMED)),
        )
        self.conn.commit()
        self.assertEqual(self._eligible_fetch_rows(), 0, "AC-PRE: no fetch row for the issue")
        self._assert_stamp_governed(self._entry(self._listing()["claims"], "NO-ROW"), "NO-ROW")
        self.assertNotIn(
            CLAIMED, claims_gate_by_number(self.conn, REPO, self.cfg), "NO-ROW: must gate nothing"
        )

    def test_unparseable_fetch_time_is_not_used(self) -> None:
        """Kills: an unparseable fetched_at replaced by a default observation."""
        self._unproven()
        self.conn.execute(
            "UPDATE fetch_log SET fetched_at = 'not-a-time' WHERE repo = ?"
            " AND resource = 'item' AND resource_id = ?",
            (REPO, str(CLAIMED)),
        )
        self.conn.commit()
        self.assertGreater(self._eligible_fetch_rows(), 0, "AC-PRE: the row is still eligible")
        self._assert_stamp_governed(self._entry(self._listing()["claims"], "BAD-TIME"), "BAD-TIME")

    def test_missing_fetch_table_fails_closed(self) -> None:
        """Kills: a lookup that lets the failed query raise."""
        self._unproven()
        self.conn.execute("DROP TABLE fetch_log")
        self.conn.commit()
        self._assert_stamp_governed(self._entry(self._listing()["claims"], "NO-TABLE"), "NO-TABLE")

    def test_foreign_marked_and_failed_rows_supply_no_anchor(self) -> None:
        """Kills: dropping the repo, the resource, the issue, the status or
        the note predicate of the fetch-row lookup (one mutant per predicate)."""
        self._unproven()
        self.conn.execute(
            "DELETE FROM fetch_log WHERE repo = ? AND resource = 'item' AND resource_id = ?",
            (REPO, str(CLAIMED)),
        )
        # Each seed satisfies every predicate but one.  All are dated T0+660,
        # later than the claim, so any predicate the lookup forgets to apply
        # lets its seed supply the anchor.
        seeds = [
            ("other/repo", "item", str(CLAIMED), ts(660), 200, None),
            (REPO, "item", str(CONTROL), ts(660), 200, None),
            (REPO, "item", str(CLAIMED), ts(660), 500, None),
            (REPO, "item", str(CLAIMED), ts(660), 200, "Introspection fields may only be used 2 times"),
            (REPO, "listing", str(CLAIMED), ts(660), 200, None),
        ]
        self.conn.executemany(
            "INSERT INTO fetch_log(repo, resource, resource_id, fetched_at, http_status, note)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            seeds,
        )
        self.conn.commit()
        self.assertEqual(self._eligible_fetch_rows(), 0, "AC-PRE: no eligible row for the issue")
        # The listing sync logs rows of its own; the five seeds are the only
        # rows dated after the claim.
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM fetch_log WHERE fetched_at = ?", (ts(660),)
            ).fetchone()[0],
            5,
            "AC-PRE: the five seeds are the only rows dated after the claim",
        )
        self._assert_stamp_governed(self._entry(self._listing()["claims"], "FOREIGN"), "FOREIGN")

    # -- no anchor: there is no age -------------------------------------------

    def test_missing_stamp_row_has_no_anchor(self) -> None:
        """Kills: a missing stamp replaced by the wall clock."""
        self._proven()
        self.conn.execute("DELETE FROM sync_state WHERE repo = ?", (REPO,))
        self.conn.commit()
        self._assert_no_age(self._entry(self._listing()["claims"], "NO-STAMP"), "NO-STAMP")

    def test_null_stamps_have_no_anchor(self) -> None:
        """Kills: null stamps replaced by the wall clock."""
        self._proven()
        self.conn.execute(
            "UPDATE sync_state SET last_incr_sync_at = NULL, last_full_sync_at = NULL"
            " WHERE repo = ?",
            (REPO,),
        )
        self.conn.commit()
        self._assert_no_age(self._entry(self._listing()["claims"], "NULL-STAMP"), "NULL-STAMP")

    def test_malformed_stamp_has_no_anchor(self) -> None:
        """Kills: an unparseable stamp replaced by the wall clock."""
        self._proven()
        self.conn.execute(
            "UPDATE sync_state SET last_incr_sync_at = 'garbage', last_full_sync_at = NULL"
            " WHERE repo = ?",
            (REPO,),
        )
        self.conn.commit()
        self._assert_no_age(self._entry(self._listing()["claims"], "BAD-STAMP"), "BAD-STAMP")

    # -- a proof is used ------------------------------------------------------

    def test_coherent_replacement_history_is_used(self) -> None:
        """Kills: a repair that never consults the fetch history (it would
        always fall back to the stamp)."""
        self._proven()
        # Replace the issue's fetch history outright, as a consolidation or a
        # snapshot pull does: the carried-over row is intact and usable.
        self.conn.execute(
            "DELETE FROM fetch_log WHERE repo = ? AND resource = 'item' AND resource_id = ?",
            (REPO, str(CLAIMED)),
        )
        self.conn.execute(
            "INSERT INTO fetch_log(repo, resource, resource_id, fetched_at, http_status, note)"
            " VALUES (?, 'item', ?, ?, 200, NULL)",
            (REPO, str(CLAIMED), ts(660)),
        )
        self.conn.commit()
        self.assertEqual(self._eligible_fetch_rows(), 1, "AC-PRE: one carried-over row")
        entry = self._entry(self._listing()["claims"], "CARRIED")
        self.assertIs(entry.get("active"), True, f"CARRIED: usable history must be used; entry={entry}")
        self.assertAlmostEqual(
            entry.get("age_hours"), FETCH_AGE, places=4,
            msg=f"CARRIED: the age is measured from the carried-over fetch; entry={entry}",
        )

    def test_claim_at_or_before_the_stamp_keeps_its_exact_age(self) -> None:
        """Kills: a repair that re-anchors every claim (candidate B's shape)."""
        src = _NumberedSource()
        src.add_issue(
            issue(
                CLAIMED,
                title="pre-stamp claim",
                state="open",
                created_at=ts(-4 * HOUR),
                updated_at=ts(-2 * HOUR),
                comments=0,
            )
        )
        src.issue_comments.setdefault(CLAIMED, []).append(_trusted_claim(ts(-2 * HOUR)))
        src.issues[CLAIMED]["comments"] = 1
        with patch("zaxbygraph.store.utcnow", return_value=T0):
            sync_repo(self.conn, src, REPO)
        # Two hours later the issue is fetched again; the listing stamp does not move.
        with patch("zaxbygraph.store.utcnow", return_value=ts(2 * HOUR)):
            result = sync_repo(self.conn, src, REPO, numbers=[CLAIMED])
        self.conn.commit()
        self.assertIsNone(result.get("last_error"), "AC-PRE: the refresh must finish cleanly")
        self._assert_claim_stored_and_trusted()
        self.cfg = load_claims_config()
        stamp_row = self.conn.execute(
            "SELECT last_incr_sync_at, last_full_sync_at FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()
        stamp = stamp_row["last_incr_sync_at"] or stamp_row["last_full_sync_at"]
        self.assertEqual(stamp, T0, "AC-PRE: the listing stamp")
        entry = self._entry(self._listing()["claims"], "PRE-STAMP")
        self.assertIs(entry.get("active"), True, f"PRE-STAMP: a two-hour-old claim is active; entry={entry}")
        self.assertAlmostEqual(
            entry.get("age_hours"), 2.0, places=4,
            msg=f"PRE-STAMP: the age is exactly the stamp-based one; entry={entry}",
        )

    def test_snapshot_before_the_claim_lists_and_gates_nothing(self) -> None:
        """Kills: a snapshot that copies comments created after its instant
        (evaluate.prepare_snapshot dropping its created_at <= T filter)."""
        self._proven()
        envelope = self._run_cli(
            [
                "triage", "--as-of", ts(300), "--include-gated", "--repo", REPO,
                "--db", str(self.db_path), "--format", "json",
            ]
        )
        data = envelope.get("data") or {}
        ranked = [r.get("number") for r in data["rows"] if r.get("ranked")]
        self.assertIn(
            CLAIMED, ranked,
            f"SNAPSHOT: before the claim existed the issue is unclaimed work; ranked={ranked}",
        )
        row = next(r for r in data["rows"] if r.get("number") == CLAIMED)
        self.assertNotIn(
            "claimed", row.get("gates") or [], f"SNAPSHOT: no claimed gate; row={row}"
        )
        by_gate = (data.get("gated") or {}).get("by_gate") or {}
        self.assertNotIn(CLAIMED, by_gate.get("claimed") or [], "SNAPSHOT: gates nothing")


class UpdatedAtProofTests(_TargetedRefreshBase):
    """A claim at T0+600, an issue payload whose updated_at is T0+650, a
    refresh logged at T0+660: both proofs exist before each test edits one."""

    def _state(self) -> None:
        self._build_store(
            claim_at=ts(600), issue_updated_at=ts(650), refresh_finish=ts(660)
        )
        self._assert_claim_stored_and_trusted()
        self.cfg = load_claims_config()

    def _drop_fetch_rows(self) -> None:
        self.conn.execute(
            "DELETE FROM fetch_log WHERE repo = ? AND resource = 'item' AND resource_id = ?",
            (REPO, str(CLAIMED)),
        )
        self.conn.commit()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*)" + ISSUE_ROWS, (REPO, str(CLAIMED))).fetchone()[0],
            0, "AC-PRE: no fetch row for the issue",
        )

    def _set_updated_at(self, value) -> None:
        self.conn.execute(
            "UPDATE items SET updated_at = ? WHERE repo = ? AND number = ? AND kind = 'issue'",
            (value, REPO, CLAIMED),
        )
        self.conn.commit()
        stored = self.conn.execute(
            "SELECT updated_at FROM items WHERE repo = ? AND number = ?", (REPO, CLAIMED)
        ).fetchone()["updated_at"]
        self.assertEqual(stored, value, "AC-PRE: the stored updated_at")

    def _entry(self, tag: str) -> dict:
        claims = claims_listing(self.conn, REPO, self.cfg)["claims"]
        entries = [c for c in claims if c.get("number") == CLAIMED]
        self.assertEqual(len(entries), 1, f"{tag}: exactly one entry; entries={entries}")
        return entries[0]

    def test_null_updated_at_gives_no_proof(self) -> None:
        """Kills: a NULL updated_at treated as an observation."""
        self._state()
        self._drop_fetch_rows()
        self._set_updated_at(None)
        entry = self._entry("NULL-UPDATED")
        self.assertIs(entry.get("active"), False, f"NULL-UPDATED: must stay inactive; entry={entry}")
        self.assertAlmostEqual(
            entry.get("age_hours"), STAMP_AGE, places=4,
            msg=f"NULL-UPDATED: the stamp governs; entry={entry}",
        )

    def test_unparseable_updated_at_gives_no_proof(self) -> None:
        """Kills: an unparseable updated_at replaced by a default observation."""
        self._state()
        self._drop_fetch_rows()
        self._set_updated_at("not-a-time")
        entry = self._entry("BAD-UPDATED")
        self.assertIs(entry.get("active"), False, f"BAD-UPDATED: must stay inactive; entry={entry}")
        self.assertAlmostEqual(
            entry.get("age_hours"), STAMP_AGE, places=4,
            msg=f"BAD-UPDATED: the stamp governs; entry={entry}",
        )

    def test_fetch_row_proves_alone_when_updated_at_is_unusable(self) -> None:
        """Kills: a lookup that ignores the fetch row."""
        self._state()
        self._set_updated_at(None)
        entry = self._entry("FETCH-ALONE")
        self.assertIs(entry.get("active"), True, f"FETCH-ALONE: the fetch row proves; entry={entry}")
        self.assertAlmostEqual(
            entry.get("age_hours"), FETCH_AGE, places=4,
            msg=f"FETCH-ALONE: the age is measured from the fetch; entry={entry}",
        )


class _HistoryLossBase(_TargetedRefreshBase):
    def _proven(self, *, claim_at: str, issue_updated_at: str, refresh_finish: str) -> None:
        self._build_store(
            claim_at=claim_at, issue_updated_at=issue_updated_at, refresh_finish=refresh_finish
        )
        self._assert_claim_stored_and_trusted()
        self.cfg = load_claims_config()
        self.args = (REPO, str(CLAIMED))

    def _eligible_rows(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log " + ITEM_WHERE, self.args
        ).fetchone()[0]

    def _premises(self, tag: str) -> None:
        claim_created = self._stored_claim_rows()[0]["created_at"]
        self.assertGreater(claim_created, self.listing_stamp, f"{tag}: the claim postdates the stamp")
        self.assertLessEqual(
            claim_created, self.issue_updated_at,
            f"{tag}: the issue's stored updated_at is at or after the claim",
        )

    def _assert_inactive(self, tag: str, age: float, gated: bool = False) -> None:
        entry = self._claim_entry(self._claims_listing(), tag=tag)
        self.assertIs(entry.get("active"), False, f"{tag}: must stay inactive; entry={entry}")
        self.assertAlmostEqual(
            entry.get("age_hours"), age, places=4,
            msg=f"{tag}: the stamp governs, so the age is the stamp-based one; entry={entry}",
        )
        self.assertEqual(
            CLAIMED in claims_gate_by_number(self.conn, REPO, self.cfg), gated,
            f"{tag}: the claimed gate",
        )


class HistoryLossStaysInactiveTests(_HistoryLossBase):
    def test_no_fetch_row_with_a_qualifying_updated_at_stays_inactive(self) -> None:
        """Kills: candidate H, which counts updated_at without a logged fetch."""
        self._proven(claim_at=ts(600), issue_updated_at=ts(610), refresh_finish=ts(300))
        self._premises("NO-ROW")
        # Control: while the logged fetch exists the claim reads active.
        self.assertIs(self._claim_entry(self._claims_listing(), tag="NO-ROW").get("active"), True,
                      "NO-ROW: control, with the logged fetch present the claim is active")
        self.conn.execute("DELETE FROM fetch_log " + ITEM_WHERE, self.args)
        self.conn.commit()
        self.assertEqual(self._eligible_rows(), 0, "NO-ROW: no eligible fetch row for the issue")
        self._assert_inactive("NO-ROW", STAMP_AGE)

    def test_expired_claim_with_no_fetch_row_stays_inactive(self) -> None:
        """Kills: candidate H in its worst case, an expired claim reading active."""
        self._proven(
            claim_at=ts(10), issue_updated_at=ts(20), refresh_finish=ts(111 * HOUR + 350)
        )
        self._premises("EXPIRED")
        self.assertAlmostEqual(
            self._claim_entry(self._claims_listing(), tag="EXPIRED").get("age_hours"),
            EXPIRED_FETCH_AGE, places=4,
            msg="EXPIRED: control, while its fetch row exists the claim is 111 hours old",
        )
        self.conn.execute("DELETE FROM fetch_log " + ITEM_WHERE, self.args)
        self.conn.commit()
        self.assertEqual(self._eligible_rows(), 0, "EXPIRED: no eligible fetch row for the issue")
        self._assert_inactive("EXPIRED", EXPIRED_STAMP_AGE)

    def test_unreadable_fetch_time_counts_as_no_row(self) -> None:
        """Kills: a lookup that counts a logged fetch whose fetched_at is unparseable."""
        self._proven(claim_at=ts(600), issue_updated_at=ts(610), refresh_finish=ts(300))
        self._premises("UNREADABLE")
        self.conn.execute(
            "UPDATE fetch_log SET fetched_at = 'not-a-time' " + ITEM_WHERE, self.args
        )
        self.conn.commit()
        self._assert_inactive("UNREADABLE", STAMP_AGE)

    def test_missing_fetch_time_counts_as_no_row(self) -> None:
        """Kills: a lookup that counts a logged fetch whose fetched_at is empty."""
        self._proven(claim_at=ts(600), issue_updated_at=ts(610), refresh_finish=ts(300))
        self._premises("EMPTY")
        self.conn.execute("UPDATE fetch_log SET fetched_at = '' " + ITEM_WHERE, self.args)
        self.conn.commit()
        self._assert_inactive("EMPTY", STAMP_AGE)

    def test_missing_fetch_table_stays_inactive(self) -> None:
        """Kills: a lookup that lets the failed query raise, or that reads updated_at anyway."""
        self._proven(claim_at=ts(600), issue_updated_at=ts(610), refresh_finish=ts(300))
        self._premises("NO-TABLE")
        self.conn.execute("DROP TABLE fetch_log")
        self.conn.commit()
        self._assert_inactive("NO-TABLE", STAMP_AGE)


class ClockAndExpiryTests(_TargetedRefreshBase):
    CLAIM_AT = ts(10)
    ISSUE_UPDATED_AT = ts(20)

    def _state(self, refresh_finish: str) -> None:
        self._build_store(
            claim_at=self.CLAIM_AT, issue_updated_at=self.ISSUE_UPDATED_AT, refresh_finish=refresh_finish
        )
        self._assert_claim_stored_and_trusted()
        self.cfg = load_claims_config()
        window = float(self.cfg.get("window_hours", 72) or 0)
        self.assertEqual(window, 72.0, "AC-PRE: the claims window this file reasons with")

    def _entry(self, tag: str) -> dict:
        return self._claim_entry(self._claims_listing(), tag=tag)

    def _gated(self) -> bool:
        return CLAIMED in claims_gate_by_number(self.conn, REPO, self.cfg)

    def _assert_age(self, tag: str, age: float) -> None:
        self.assertAlmostEqual(
            self._entry(tag).get("age_hours"), age, places=4, msg=f"{tag}: the reported age"
        )

    def test_expiry_is_truthful_on_a_clock_that_is_not_behind(self) -> None:
        self._state(ts(111 * HOUR + 350))
        self.assertIs(self._entry("ON-TIME").get("active"), False, "ON-TIME: must be inactive")
        self._assert_age("ON-TIME", TRUE_AGE)
        self.assertIs(self._gated(), False, "ON-TIME: an expired claim gates nothing")

    def test_a_clock_two_hours_behind_keeps_a_claim_active_inside_the_window_plus_the_lag(self) -> None:
        """Edge one: true age 73 h, window plus lag 74 h. Late expiry, bounded."""
        self._state(ts(10 + 71 * HOUR))
        self.assertIs(
            self._entry("LAG-ACTIVE").get("active"), True,
            "LAG-ACTIVE: a claim whose understated age is inside the window reads active; "
            f"entry={self._entry('LAG-ACTIVE')}",
        )
        self._assert_age("LAG-ACTIVE", 71.0)
        self.assertIs(self._gated(), True, "LAG-ACTIVE: the active claim gates its issue")

    def test_on_the_same_clock_a_claim_past_the_window_plus_the_lag_reads_expired(self) -> None:
        """Edge two: true age 74.5 h, past the window plus the lag. Expiry is late
        by at most the lag and no longer."""
        self._state(ts(10 + int(72.5 * HOUR)))
        self.assertIs(
            self._entry("LAG-EXPIRED").get("active"), False,
            "LAG-EXPIRED: a claim older than the window plus the lag must read expired; "
            f"entry={self._entry('LAG-EXPIRED')}",
        )
        self._assert_age("LAG-EXPIRED", 72.5)
        self.assertIs(self._gated(), False, "LAG-EXPIRED: an expired claim gates nothing")

    def test_the_critics_executed_case_a_clock_111_hours_behind(self) -> None:
        self._state(ts(300))
        stored_updated = self.conn.execute(
            "SELECT updated_at FROM items WHERE repo = ? AND number = ?", (REPO, CLAIMED)
        ).fetchone()["updated_at"]
        self.assertEqual(stored_updated, self.ISSUE_UPDATED_AT, "AC-PRE: the stored updated_at")
        self.assertIs(
            self._entry("LAG-111H").get("active"), True,
            "LAG-111H: beside a readable logged fetch a claim whose understated age is inside "
            f"the window reads active; entry={self._entry('LAG-111H')}",
        )
        self._assert_age("LAG-111H", round(290 / HOUR, 4))
        self.assertIs(self._gated(), True, "LAG-111H: the active claim gates its issue")


class _CountingConn:
    """Delegates to the real connection and counts the proof lookups."""

    def __init__(self, conn) -> None:
        self._conn = conn
        self.fetch_queries = 0
        self.item_queries = 0

    def execute(self, sql, *args, **kwargs):
        if "FROM fetch_log" in sql:
            self.fetch_queries += 1
        if "updated_at FROM items" in sql:
            self.item_queries += 1
        return self._conn.execute(sql, *args, **kwargs)

def _trusted(cid: int, created_at: str) -> dict:
    rec = comment(cid, CLAIM_BODY, author="alice", created_at=created_at)
    rec["author_association"] = "MEMBER"
    rec["html_url"] = f"https://github.com/{REPO}/issues/{CLAIMED}#issuecomment-{cid}"
    return rec


class OneLookupPerIssueTests(_TargetedRefreshBase):
    def test_raw_proofs_are_cached_and_qualified_per_claim(self) -> None:
        src = _NumberedSource()
        src.add_issue(issue(CLAIMED, title="two claims", state="open", created_at=ts(-7200),
                            updated_at=ts(-3600), comments=0))
        src.add_issue(issue(CONTROL, title="control", state="open", created_at=ts(-7200),
                            updated_at=ts(-3500), comments=0))
        with patch("zaxbygraph.store.utcnow", return_value=LISTING_FINISH):
            sync_repo(self.conn, src, REPO)
        src.issue_comments[CLAIMED] = [_trusted(9001, CLAIM_A_AT), _trusted(9002, CLAIM_B_AT)]
        src.issues[CLAIMED]["comments"] = 2
        src.issues[CLAIMED]["updated_at"] = MEMO_ISSUE_UPDATED_AT
        with patch("zaxbygraph.store.utcnow", return_value=MEMO_REFRESH_FINISH):
            result = sync_repo(self.conn, src, REPO, numbers=[CLAIMED])
        self.conn.commit()
        self.assertIsNone(result.get("last_error"), "AC-PRE: the refresh must finish cleanly")
        stored = {
            r["github_id"]: r["created_at"]
            for r in self.conn.execute(
                "SELECT github_id, created_at FROM comments WHERE repo = ? AND number = ?",
                (REPO, CLAIMED),
            )
        }
        self.assertEqual(stored, {9001: CLAIM_A_AT, 9002: CLAIM_B_AT}, "AC-PRE: two stored claims")
        stored_updated = self.conn.execute(
            "SELECT updated_at FROM items WHERE repo = ? AND number = ?", (REPO, CLAIMED)
        ).fetchone()["updated_at"]
        self.assertEqual(stored_updated, MEMO_ISSUE_UPDATED_AT, "AC-PRE: the stored updated_at")
        cfg = load_claims_config()

        counting = _CountingConn(self.conn)
        data = claims_listing(counting, REPO, cfg)
        by_id = {c.get("comment_id"): c for c in data["claims"] if c.get("number") == CLAIMED}
        self.assertEqual(sorted(by_id), [9001, 9002], f"MEMO: both claims listed; {sorted(by_id)}")

        a, b = by_id[9001], by_id[9002]
        # A: the logged fetch qualifies.
        self.assertIs(a.get("active"), True, f"MEMO: claim A is active; entry={a}")
        self.assertAlmostEqual(a.get("age_hours"), AGE_A, places=4,
                               msg=f"MEMO: A's age is measured from the fetch; entry={a}")
        # B: beyond both proofs, so the stamp governs. A cache that stored A's
        # finished anchor would hand B an anchor of 12:11:00 and an age of
        # -0.1222 h.
        self.assertIs(b.get("active"), False, f"MEMO: claim B stays inactive; entry={b}")
        self.assertAlmostEqual(b.get("age_hours"), AGE_B_FROM_STAMP, places=4,
                               msg=f"MEMO: B keeps the stamp-based age; entry={b}")
        # One lookup per issue for the whole call.
        self.assertEqual(counting.fetch_queries, 1,
                         f"MEMO: exactly one fetch_log lookup for the issue; got {counting.fetch_queries}")
        self.assertEqual(counting.item_queries, 1,
                         f"MEMO: exactly one items lookup for the issue; got {counting.item_queries}")


class TargetedRefreshPreservationTests(_TargetedRefreshBase):
    """The observation anchor does not change release or trust semantics."""

    def test_released_targeted_claim_keeps_observation_age(self) -> None:
        self._build_store(
            claim_at=ts(600), issue_updated_at=ts(650), refresh_finish=ts(660)
        )
        self._assert_claim_stored_and_trusted()
        self.conn.execute(
            "INSERT INTO comments(github_id, repo, number, kind, author,"
            " author_association, created_at, updated_at, body, html_url, raw_json)"
            " VALUES (?, ?, ?, 'issue_comment', ?, ?, ?, ?, ?, ?, ?)",
            (
                9002, REPO, CLAIMED, "alice", "MEMBER", ts(700), ts(700),
                "**Claim released:** session=TARGETED-ANCHOR-TOKEN-0001 done.",
                f"https://github.com/{REPO}/issues/{CLAIMED}#issuecomment-9002",
                "{}",
            ),
        )
        self.conn.commit()
        entry = self._claim_entry(self._claims_listing(), tag="RELEASE-ANCHOR")
        self.assertIs(entry.get("ended_by_release"), True,
                      f"RELEASE-ANCHOR: release ordering ends the claim; entry={entry}")
        self.assertAlmostEqual(
            entry.get("age_hours"), round(60 / 3600, 4), places=4,
            msg=f"RELEASE-ANCHOR: age remains observation-based; entry={entry}",
        )

    def test_untrusted_targeted_claim_keeps_observation_age_and_never_gates(self) -> None:
        self._build_store(
            claim_at=ts(600), issue_updated_at=ts(650), refresh_finish=ts(660)
        )
        self.conn.execute(
            "UPDATE comments SET author_association = 'NONE'"
            " WHERE repo = ? AND number = ? AND github_id = 9001",
            (REPO, CLAIMED),
        )
        self.conn.commit()
        entry = self._claim_entry(self._claims_listing(), tag="UNTRUSTED-ANCHOR")
        self.assertIs(entry.get("trusted"), False,
                      f"UNTRUSTED-ANCHOR: trust remains false; entry={entry}")
        self.assertIs(entry.get("active"), False,
                      f"UNTRUSTED-ANCHOR: untrusted claim stays inactive; entry={entry}")
        self.assertAlmostEqual(
            entry.get("age_hours"), round(60 / 3600, 4), places=4,
            msg=f"UNTRUSTED-ANCHOR: age remains observation-based; entry={entry}",
        )
        self.assertNotIn(CLAIMED, claims_gate_by_number(self.conn, REPO),
                         "UNTRUSTED-ANCHOR: untrusted claim never gates")

    def test_only_claims_newer_than_stamp_are_reanchored(self) -> None:
        self._build_store(
            claim_at=ts(600), issue_updated_at=ts(650), refresh_finish=ts(660)
        )
        older_body = "**Agent claim:** session=OLDER-ANCHOR-TOKEN-00001 Working this issue now."
        self.conn.execute(
            "INSERT INTO comments(github_id, repo, number, kind, author,"
            " author_association, created_at, body, html_url, raw_json)"
            " VALUES (?, ?, ?, 'issue_comment', ?, ?, ?, ?, ?, ?)",
            (9002, REPO, CLAIMED, "alice", "MEMBER", ts(-120), older_body,
             f"https://github.com/{REPO}/issues/{CLAIMED}#issuecomment-9002", "{}"),
        )
        self.conn.commit()
        entries = {
            entry["comment_id"]: entry
            for entry in self._claims_listing()["claims"]
            if entry.get("number") == CLAIMED
        }
        self.assertEqual(set(entries), {9001, 9002}, "MIXED-ANCHOR: both claims are listed")
        self.assertAlmostEqual(entries[9001].get("age_hours"), round(60 / 3600, 4), places=4,
                               msg=f"MIXED-ANCHOR: newer claim uses observation; {entries[9001]}")
        self.assertAlmostEqual(entries[9002].get("age_hours"), round(120 / 3600, 4), places=4,
                               msg=f"MIXED-ANCHOR: older claim keeps stamp age; {entries[9002]}")

    def test_plain_sync_advances_stamp_and_governs_claim_age(self) -> None:
        self._build_store(
            claim_at=ts(600), issue_updated_at=ts(650), refresh_finish=ts(660)
        )
        src = _NumberedSource()
        src.add_issue(issue(CLAIMED, title="plain sync claim", state="open",
                            created_at=ts(-7200), updated_at=ts(650), comments=1))
        rec = comment(9001, CLAIM_BODY, author="alice", created_at=ts(600))
        rec["author_association"] = "MEMBER"
        rec["html_url"] = f"https://github.com/{REPO}/issues/{CLAIMED}#issuecomment-9001"
        src.issue_comments[CLAIMED] = [rec]
        with patch("zaxbygraph.store.utcnow", return_value=ts(700)):
            result = sync_repo(self.conn, src, REPO)
        self.assertIsNone(result.get("last_error"), "PLAIN-SYNC: listing sync finishes cleanly")
        self.conn.commit()
        stamp_row = self.conn.execute(
            "SELECT last_incr_sync_at, last_full_sync_at FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()
        stamp = stamp_row["last_incr_sync_at"] or stamp_row["last_full_sync_at"]
        self.assertEqual(stamp, ts(700), "PLAIN-SYNC: the listing stamp advances")
        entry = self._claim_entry(self._claims_listing(), tag="PLAIN-SYNC")
        self.assertAlmostEqual(
            entry.get("age_hours"), round(100 / 3600, 4), places=4,
            msg=f"PLAIN-SYNC: the advanced stamp governs age; entry={entry}",
        )


class TargetedRefreshSurfaceTests(_TargetedRefreshBase):
    def test_bundle_brief_and_mcp_surfaces_report_refreshed_claim(self) -> None:
        self._build_store(
            claim_at=ts(600), issue_updated_at=ts(650), refresh_finish=ts(660)
        )

        def _surface(argv: list[str]) -> dict:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                with patch.object(cli_mod, "_run_refresh_sync", return_value=0):
                    code = main(argv)
            self.assertEqual(code, 0, f"SURFACE-CLI: command succeeds; stderr={err.getvalue()}")
            envelope = json.loads(out.getvalue())
            self.assertTrue(envelope.get("ok"), f"SURFACE-CLI: successful envelope; {envelope}")
            return envelope.get("data") or {}

        bundle = _surface(
            ["bundle", "--repo", REPO, "--db", str(self.db_path), "--format", "json"]
        )
        self.assertNotIn(CLAIMED, bundle.get("pool") or [],
                         "SURFACE-BUNDLE: claimed issue is not in the pool")
        bundled_numbers = {
            number
            for group in (bundle.get("bundles") or [])
            for item in (group.get("items") or [])
            for number in [item.get("number") if isinstance(item, dict) else item]
        }
        self.assertNotIn(CLAIMED, bundled_numbers,
                         "SURFACE-BUNDLE: claimed issue is offered in no bundle")

        brief = _surface(
            ["brief", str(CLAIMED), "--repo", REPO, "--db", str(self.db_path), "--format", "json"]
        )
        self.assertIn("claimed", (brief.get("features") or {}).get("gates") or [],
                      "SURFACE-BRIEF: brief reports the claimed gate")

        pin_env_for(self, self.root, db=self.db_path)
        session = McpSession(
            self, cwd=str(self.root), repo=REPO, db=str(self.db_path), stale_after_s=10**9
        )
        self.addCleanup(session.stop)
        session.initialize()
        _result, claims_env = tool_envelope(
            self, session.call_tool("claims", {"repo": REPO}), "SURFACE-MCP-CLAIMS"
        )
        claims = (claims_env.get("data") or {}).get("claims") or []
        entry = next((c for c in claims if c.get("number") == CLAIMED), None)
        self.assertIsNotNone(entry, "SURFACE-MCP-CLAIMS: refreshed claim is returned")
        self.assertIs(entry.get("active"), True,
                      f"SURFACE-MCP-CLAIMS: refreshed claim is active; entry={entry}")

        _triage_result, triage_env = tool_envelope(
            self, session.call_tool("triage", {"repo": REPO, "include_gated": True}),
            "SURFACE-MCP-TRIAGE",
        )
        gated = ((triage_env.get("data") or {}).get("gated") or {}).get("by_gate") or {}
        self.assertIn(CLAIMED, gated.get("claimed") or [],
                      "SURFACE-MCP-TRIAGE: refreshed issue reports claimed gate")

        _brief_result, brief_env = tool_envelope(
            self, session.call_tool("brief", {"repo": REPO, "number": CLAIMED}),
            "SURFACE-MCP-BRIEF",
        )
        features = (brief_env.get("data") or {}).get("features") or {}
        self.assertIn("claimed", features.get("gates") or [],
                      "SURFACE-MCP-BRIEF: brief reports claimed gate")
        session.stop()

if __name__ == "__main__":
    unittest.main()
