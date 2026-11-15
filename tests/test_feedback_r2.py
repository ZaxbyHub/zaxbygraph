"""Swarm PR-feedback round 2 regression tests (PR #79, branch
fix/68-69-eval-selection-quality).

Pins the round-2 code fixes that have a behavior to observe:

- CS-1: prepare_snapshot's three ``_parse_instant`` comparisons are
  None-guarded — a malformed created_at/updated_at discloses
  ``unparseable_timestamps`` and the row is handled fail-closed, never a
  ``TypeError``.
- CUB-9686: ``_eval_bundle``/``_eval_triage`` SKIP fix-PR cases whose
  created_at is missing or unparseable (the ``_eval_similar``
  missing_created_at/created_at_unparseable discipline) instead of
  aborting the whole eval through prepare_snapshot's bad_request.
- IA-6 parity: both backtests emit the same per-case disclosure
  vocabulary — triage-mode entries now carry ``text_as_of_unknown`` when
  a truth endpoint's as-of text cannot be certified, and bundle-mode
  entries carry ``eval_triage_pool_cap`` when the bundle pool was cut at
  ``[triage] top_k``.
- CS-3/ST-5 (the #67 invariant): review-origin keyword evidence never
  gates ``linked_pr`` — only PR-description/commit keyword evidence (and
  the comment origin, already excluded) is excluded from the gate query.
- IA-2/CUB-9702: participants excludes the issue AUTHOR when the actor
  is flagged ``is_bot`` (the same bot exclusion the comment/review
  authors already had).
- GP-2/TI-2: the recorded backtest fixture is byte-addressed — the
  committed events.jsonl sha256 equals recording_manifest.json's pin.

Hermetic: FakeGitHubSource through the real sync path; no network, no
credentials; unittest (also collectable by pytest). Every class runs in
an empty temp cwd (TI-1/CUB-9739/CUB-9647) so the [triage]/[derive]
config chain never sees an ambient ``.zaxbygraph.toml`` — except the
top_k leg, which deliberately plants one in ITS OWN hermetic cwd.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, issue, pull

from zaxbygraph import derive, evaluate
from zaxbygraph.db import connect, init_schema, open_existing
from zaxbygraph.sync import sync_repo

_FIXTURE_DIR = Path(__file__).resolve().parent / "eval_fixture_backtest"


def _pin_synced_at(
    conn, repo: str = REPO, stamp: str = "2027-07-01T00:00:00Z"
) -> None:
    """Freeze the store's freshness stamps so stage-2 arithmetic is
    deterministic (the tests/test_eval_backtest.py pattern)."""
    conn.execute(
        "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ?"
        " WHERE repo = ?",
        (stamp, stamp, repo),
    )
    conn.commit()


class _HermeticTestCase(unittest.TestCase):
    """Empty-temp-cwd base (TI-1): in-process triage/bundle/eval calls
    resolve ``.zaxbygraph.toml`` from the cwd, so the checks run inside a
    directory that cannot carry one."""

    def setUp(self) -> None:
        self._prior_cwd = os.getcwd()
        self._hermetic_cwd = tempfile.TemporaryDirectory(
            prefix="zaxbygraph-r2-"
        )
        os.chdir(self._hermetic_cwd.name)

    def tearDown(self) -> None:
        os.chdir(self._prior_cwd)
        self._hermetic_cwd.cleanup()


class _StoreTestCase(_HermeticTestCase):
    """One temp store per test class (subclasses override ``_build``);
    one read-only connection per test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._td.name) / "store.db"
        conn = connect(cls.db_path)
        try:
            init_schema(conn)
            cls._build(conn)
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    @classmethod
    def _build(cls, conn) -> None:  # pragma: no cover - overridden
        raise AssertionError("_build must be overridden")

    def setUp(self) -> None:
        super().setUp()
        self.conn = open_existing(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        super().tearDown()


# ==== CS-1: unparseable stamps disclose, never TypeError ======================


class UnparseableStampTests(_StoreTestCase):
    """A store carrying malformed stamps: prepare_snapshot (via
    ``triage --as-of``) must answer, discard/handle the rows fail-closed,
    and disclose them under ``unparseable_timestamps`` — never raise
    ``TypeError`` from a None comparison."""

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_issue(issue(
            200, title="malformed created_at", body="plain",
            created_at="2027-06-01T00:00:00Z",
        ))
        src.add_issue(issue(
            201, title="malformed updated_at", body="plain",
            created_at="2027-06-01T00:00:00Z",
            updated_at="2027-06-02T00:00:00Z",
        ))
        src.add_issue(issue(
            202, title="clean row", body="plain",
            created_at="2027-06-01T00:00:00Z",
        ))
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)
        # Corrupt the stamps AFTER ingest: 200's created_at unparseable,
        # 201's updated_at unparseable, 202 untouched.
        conn.execute(
            "UPDATE items SET created_at = 'not-a-date' WHERE number = 200"
        )
        conn.execute(
            "UPDATE items SET updated_at = 'garbage' WHERE number = 201"
        )
        conn.commit()

    def test_as_of_answers_and_discloses(self) -> None:
        out = derive.triage(
            self.conn, REPO, include_gated=True, as_of="2027-06-05T00:00:00Z",
        )
        disclosures = out.get("asof_disclosures", {}).get("disclosures", {})
        unparseable = disclosures.get("unparseable_timestamps", [])
        self.assertIn(
            200, unparseable,
            f"a present-but-unparseable created_at must be disclosed under"
            f" unparseable_timestamps (the row is discarded from the"
            f" snapshot); got {unparseable!r}",
        )
        self.assertIn(
            201, unparseable,
            f"an unparseable updated_at must be disclosed under"
            f" unparseable_timestamps (payload fields stripped"
            f" fail-closed); got {unparseable!r}",
        )
        # The clean row still ranks; the malformed-created_at row is
        # invisible at T (discarded like a post-T row).
        numbers = [r["number"] for r in out["rows"]]
        self.assertIn(201, numbers)
        self.assertIn(202, numbers)
        self.assertNotIn(200, numbers)


