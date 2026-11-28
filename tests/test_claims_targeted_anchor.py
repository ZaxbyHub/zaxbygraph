"""Acceptance checks for ZaxbyHub/zaxbygraph issue #100 -- the derived
claims tier's as-of anchor after a targeted refresh.

THE DEFECT (verified at the base commit c38e4c2, the anchor this file was
written against): the derived claims tier answers "is this claim active?"
as of the store's LISTING-COVERAGE stamp (``_sync_stamp`` ->
``last_incr_sync_at`` or ``last_full_sync_at``). A targeted refresh
(``sync --numbers N``) ingests comment rows but -- by repository
invariant -- never moves that stamp, so a claim posted AFTER the last
drained listing is dated after the store's own anchor: its age comes out
NEGATIVE, the ``active`` predicate fails, and the ``claimed`` gate stops
gating the issue -- ``triage`` offers it as unclaimed work until the next
plain ``sync``.

FROZEN CHECK NAMES (the published commands select them with ``-k``):

    AC1  test_targeted_refresh_makes_fresh_claim_active
    AC2  test_targeted_refresh_gates_its_issue
    AC5  test_claim_after_every_proof_stays_inactive
    AC8  test_claim_without_a_logged_fetch_stays_inactive

RED at base, GREEN after the fix. The base leg of AC1/AC2 fails on the
TAGGED assertion (the claim reads inactive / the claimed gate does not
fire), never on an import, a skip or a crash: every test first proves
the claim row is really in the store and really trusted.

CONTRACT THESE CHECKS PIN (the observable surface only -- the mechanism
that restores activeness is the implementer's choice):

- AC1: after the production path -- a drained listing sync finishing at
  T0, a trusted unreleased claim posted at T1 > T0 on an OPEN issue whose
  stored ``updated_at`` is at or after T1, then the real targeted refresh
  ``sync_repo(conn, src, REPO, numbers=[N])`` -- the derived ``claims``
  listing reports the claim ``active`` with a NON-NEGATIVE ``age_hours``.
- AC2: the same refreshed store, read through the real ``triage`` command:
  the refreshed issue is withheld from the ranked rows and named under
  ``gated.by_gate["claimed"]``. An issue carrying no claim stays ranked
  (the gate is selective, not a blanket veto).
- AC5 (the regression floor, GREEN at base and GREEN after any correct
  fix): a stored claim dated after the listing stamp, after the newest
  eligible ``fetch_log`` row of its issue in ingestion order AND after
  that issue's stored ``updated_at`` stays INACTIVE in the derived
  listing and gates nothing.
- AC8 (a second regression floor, GREEN at base and GREEN after any
  correct fix): a stored claim dated after the listing stamp whose issue
  has no eligible ``fetch_log`` row with a readable ``fetched_at`` stays
  INACTIVE and gates nothing, whatever the issue's stored ``updated_at``
  says.

AMENDED 2026-11-28 (UTC) by the owner's delegated decision on the
clock-skew contract (issue-tracer trace ``100-targeted-refresh-claim-``,
reason ``AC_CHANGED_BY_USER``): the AC5 class was replaced, and AC1's
exception was published. It first
pinned a claim dated after the stamp and its issue's ``updated_at``,
a fixture that is the ordinary shape of a fresh claim which raced the
issue-payload fetch, so it forbade the fix AC1 asks for. C1's and C2's
tests are unchanged.

AMENDED again 2026-11-28 (UTC) by the swarm-critic's follow-up ruling,
under the same delegation (reason ``AC_CHANGED_BY_USER``): AC1's exception
and AC5 gain one clause, and AC8 is added. The issue's stored
``updated_at`` may only repair the timestamp of a fetch the store has
LOGGED for that issue with a readable ``fetched_at``; with no such row
there is no proof, whatever ``updated_at`` says. Counting it alone let an
expired claim read active once the fetch history was lost. C1's, C2's and
C5's tests are unchanged; AC8's test is new and is green at the anchor.

The check set kills the tempting pseudo-fixes (a clamped age, a dropped
``0.0 <=`` lower bound), a listing-only repair that leaves the gate
skipping the row, and an over-broad repair that gates every open issue.

HERMETIC: ``FakeGitHubSource`` (tests/fixtures.py) only -- no network, no
``gh``, no credentials; a temporary store under a temporary directory; the
sync clocks pinned with ``patch("zaxbygraph.store.utcnow", ...)`` so no
assertion depends on the wall clock; the read path runs the real
``zaxbygraph.cli.main`` in-process, refresh seam stubbed out, so no
background sync can run.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fixtures import REPO, FakeGitHubSource, comment, issue, ts

import zaxbygraph.cli as cli_mod
from zaxbygraph.cli import main
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

#: The claim's token (16-64 chars of ``[A-Za-z0-9-]``): a parsed claim
#: needs a token to carry a session identity.
TOKEN = "TARGETED-ANCHOR-TOKEN-0001"

#: The claim body: matches the built-in ``Agent claim`` pattern and
#: carries the token above.
CLAIM_BODY = f"**Agent claim:** session={TOKEN} Working this issue now."

#: The issue the claim is posted on, and an untouched control issue.
CLAIMED = 5
CONTROL = 6

#: T0 -- the instant the drained listing sync finishes (the store's
#: listing-coverage stamp).
T0 = ts(0)
#: T1 -- the claim's creation instant (strictly after T0).
T1 = ts(60)
#: The owning issue's stored ``updated_at`` after the refresh: GitHub bumps
#: an issue's ``updated_at`` when a comment lands on it, so the stored
#: value is at or after the claim's creation (AC1's premise).
ISSUE_UPDATED_AT = ts(61)
#: The targeted refresh's own finish instant.
REFRESH_FINISH = ts(120)

#: AC5's floor: the claim postdates the listing stamp (T0), the logged
#: fetch of its issue and its own issue's stored ``updated_at``. The
#: refresh's clock lags the claim, and the payload it fetched predates
#: the claim (a comment that landed between the issue-payload request
#: and the comments request), so nothing the store holds proves an
#: observation at or after the claim.
AC5_CLAIM_AT = ts(600)
AC5_ISSUE_UPDATED_AT = ts(590)
AC5_REFRESH_FINISH = ts(300)

#: The observation lookup AC5's premise reads: the newest eligible
#: ``fetch_log`` row of the claim's issue in ingestion order.
OBSERVATION_SQL = (
    "SELECT fetched_at FROM fetch_log"
    " WHERE repo = ? AND resource = 'item' AND resource_id = ?"
    " AND http_status = 200 AND note IS NULL ORDER BY id DESC LIMIT 1"
)
#: A claim dated T0+600 against the listing stamp T0, in hours.
AC5_STAMP_AGE = round(-600 / 3600.0, 4)

#: AC8's state: an EXPIRED claim (created at T0+10, fetched 111 h later)
#: whose issue payload carries an ``updated_at`` at or after it, after the
#: issue's eligible fetch rows are gone (history replaced, or never kept).
AC8_CLAIM_AT = ts(10)
AC8_ISSUE_UPDATED_AT = ts(20)
AC8_REFRESH_FINISH = ts(111 * 3600 + 350)
#: The stamp-based age the claim must keep once the row is gone, in hours.
AC8_STAMP_AGE = round(-10 / 3600.0, 4)
#: The issue's eligible fetch rows: a successful item fetch, no note.
ELIGIBLE_FETCH_ROWS = (
    "WHERE repo = ? AND resource = 'item' AND resource_id = ?"
    " AND http_status = 200 AND note IS NULL"
)


class _NumberedSource(FakeGitHubSource):
    """A numbers-capable fake: ``get_issues`` serves exactly the requested
    numbers as ``(payload, status)`` tuples, so ``sync --numbers``
    exercises the same exact-number seam the production source uses."""

    def get_issues(self, numbers):
        out = {}
        for n in numbers:
            rec = self.issues.get(int(n))
            out[int(n)] = (deepcopy(rec), 200) if rec is not None else (None, 404)
        return out

    def probe_repository(self) -> bool:
        return True


class _TargetedRefreshBase(unittest.TestCase):
    """The store the three checks share: a drained listing sync, then
    the real targeted refresh that ingests the claim."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = self.root / "history.db"
        self.conn = connect(self.db_path)
        init_schema(self.conn)
        # The CLI reads its config from the CWD: run it in the scratch
        # root so no checkout-local config can reach the derived tier.
        self._saved_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(lambda: os.chdir(self._saved_cwd))

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    # -- the production path -------------------------------------------------

    def _build_store(
        self,
        *,
        claim_at: str,
        issue_updated_at: str,
        refresh_finish: str,
    ) -> None:
        """Listing sync at T0, then the claim, then the targeted refresh."""
        src = _NumberedSource()
        src.add_issue(
            issue(
                CLAIMED,
                title="targeted refresh claim",
                state="open",
                created_at=ts(-7200),
                updated_at=ts(-3600),
                comments=0,
            )
        )
        src.add_issue(
            issue(
                CONTROL,
                title="untouched control issue",
                state="open",
                created_at=ts(-7200),
                updated_at=ts(-3500),
                comments=0,
            )
        )
        with patch("zaxbygraph.store.utcnow", return_value=T0):
            sync_repo(self.conn, src, REPO)
        # The store's listing-coverage stamp after that sync (read back,
        # not assumed): the premise every check below states.
        stamp_row = self.conn.execute(
            "SELECT last_incr_sync_at, last_full_sync_at FROM sync_state"
            " WHERE repo = ?",
            (REPO,),
        ).fetchone()
        self.listing_stamp = stamp_row["last_incr_sync_at"] or stamp_row["last_full_sync_at"]
        self.assertIsNotNone(
            self.listing_stamp,
            "AC-PRE: the drained listing sync must leave a coverage stamp",
        )

        rec = comment(9001, CLAIM_BODY, author="alice", created_at=claim_at)
        rec["author_association"] = "MEMBER"
        rec["html_url"] = (
            f"https://github.com/{REPO}/issues/{CLAIMED}#issuecomment-9001"
        )
        src.issue_comments.setdefault(CLAIMED, []).append(rec)
        src.issues[CLAIMED]["comments"] = 1
        # GitHub bumps an issue's updated_at when a comment lands on it.
        src.issues[CLAIMED]["updated_at"] = issue_updated_at

        with patch("zaxbygraph.store.utcnow", return_value=refresh_finish):
            result = sync_repo(self.conn, src, REPO, numbers=[CLAIMED])
        self.assertIsNone(
            result.get("last_error"),
            "AC-PRE: the targeted refresh must finish cleanly",
        )
        self.conn.commit()

    # -- observation helpers -------------------------------------------------

    def _stored_claim_rows(self) -> list:
        """The stored comment rows for the claimed issue."""
        return list(
            self.conn.execute(
                "SELECT number, kind, author, author_association, created_at"
                " FROM comments WHERE repo = ? AND number = ?"
                " ORDER BY created_at, github_id",
                (REPO, CLAIMED),
            )
        )

    def _assert_claim_stored_and_trusted(self) -> None:
        """The ingestion guard: the claim row must really be in the store,
        and really carry a trusted association."""
        rows = self._stored_claim_rows()
        self.assertEqual(
            len(rows),
            1,
            "AC-PRE: exactly one claim comment must be stored for the "
            f"claimed issue; rows={rows}",
        )
        self.assertEqual(
            (rows[0]["author_association"], rows[0]["kind"]),
            ("MEMBER", "issue_comment"),
            "AC-PRE: the stored claim must be a trusted issue comment; "
            f"row={dict(rows[0])}",
        )
        # The owning issue's stored updated_at, read back from the store.
        self.issue_updated_at = self.conn.execute(
            "SELECT updated_at FROM items WHERE repo = ? AND number = ?",
            (REPO, CLAIMED),
        ).fetchone()["updated_at"]

    def _run_cli(self, argv: list[str]) -> dict:
        """The real CLI in-process; returns the envelope."""
        # The repo's own read-path contract (tests/test_read_truth.py): the
        # stale-data refresh goes through the module-level
        # ``zaxbygraph.cli._run_refresh_sync``, stubbed out here so a read
        # can never spawn a background sync -- a failure then surfaces as
        # an assertion, not as a temp-directory cleanup race.
        self.assertIsNotNone(
            getattr(cli_mod, "_run_refresh_sync", None),
            "AC-PRE: cli must expose the module-level _run_refresh_sync seam "
            "the read path drives",
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with patch.object(cli_mod, "_run_refresh_sync", return_value=0) as refresh:
                code = main(argv)
                self.assertFalse(
                    refresh.called,
                    "AC-PRE: a read must not trigger a background sync",
                )
        self.assertEqual(
            code, 0, f"AC-PRE: the command must succeed; stderr={err.getvalue()[:400]}"
        )
        envelope = json.loads(out.getvalue())
        self.assertTrue(
            envelope.get("ok"), f"AC-PRE: the envelope must be ok; {envelope}"
        )
        return envelope

    def _claims_listing(self) -> dict:
        """The derived ``claims`` listing payload (the CLI's surface)."""
        envelope = self._run_cli(
            ["claims", "--repo", REPO, "--db", str(self.db_path), "--format", "json"]
        )
        data = envelope.get("data") or {}
        self.assertIsInstance(
            data.get("claims"),
            list,
            f"AC-PRE: the claims listing must carry a claims list; {sorted(data)}",
        )
        return data

    def _claim_entry(self, data: dict, tag: str = "AC-PRE") -> dict:
        entries = [c for c in data["claims"] if c.get("number") == CLAIMED]
        self.assertEqual(
            len(entries),
            1,
            f"{tag}: the derived listing must carry exactly one entry for "
            f"the claimed issue; entries={entries}",
        )
        return entries[0]

    def _triage_payload(self) -> dict:
        """The real ``triage`` payload (rows + the gated block)."""
        envelope = self._run_cli(
            [
                "triage",
                "--include-gated",
                "--repo",
                REPO,
                "--db",
                str(self.db_path),
                "--format",
                "json",
            ]
        )
        data = envelope.get("data") or {}
        self.assertIsInstance(
            data.get("rows"), list, f"AC-PRE: triage must carry rows; {sorted(data)}"
        )
        self.assertIsInstance(
            data.get("gated"), dict, f"AC-PRE: triage must carry gated; {sorted(data)}"
        )
        return data


class TargetedRefreshActiveClaimTests(_TargetedRefreshBase):
    """AC1: the refreshed claim reads ACTIVE with a non-negative age."""

    def test_targeted_refresh_makes_fresh_claim_active(self) -> None:
        """AC1: a trusted unreleased claim ingested by ``sync --numbers``
        after the last drained listing is reported active, with a
        non-negative age_hours, by the derived claims listing.

        RED at base (the listing's anchor is the pre-refresh listing stamp,
        so the age is negative and ``active`` is false); GREEN after the fix.
        """
        self._build_store(
            claim_at=T1,
            issue_updated_at=ISSUE_UPDATED_AT,
            refresh_finish=REFRESH_FINISH,
        )
        self._assert_claim_stored_and_trusted()
        self.assertGreaterEqual(
            self.issue_updated_at,
            self._stored_claim_rows()[0]["created_at"],
            "AC-PRE: the owning issue's stored updated_at must be at or after "
            "the claim's creation (GitHub bumps it when a comment lands); "
            f"issue={self.issue_updated_at}",
        )

        data = self._claims_listing()
        entry = self._claim_entry(data)

        self.assertIs(
            entry.get("trusted"),
            True,
            "AC-PRE: the listed claim must be trusted; entry=" f"{entry}",
        )
        self.assertIs(
            entry.get("ended_by_release"),
            False,
            "AC-PRE: the listed claim must not be released; entry=" f"{entry}",
        )
        self.assertIs(
            entry.get("active"),
            True,
            "AC1-ANCHOR: the targeted-refreshed trusted claim must be "
            "reported active by the derived claims listing; entry=" f"{entry}",
        )
        age = entry.get("age_hours")
        self.assertIsInstance(
            age,
            (int, float),
            "AC1-ANCHOR: the active claim must report an age_hours "
            f"number; entry={entry}",
        )
        self.assertGreaterEqual(
            age,
            0.0,
            "AC1-ANCHOR: the active claim's age_hours must be non-negative; "
            f"entry={entry}",
        )


class TargetedRefreshClaimGateTests(_TargetedRefreshBase):
    """AC2: triage withholds the refreshed issue through the claimed gate."""

    def test_targeted_refresh_gates_its_issue(self) -> None:
        """AC2: after the targeted refresh, ``triage`` withholds the
        refreshed issue from the ranked rows and names it under the
        ``claimed`` gate; an issue with no claim stays ranked.

        RED at base (the claim reads inactive, so the ``claimed`` gate does
        not fire and the issue is offered as unclaimed work); GREEN after
        the fix.
        """
        self._build_store(
            claim_at=T1,
            issue_updated_at=ISSUE_UPDATED_AT,
            refresh_finish=REFRESH_FINISH,
        )
        self._assert_claim_stored_and_trusted()

        data = self._triage_payload()
        gated = data.get("gated") or {}
        by_gate = gated.get("by_gate") or {}

        self.assertIn(
            CLAIMED,
            by_gate.get("claimed") or [],
            "AC2-GATE: the refreshed issue must be withheld by the claimed "
            f"gate; by_gate={by_gate}",
        )

        ranked = [r.get("number") for r in data["rows"] if r.get("ranked")]
        self.assertNotIn(
            CLAIMED,
            ranked,
            "AC2-GATE: the refreshed issue must be withheld from the "
            f"ranked rows; ranked={ranked}",
        )
        self.assertIn(
            CONTROL,
            ranked,
            "AC2-GATE: an issue without a claim must stay ranked (the gate "
            f"is selective); ranked={ranked}",
        )

        row = next((r for r in data["rows"] if r.get("number") == CLAIMED), None)
        self.assertIsNotNone(
            row,
            "AC2-GATE: the gated issue must still be reported (gated rows "
            f"are not ranked away); rows={data['rows']}",
        )
        self.assertIn(
            "claimed",
            row.get("gates") or [],
            "AC2-GATE: the withheld issue must name the claimed gate; "
            f"row={row}",
        )
        self.assertIs(
            row.get("ranked"),
            False,
            "AC2-GATE: the withheld issue must not be ranked; " f"row={row}",
        )


class UnprovenClaimStaysInactiveTests(_TargetedRefreshBase):
    """AC5: the regression floor -- a claim dated after every proof the store
    holds stays inactive and gates nothing."""

    def test_claim_after_every_proof_stays_inactive(self) -> None:
        """AC5: a stored claim dated after the listing stamp, after the
        newest eligible ``fetch_log`` row of its issue in ingestion order
        AND after that issue's stored ``updated_at`` stays INACTIVE in the
        derived listing, with the stamp-based age, and gates nothing.

        GREEN at base and GREEN after any correct fix: the floor that
        rejects the pseudo-fixes clamping a negative age to 0.0 or dropping
        the ``0.0 <=`` lower bound, and a repair that counts ``updated_at``
        before the claim or reaches for the wall clock.
        """
        self._build_store(
            claim_at=AC5_CLAIM_AT,
            issue_updated_at=AC5_ISSUE_UPDATED_AT,
            refresh_finish=AC5_REFRESH_FINISH,
        )
        self._assert_claim_stored_and_trusted()

        # The three premises, read back from the store.
        newest_fetch = self.conn.execute(OBSERVATION_SQL, (REPO, str(CLAIMED))).fetchone()[0]
        claim_created = self._stored_claim_rows()[0]["created_at"]
        self.assertEqual(self.listing_stamp, T0, "AC-PRE: the listing stamp")
        self.assertEqual(newest_fetch, AC5_REFRESH_FINISH, "AC-PRE: the logged fetch time")
        self.assertEqual(
            self.issue_updated_at, AC5_ISSUE_UPDATED_AT, "AC-PRE: the stored updated_at"
        )
        self.assertGreater(
            claim_created,
            self.listing_stamp,
            "AC-PRE: the stored claim must postdate the listing stamp; "
            f"claim={claim_created} stamp={self.listing_stamp}",
        )
        self.assertGreater(
            claim_created,
            newest_fetch,
            "AC-PRE: the stored claim must postdate the newest successful "
            f"fetch of its issue; claim={claim_created} fetch={newest_fetch}",
        )
        self.assertGreater(
            claim_created,
            self.issue_updated_at,
            "AC-PRE: the stored claim must postdate its issue's stored "
            f"updated_at; claim={claim_created} issue={self.issue_updated_at}",
        )

        entry = self._claim_entry(self._claims_listing(), tag="AC5-FLOOR")
        self.assertIs(
            entry.get("ended_by_release"),
            False,
            "AC-PRE: the claim must not be released; " f"entry={entry}",
        )
        self.assertIs(
            entry.get("active"),
            False,
            "AC5-FLOOR: a claim dated after the listing stamp, after the newest "
            "successful fetch of its issue and after that issue's stored "
            f"updated_at must stay inactive; entry={entry}",
        )
        self.assertAlmostEqual(
            entry.get("age_hours"),
            AC5_STAMP_AGE,
            places=4,
            msg="AC5-FLOOR: with no proof of observation the stamp governs and "
            f"the age is the stamp-based one; entry={entry}",
        )
        gate = self._triage_payload().get("gated") or {}
        self.assertNotIn(
            CLAIMED,
            (gate.get("by_gate") or {}).get("claimed") or [],
            "AC5-FLOOR: an inactive unproven claim must gate nothing",
        )


class NoLoggedFetchStaysInactiveTests(_TargetedRefreshBase):
    """AC8: a claim whose issue has no logged fetch with a readable time has
    no proof of observation, whatever the issue's stored ``updated_at`` says."""

    def test_claim_without_a_logged_fetch_stays_inactive(self) -> None:
        """AC8: an expired claim dated after the listing stamp, whose issue
        payload carries an ``updated_at`` at or after it, stays INACTIVE with
        the stamp-based age once the issue has no eligible ``fetch_log`` row,
        and gates nothing.

        GREEN at base (today's code is inactive here as well) and GREEN after
        any correct fix: the floor that forbids counting a last-modification
        time alone as the moment of observation, which would read an expired
        claim active after the fetch history is lost.
        """
        self._build_store(
            claim_at=AC8_CLAIM_AT,
            issue_updated_at=AC8_ISSUE_UPDATED_AT,
            refresh_finish=AC8_REFRESH_FINISH,
        )
        self._assert_claim_stored_and_trusted()
        self.conn.execute("DELETE FROM fetch_log " + ELIGIBLE_FETCH_ROWS, (REPO, str(CLAIMED)))
        self.conn.commit()

        # The premises, read back from the store.
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log " + ELIGIBLE_FETCH_ROWS, (REPO, str(CLAIMED))
        ).fetchone()[0]
        claim_created = self._stored_claim_rows()[0]["created_at"]
        self.assertEqual(rows, 0, "AC-PRE: no eligible fetch row for the issue")
        self.assertGreater(
            claim_created,
            self.listing_stamp,
            "AC-PRE: the stored claim must postdate the listing stamp; "
            f"claim={claim_created} stamp={self.listing_stamp}",
        )
        self.assertLessEqual(
            claim_created,
            self.issue_updated_at,
            "AC-PRE: the issue's stored updated_at must be at or after the "
            f"claim; claim={claim_created} issue={self.issue_updated_at}",
        )

        entry = self._claim_entry(self._claims_listing(), tag="AC8-FLOOR")
        self.assertIs(
            entry.get("ended_by_release"),
            False,
            "AC-PRE: the claim must not be released; " f"entry={entry}",
        )
        self.assertIs(
            entry.get("active"),
            False,
            "AC8-FLOOR: a claim whose issue has no logged fetch with a readable "
            f"time must stay inactive, whatever updated_at says; entry={entry}",
        )
        self.assertAlmostEqual(
            entry.get("age_hours"),
            AC8_STAMP_AGE,
            places=4,
            msg="AC8-FLOOR: with no logged fetch the stamp governs and the age "
            f"is the stamp-based one; entry={entry}",
        )
        gate = self._triage_payload().get("gated") or {}
        self.assertNotIn(
            CLAIMED,
            (gate.get("by_gate") or {}).get("claimed") or [],
            "AC8-FLOOR: an inactive claim gates nothing",
        )

if __name__ == "__main__":
    unittest.main()