# ==== CUB-9686 + IA-6 parity: the backtests skip bad stamps ===================


def _add_fix_pr(src, number: int, closed: list[int], *, merged_at: str,
                created_at: str) -> None:
    """One merged PR whose body closes ``closed`` (the body@ keyword
    stream) plus the closed issues themselves, all created before the PR
    and closed AT the merge (so an as-of replay at the PR's creation
    reopens them)."""
    body = " ".join(f"closes #{n}" for n in closed)
    pr = pull(number, merged=True)
    pr["merged_at"] = merged_at
    src.add_pr(
        issue(number, title=f"fix batch {number}", body=body, kind="pr",
              created_at=created_at, updated_at=created_at),
        pr,
    )
    for n in closed:
        src.add_issue(issue(
            n, title=f"closed work {n}", body="plain", state="closed",
            created_at="2027-06-01T00:00:00Z",
            updated_at="2027-06-01T00:00:00Z",
        ))
        # issue() derives closed_at from its own clock (2026-01-01 base);
        # pin it to the merge so the replay at the PR's creation reopens.
        src.issues[n]["closed_at"] = merged_at


class EvalBadStampSkipsTests(_StoreTestCase):
    """One good fix PR (300) plus a missing-created_at fix PR (310) and an
    unparseable-created_at fix PR (320): every eval backtest SKIPS the bad
    cases with the _eval_similar disclosure discipline and still measures
    the good one — instead of dying in prepare_snapshot's bad_request.
    The good case's endpoint 302 carries an updated_at AFTER the PR's
    creation (no revisions), so its as-of text is uncertifiable: BOTH
    modes must disclose text_as_of_unknown for that case (IA-6 parity)."""

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        # Good case: PR 300 closes 301+302 (threshold 2 for bundle).
        _add_fix_pr(
            src, 300, [301, 302],
            merged_at="2027-06-03T00:00:00Z", created_at="2027-06-02T00:00:00Z",
        )
        # 302 was edited after the fix PR's creation: its as-of text at
        # 2027-06-02 cannot be certified from the store (no revisions).
        src.issues[302]["updated_at"] = "2027-06-05T00:00:00Z"
        # Bad cases: PRs 310 (created_at -> NULL) and 320 (unparseable).
        _add_fix_pr(
            src, 310, [311, 312],
            merged_at="2027-06-03T00:00:00Z", created_at="2027-06-02T00:00:00Z",
        )
        _add_fix_pr(
            src, 320, [321, 322],
            merged_at="2027-06-03T00:00:00Z", created_at="2027-06-02T00:00:00Z",
        )
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)
        conn.execute("UPDATE items SET created_at = NULL WHERE number = 310")
        conn.execute(
            "UPDATE items SET created_at = 'not-a-date' WHERE number = 320"
        )
        conn.commit()

    def test_triage_and_bundle_skip_bad_stamp_cases(self) -> None:
        for mode in ("triage", "bundle"):
            with self.subTest(mode=mode):
                result = evaluate.run_eval(self.conn, REPO, mode)
                leakage = result.get("with_leakage", {})
                self.assertIn(
                    "310", leakage,
                    f"{mode}: the missing-created_at fix PR must be"
                    f" disclosed under with_leakage (missing_created_at);"
                    f" got {leakage!r}",
                )
                self.assertIn("missing_created_at", leakage["310"])
                self.assertIn(
                    "320", leakage,
                    f"{mode}: the unparseable-created_at fix PR must be"
                    f" disclosed under with_leakage"
                    f" (created_at_unparseable); got {leakage!r}",
                )
                self.assertIn("created_at_unparseable", leakage["320"])
                # The good case was still measured and reported.
                per_fix = {int(e["pr"]): e for e in result["per_fix_pr"]}
                self.assertIn(300, per_fix)
                self.assertNotIn(310, per_fix)
                self.assertNotIn(320, per_fix)

    def test_triage_disclosures_match_bundle_vocabulary(self) -> None:
        """IA-6 parity: the good case (endpoint 302's text uncertifiable at
        the stamp) discloses text_as_of_unknown in BOTH modes."""
        for mode in ("triage", "bundle"):
            with self.subTest(mode=mode):
                result = evaluate.run_eval(self.conn, REPO, mode)
                entry = next(
                    e for e in result["per_fix_pr"] if int(e["pr"]) == 300
                )
                self.assertIn(
                    "text_as_of_unknown", entry["disclosures"],
                    f"{mode}: a truth endpoint whose as-of text cannot be"
                    f" certified must disclose text_as_of_unknown (the"
                    f" bundle-mode rule, now shared); got"
                    f" {entry['disclosures']!r}",
                )


class BundlePoolCapDisclosureTests(_StoreTestCase):
    """IA-6 parity, cap leg: when the bundle pool is cut at ``[triage]
    top_k`` the per-case disclosures carry ``eval_triage_pool_cap`` — the
    same bounded-pool vocabulary the triage mode reports. The top_k cut
    is driven by a ``.zaxbygraph.toml`` planted in THIS test's own
    hermetic cwd (the config chain the eval replay really reads)."""

    TOP_K = 1

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        _add_fix_pr(
            src, 300, [301, 302],
            merged_at="2027-06-03T00:00:00Z", created_at="2027-06-02T00:00:00Z",
        )
        # A third open issue at T so the ungated pool (3) exceeds top_k=1.
        src.add_issue(issue(
            303, title="bystander", body="plain",
            created_at="2027-06-01T00:00:00Z",
        ))
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_bundle_pool_cut_disclosed(self) -> None:
        # setUp already chdir'd into the hermetic cwd; plant the config
        # THERE (never the operator's filesystem).
        Path(".zaxbygraph.toml").write_text(
            f"[triage]\ntop_k = {self.TOP_K}\n", encoding="utf-8"
        )
        result = evaluate.run_eval(self.conn, REPO, "bundle")
        entry = next(e for e in result["per_fix_pr"])
        cap_notes = [
            d for d in entry["disclosures"] if d.startswith(
                "eval_triage_pool_cap"
            )
        ]
        self.assertTrue(
            cap_notes,
            f"a bundle pool cut at [triage] top_k must disclose"
            f" eval_triage_pool_cap (the shared vocabulary); got"
            f" {entry['disclosures']!r}",
        )
        # Round-3 wording pin: the cap names the INITIAL top-k ungated
        # ranking cut and the blocker-relaxation admission beyond it — it
        # must never claim rows past the cut "can never enter a bundle"
        # (the bundler's in-pool blocker relaxation admits blocked-only
        # rows past the cut, derive.py's fixpoint).
        note = cap_notes[0]
        self.assertIn(
            "initial pool was the top-k ungated ranking cut", note,
            f"the disclosure must describe the INITIAL top-k cut"
            f" accurately; got {note!r}",
        )
        self.assertIn("blocker-relaxation", note)
        self.assertIn("may still admit blocked-only rows beyond that cut", note)
        self.assertNotIn(
            "can never enter a bundle", note,
            f"rows past the cut CAN enter a bundle via blocker relaxation;"
            f" the old absolute wording was false; got {note!r}",
        )


# ==== CS-3/ST-5: review-origin keyword evidence never gates linked_pr ========


def _build_gate_target(conn) -> None:
    """Open issue 400 + open unmerged non-draft PR 401 (no edges between
    them yet — the leg classes insert the keyword edge under test)."""
    src = FakeGitHubSource()
    src.add_issue(issue(
        400, title="gate target", body="plain",
        created_at="2027-06-01T00:00:00Z",
    ))
    open_pr = pull(401, merged=False)
    src.add_pr(
        issue(401, title="claims the fix", body="", kind="pr",
              created_at="2027-06-01T00:00:00Z"),
        open_pr,
    )
    sync_repo(conn, src, REPO)
    _pin_synced_at(conn)


def _insert_keyword_edge(db_path: Path, evidence: str) -> None:
    # The per-test connections are read-only; the edge insertion rides its
    # own short-lived read-write handle.
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO edges (repo, src_type, src_id, rel,"
            " dst_type, dst_id, source, evidence, confidence)"
            " VALUES (?, 'item', '401', 'closes_keyword', 'item', '400',"
            " 'keyword', ?, 'EXTRACTED')",
            (REPO, evidence),
        )
        conn.commit()
    finally:
        conn.close()


class ReviewEvidenceGateTests(_StoreTestCase):
    """The #67 invariant at the gate query: a ``closes_keyword`` edge whose
    evidence names a REVIEW origin must not gate the issue out of triage —
    only PR-description (body@) and commit keyword evidence gates, with
    the comment origin already excluded."""

    @classmethod
    def _build(cls, conn) -> None:
        _build_gate_target(conn)
        _insert_keyword_edge(cls.db_path, "review:999@0 closing keyword")

    def test_review_origin_does_not_gate(self) -> None:
        out = derive.triage(self.conn, REPO, include_gated=True)
        rows = {r["number"]: r for r in out["rows"]}
        self.assertEqual(
            rows[400]["gates"], [],
            f"review-origin keyword evidence must never gate linked_pr"
            f" (#67: only PR-description/commit keyword evidence gates);"
            f" got {rows[400]['gates']!r}",
        )
        self.assertIs(rows[400]["ranked"], True)


class BodyEvidenceGateControlTests(_StoreTestCase):
    """Control leg: the SAME edge with PR-body evidence (body@) still
    gates — the fix excludes only the review origin, not the keyword gate
    itself."""

    @classmethod
    def _build(cls, conn) -> None:
        _build_gate_target(conn)
        _insert_keyword_edge(cls.db_path, "body@0 closing keyword")

    def test_body_origin_still_gates_control(self) -> None:
        out = derive.triage(self.conn, REPO, include_gated=True)
        rows = {r["number"]: r for r in out["rows"]}
        self.assertIn(
            "linked_pr (append-only evidence; may be stale)",
            rows[400]["gates"],
            f"control leg: PR-body keyword evidence still gates (the"
            f" append-only variant); got {rows[400]['gates']!r}",
        )
        self.assertIs(rows[400]["ranked"], False)


# ==== IA-2/CUB-9702: a bot issue author is not a participant ==================


class BotAuthorParticipantsTests(_StoreTestCase):
    """The item's own author joins participants under the same bot
    exclusion the comment/review authors already had: a bot-filed issue
    with one human comment counts ONE participant (the human)."""

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_issue(issue(
            500, title="bot-filed report", body="plain",
            author="mergebot[bot]", created_at="2027-06-01T00:00:00Z",
        ))
        src.comment_on(500, "triaging this", author="alice")
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_bot_author_not_counted(self) -> None:
        out = derive.triage(self.conn, REPO, include_gated=True)
        rows = {r["number"]: r for r in out["rows"]}
        self.assertIn(500, rows)
        participants = rows[500]["scores"]["features_raw"]["participants"]
        self.assertEqual(
            participants, 1,
            f"a bot-flagged issue author must not count as a participant"
            f" (alice only); got {participants!r}",
        )


# ==== GP-2/TI-2: the recorded fixture is byte-addressed =======================


class FixtureShaPinTests(_HermeticTestCase):
    """sha256(committed events.jsonl bytes) == recording_manifest.json's
    events_sha256 — the pin that makes the fixture trustworthy as
    evidence, protected against line-ending translation by the
    directory's ``.gitattributes`` (``events.jsonl -text``)."""

    def test_events_sha_matches_manifest(self) -> None:
        digest = hashlib.sha256(
            (_FIXTURE_DIR / "events.jsonl").read_bytes()
        ).hexdigest()
        manifest = json.loads(
            (_FIXTURE_DIR / "recording_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            digest, manifest["events_sha256"],
            f"the committed events.jsonl bytes must hash to the manifest"
            f" pin (LF line endings preserved by .gitattributes);"
            f" got {digest!r} vs {manifest['events_sha256']!r}",
        )
        # The protecting attribute exists and marks the file -text.
        attrs = (_FIXTURE_DIR / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn(
            "events.jsonl -text", attrs,
            f"the .gitattributes must pin events.jsonl as -text (no eol"
            f" translation anywhere); got {attrs!r}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
