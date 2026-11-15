"""Implementation extension pins for the claim lifecycle (issues #77/#74,
trace 77-74-claims-gate). NOT a frozen check file: these are the
post-critic extension legs the approved plan inventories --

- collaborator trust legs (a FakeGh subclass routing the
  repos/O/R/collaborators/<login> probe): a read-only outside collaborator
  is trusted (204 -> claimed, one post); a 404 -> not_applicable, no posts.
- selective org/collaborator transport-failure legs: a probe FAILURE (any
  rc=1 that is not HTTP-404-shaped) answers github_unreachable, posts
  nothing, and a corroborating viewerPermission never masks it.
- the PR-501 fail-closed leg: a safety read that hits its page cap can
  never fabricate a clean claim or a zero-collision pr-guard.
- no `since=` anywhere in a live argv; every `gh api` call carries an
  explicit repos/O/R route and no -R (gh api rejects it); -R rides the
  `gh pr list` call alone.
- outcome-envelope legs: exactly one outcome per failure, error.code =
  data.outcome, winner/rollback evidence preserved, pr-guard collisions =
  taken, MCP isError:true with the complete envelope.
- second-wave rollback, seeded-RNG retry delay, parser bounds (cross-repo
  refs, #5abc, oversized digit runs, a 65-char token), token-reuse
  ordering (a release BEFORE its claim ends nothing), future timestamps,
  equal-timestamp github_id ordering, multiple claims per issue, the
  as-of claim window (anchored at the SNAPSHOT's sync stamp), PR-comment
  filtering in the live feed, the brief suppression pin, store
  byte-identity around every claims command, and bundle exclusion.

Hermetic: FakeGh (imported from the frozen live-check file) and
FakeGitHubSource only; no network, no gh subprocess.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import re
import tempfile
import unittest
import contextlib
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, comment, issue, ts
import test_claim_cmd as _tcc
from test_claim_cmd import (
    FakeGh,
    _LiveCmdTest,
    _patched_gh,
    _run_cli,
    claim_body,
    release_body,
)
from test_mcp import McpSession, pin_env_for, tool_envelope
from zaxbygraph import claims as claims_mod
from zaxbygraph.cli import main
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

ASOF = ts(0)


def _h(hours: float) -> str:
    return ts(-int(hours * 3600))


@contextlib.contextmanager
def _frozen_claims_clock(instant: str):
    """Freeze the LIVE classification clock (``claims._now``) alongside
    fixed comment stamps: the live claim commands classify window fitness
    against ``_now()``, so a live-path pin with absolute timestamps MUST
    pin that clock too or it becomes a wall-clock time bomb (F9 -- before
    the stamps' instant the claims are future-dated and inactive; past the
    72h window the earliest competitor expires). Derived-tier pins are
    safe by construction: their asof comes from the store's pinned sync
    stamp."""
    saved = claims_mod._now
    claims_mod._now = lambda: claims_mod._parse_instant(instant)
    try:
        yield
    finally:
        claims_mod._now = saved


def _claim_body(token: str | None = None, named: str = "") -> str:
    if token is None:
        return "Working this issue now (collated PR in progress)."
    return f"**Agent claim:** session={token}{named} Working this issue now."


def _env_of(out: str) -> dict:
    return json.loads(out)


# ---------------------------------------------------------------------------
# FakeGh subclasses (probe legs)


# ---------------------------------------------------------------------------
class _CollabGh(FakeGh):
    """Routes the repository-collaborator probe the base fake cannot."""

    def __init__(self, *, collab_status: int = 204, **kw) -> None:
        super().__init__(**kw)
        self.collab_status = collab_status

    def __call__(self, cmd):
        joined = " ".join(str(a) for a in cmd)
        if re.search(r"repos/[\w.-]+/[\w.-]+/collaborators/", joined):
            if self.collab_status in (0, 204):
                return (204, "", "")
            return (404, "", "Not Found")
        return super().__call__(cmd)


class _OrgFailGh(FakeGh):
    """The org-membership probe fails with a NON-404 transport error."""

    def __call__(self, cmd):
        joined = " ".join(str(a) for a in cmd)
        if re.search(r"orgs/[\w.-]+/members/", joined):
            return (1, "", "gh: connection reset by peer")
        return super().__call__(cmd)


class _CollabFailGh(FakeGh):
    """Org membership misses (404); the collaborator probe FAILS."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.org_member_status = 404

    def __call__(self, cmd):
        joined = " ".join(str(a) for a in cmd)
        if re.search(r"repos/[\w.-]+/[\w.-]+/collaborators/", joined):
            return (1, "", "gh: dial tcp: lookup api.github.com: no such host")
        return super().__call__(cmd)


class _FloodedCommentsGh(FakeGh):
    """Every per-issue comments page answers a FULL 100-entry page with
    FRESH ids (the page-cap shape): the safety read can never complete."""

    def __call__(self, cmd):
        joined = " ".join(str(a) for a in cmd)
        if "comments?page=" in joined:
            page_match = re.search(r"page=(\d+)", joined)
            page = int(page_match.group(1)) if page_match else 1
            base = 950_000 + page * 1000
            filler = [
                {
                    "id": base + i,
                    "body": "plain chatter",
                    "user": {"login": "alice"},
                    "author_association": "MEMBER",
                    "created_at": _h(1.0),
                    "html_url": (
                        f"https://github.com/{REPO}/issues/5#issuecomment-{base + i}"
                    ),
                }
                for i in range(100)
            ]
            return (0, json.dumps(filler), "")
        return super().__call__(cmd)


class _FloodedPrListGh(FakeGh):
    """The pr list answers exactly the --limit cap: the closing-ref safety
    read may be partial and can never certify a clean guard."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)

    def __call__(self, cmd):
        joined = " ".join(str(a) for a in cmd)
        if self._is_pr_read(joined):
            flood = [
                {
                    "number": 900 + i,
                    "state": "open",
                    "title": f"pr {i}",
                    "url": f"https://github.com/{REPO}/pull/{900 + i}",
                    "closingIssuesReferences": {"nodes": []},
                }
                for i in range(500)
            ]
            return (0, json.dumps(flood), "")
        return super().__call__(cmd)


# ---------------------------------------------------------------------------
# Trust probes (collaborator legs + failure legs)


class CollaboratorTrustTests(_LiveCmdTest):
    def test_collaborator_trust_legs(self) -> None:
        # HIT: an outside read-only collaborator (org 404, viewer READ,
        # collaborator 204) is trusted -> claimed with ONE post.
        fake = _CollabGh(collab_status=204)
        fake.viewer_login = "outsider"
        fake.owner_login = "acme"
        fake.org_member_status = 404
        fake.viewer_permission = "READ"
        with _patched_gh(self, "EXT-COLLAB-HIT", fake):
            code, out, err = _run_cli(
                ["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        env = _env_of(out)
        self.assertEqual(code, 0, f"collaborator-204 must claim; stderr {err.strip()[:160]!r}")
        self.assertEqual((env.get("data") or {}).get("outcome"), "claimed")
        self.assertEqual([n for n, _b in fake.posts], [5], "exactly one post")

        # MISS: collaborator 404 (org 404, viewer READ) -> not_applicable,
        # nothing posted.
        fake2 = _CollabGh(collab_status=404)
        fake2.viewer_login = "outsider"
        fake2.org_member_status = 404
        fake2.viewer_permission = "READ"
        with _patched_gh(self, "EXT-COLLAB-MISS", fake2):
            code2, out2, _err2 = _run_cli(
                ["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        env2 = _env_of(out2)
        self.assertEqual(code2, 0)
        self.assertEqual((env2.get("data") or {}).get("outcome"), "not_applicable")
        self.assertEqual(fake2.posts, [], "an untrusted login posts nothing")


class ProbeFailureTests(_LiveCmdTest):
    def test_org_probe_failure_is_unreachable_and_never_masked(self) -> None:
        fake = _OrgFailGh()
        # viewerPermission stays ADMIN on this fake: the corroborating
        # payload must NEVER mask the failed membership probe.
        with _patched_gh(self, "EXT-ORGFAIL", fake):
            code, out, _err = _run_cli(
                ["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        env = _env_of(out)
        self.assertEqual(code, 6, "a probe failure exits the unreachable code")
        data = env.get("data") or {}
        self.assertEqual(data.get("outcome"), "github_unreachable")
        self.assertEqual((env.get("error") or {}).get("code"), "github_unreachable")
        self.assertEqual(fake.posts, [], "a failed probe posts nothing")

    def test_collaborator_probe_failure_is_unreachable(self) -> None:
        fake = _CollabFailGh()
        with _patched_gh(self, "EXT-COLLABFAIL", fake):
            code, out, _err = _run_cli(
                ["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        env = _env_of(out)
        self.assertEqual(code, 6)
        self.assertEqual((env.get("data") or {}).get("outcome"), "github_unreachable")
        self.assertEqual(fake.posts, [])


# ---------------------------------------------------------------------------
# Fail-closed safety reads (the PR-501 class)


class FailClosedSafetyReadTests(_LiveCmdTest):
    def test_page_capped_comment_read_never_claims(self) -> None:
        fake = _FloodedCommentsGh()
        with _patched_gh(self, "EXT-FLOOD-COMMENTS", fake):
            code, out, _err = _run_cli(
                ["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        env = _env_of(out)
        self.assertNotEqual(code, 0, "a capped safety read must not claim")
        self.assertEqual(
            (env.get("data") or {}).get("outcome"),
            "github_unreachable",
            "the fail-closed outcome is nonzero, never a fabricated clean claim",
        )
        self.assertEqual(fake.posts, [])

    def test_capped_pr_list_never_certifies_clean_guard(self) -> None:
        fake = _FloodedPrListGh()
        with _patched_gh(self, "EXT-FLOOD-PRS", fake):
            code, out, _err = _run_cli(
                ["pr-guard", "5", "--repo", REPO, "--format", "json"]
            )
        env = _env_of(out)
        self.assertNotEqual(code, 0, "a capped PR list must not certify clean")
        self.assertEqual(
            (env.get("data") or {}).get("outcome"),
            "github_unreachable",
            "a partial closing-ref list is fail-closed, never zero-collisions",
        )
        self.assertEqual(fake.posts, [])


# ---------------------------------------------------------------------------
# Live argv hygiene


class LiveArgvHygieneTests(_LiveCmdTest):
    def test_no_since_and_no_dash_r_on_gh_api(self) -> None:
        fake = FakeGh()
        fake.add_claim(6, "HYGIENE-TOKEN-123456789")
        with _patched_gh(self, "EXT-ARGV", fake):
            _run_cli(["claim", "5", "6", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"])
            _run_cli(["pr-guard", "5", "--repo", REPO, "--format", "json"])
            _run_cli(["claims", "--live", "--repo", REPO, "--format", "json"])
        pr_list_calls = 0
        for cmd in fake.calls:
            joined = " ".join(str(a) for a in cmd)
            self.assertNotIn(
                "since=",
                joined,
                f"no live read may be window-bound by since= (expired claims"
                f" must be listed; safety reads must be complete): {joined[:120]!r}",
            )
            if cmd[:2] == ["gh", "pr"]:
                pr_list_calls += 1
                self.assertIn("-R", cmd, "the pr list call alone carries -R O/R")
            elif cmd[:2] == ["gh", "api"]:
                self.assertNotIn(
                    "-R",
                    cmd,
                    f"gh api rejects -R; api calls carry explicit repos/O/R"
                    f" routes: {joined[:120]!r}",
                )
                self.assertRegex(
                    joined,
                    r"(repos/|orgs/|graphql| user$| user )",
                    f"every gh api call carries an explicit route: {joined[:120]!r}",
                )
        self.assertGreaterEqual(pr_list_calls, 3, "each command read the pr list once")


# ---------------------------------------------------------------------------
# Outcome envelopes + MCP parity


class OutcomeEnvelopeTests(_LiveCmdTest):
    def test_taken_envelope_carries_exactly_one_outcome(self) -> None:
        fake = FakeGh()
        fake.add_claim(5, "ENVELOPE-TAKEN-TOKEN-1")
        with _patched_gh(self, "EXT-ENV", fake):
            code, out, _err = _run_cli(
                ["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        env = _env_of(out)
        self.assertEqual(code, 4)
        self.assertIs(env.get("ok"), False)
        error = env.get("error") or {}
        data = env.get("data") or {}
        self.assertEqual(error.get("code"), "taken")
        self.assertEqual(data.get("outcome"), "taken")
        self.assertEqual(error.get("code"), data.get("outcome"), "exactly ONE outcome value")
        held = (data.get("details") or {}).get("held_by_active_claim") or {}
        holder_list = held.get("5") if isinstance(held, dict) and "5" in held else (
            held.get(5) if isinstance(held, dict) else None
        )
        holder = holder_list[0] if isinstance(holder_list, list) and holder_list else None
        self.assertIsNotNone(holder, f"the holder evidence is preserved: {json.dumps(data)[:300]}")
        self.assertEqual(holder.get("token"), "ENVELOPE-TAKEN-TOKEN-1")
        self.assertIn("sync --numbers", json.dumps(data), "the remedy names sync --numbers")
        self.assertIn("claims_config", data, "the failure keeps the claims_config echo")

    def test_mcp_claim_failure_is_error_with_full_envelope(self) -> None:
        session = McpSession(
            self, cwd=str(self.root), repo=REPO, db=str(self.db_path), stale_after_s=10**9
        )
        self.addCleanup(session.stop)
        session.initialize()
        fake = FakeGh()
        fake.add_claim(5, "MCP-TAKEN-TOKEN-00001")
        with _patched_gh(self, "EXT-MCP-ENV", fake):
            response = session.call_tool("claim", {"repo": REPO, "numbers": [5]})
        result, env = tool_envelope(self, response, "EXT-MCP-ENV")
        self.assertIs(result.get("isError"), True, "a taken outcome is isError:true")
        self.assertIs(env.get("ok"), False)
        self.assertEqual((env.get("error") or {}).get("code"), "taken")
        data = env.get("data") or {}
        self.assertEqual(data.get("outcome"), "taken", "the outcome data survives the failure")
        self.assertIn("claims_config", data)
        session.stop()


# ---------------------------------------------------------------------------
# Wave rollback + seeded RNG


class RetryWaveTests(_LiveCmdTest):
    def test_second_wave_rollback_and_seeded_rng_delay(self) -> None:
        import time as _time

        from test_claim_cmd import _foreign_claim

        fake = FakeGh()
        fake.reveal_timed = {
            20: [
                (1, 1.0, [_foreign_claim(879_101, "RNG-A-TOKEN-0000001")]),
                (2, 30.0, [_foreign_claim(879_102, "RNG-B-TOKEN-0000002")]),
            ]
        }
        delays: list[float] = []

        def _record_and_sleep(seconds: float) -> None:
            delays.append(float(seconds))
            if float(seconds) >= 0.5:
                # Real wait: the timed competitor must actually vanish
                # before the retry's fresh read.
                _time.sleep(float(seconds))

        saved_sleep = claims_mod.sleep
        saved_rng = claims_mod._RNG
        claims_mod.sleep = _record_and_sleep
        claims_mod._RNG = random.Random(1234)
        try:
            with _patched_gh(self, "EXT-RNG", fake):
                code, out, _err = _run_cli(
                    ["claim", "10", "20", "30", "--settle-seconds", "0.05",
                     "--repo", REPO, "--format", "json"]
                )
        finally:
            claims_mod.sleep = saved_sleep
            claims_mod._RNG = saved_rng
        env = _env_of(out)
        self.assertEqual((env.get("data") or {}).get("outcome"), "lost_race")
        self.assertEqual(code, 5)
        # exactly ONE retry wait in the documented 1-5s band, equal to the
        # seeded RNG draw; every other recorded wait is the 0.05s settle.
        retry_waits = [d for d in delays if d >= 0.5]
        settle_waits = [d for d in delays if d < 0.5]
        self.assertEqual(len(retry_waits), 1, f"one pre-retry wait; saw {delays}")
        self.assertTrue(1.0 <= retry_waits[0] <= 5.0, f"wait in [1,5]: {retry_waits[0]}")
        self.assertEqual(
            retry_waits[0],
            random.Random(1234).uniform(1.0, 5.0),
            "the wait is the seeded RNG draw",
        )
        self.assertTrue(
            settle_waits and all(abs(d - 0.05) < 1e-9 for d in settle_waits),
            f"the settle waits ride the settle seam: {settle_waits}",
        )
        claim_posts = [(n, b) for n, b in fake.posts if "**Agent claim" in b]
        release_posts = [(n, b) for n, b in fake.posts if "**Claim released" in b]
        self.assertEqual([n for n, _ in claim_posts], [10, 20, 30, 10, 20, 30])
        # BOTH waves were released (wave 1 on the first loss, wave 2 on the
        # second): six release posts.
        self.assertEqual([n for n, _ in release_posts], [10, 20, 30, 10, 20, 30])
        details = (env.get("data") or {}).get("details") or {}
        self.assertIn("RNG-B-TOKEN-0000002", json.dumps(details), "the second winner is named")


# ---------------------------------------------------------------------------
# Parser bounds + classification ordering (direct module legs)


class ParserBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = claims_mod.load_claims_config()

    def test_cross_repo_and_boundary_refs(self) -> None:
        body = "Agent claim: see other/repo#5 and #5abc and #" + "1" * 12 + " but #7 is real"
        parsed = claims_mod.parse_claim(body, self.cfg)
        self.assertEqual(parsed["named_issues"], [7], "only the bounded local ref counts")

    def test_sixty_five_char_token_does_not_match(self) -> None:
        long_token = "A" * 65
        parsed = claims_mod.parse_claim(
            f"**Agent claim:** session={long_token} working", self.cfg
        )
        self.assertIsNone(parsed.get("token"), "a 65-char token run matches nothing")
        ok_token = "A" * 64
        parsed_ok = claims_mod.parse_claim(
            f"**Agent claim:** session={ok_token} working", self.cfg
        )
        self.assertEqual(parsed_ok.get("token"), ok_token, "a 64-char token parses")

    def test_quoted_and_fenced_lines_never_match(self) -> None:
        self.assertIsNone(
            claims_mod.parse_claim("> Agent claim: quoted", self.cfg)
        )
        self.assertIsNone(
            claims_mod.parse_claim("```\nAgent claim: fenced\n```", self.cfg)
        )


class CollectOrderingTests(unittest.TestCase):
    """collect_claims over hand-built rows: token-reuse ordering, future
    timestamps, equal-timestamp github_id ties, multiple claims."""

    def _rows(self, *specs):
        return [
            (number, author, assoc, created, body, f"https://github.com/x/issues/{number}#issuecomment-{cid}", cid)
            for number, author, assoc, created, body, cid in specs
        ]

    def test_release_before_claim_ends_nothing(self) -> None:
        cfg = claims_mod.load_claims_config()
        rows = self._rows(
            # the release (3h ago) is OLDER than the claim (2h ago): only
            # LATER releases end claims.
            (5, "alice", "MEMBER", _h(3), release_body("REUSE-TOKEN-1234567890AB"), 200),
            (5, "alice", "MEMBER", _h(2), _claim_body("REUSE-TOKEN-1234567890AB"), 201),
        )
        out = claims_mod.collect_claims(None, "x/y", rows, cfg, ASOF)
        self.assertEqual(len(out), 1)
        self.assertIs(out[0]["active"], True, "a release BEFORE the claim ends nothing")

    def test_future_timestamp_is_never_active(self) -> None:
        cfg = claims_mod.load_claims_config()
        rows = self._rows(
            (5, "alice", "MEMBER", ts(3600), _claim_body("FUTURE-TOKEN-123456789"), 300),
        )
        out = claims_mod.collect_claims(None, "x/y", rows, cfg, ASOF)
        self.assertIs(out[0]["active"], False, "created_at > asof is never active")
        self.assertIsNotNone(out[0]["age_hours"])

    def test_equal_timestamps_order_by_github_id(self) -> None:
        cfg = claims_mod.load_claims_config()
        same = _h(2)
        rows = self._rows(
            (5, "alice", "MEMBER", same, _claim_body("LATER-ID-TOKEN-12345"), 902),
            (5, "bob", "MEMBER", same, _claim_body("EARLIER-ID-TOKEN-1234"), 901),
        )
        out = claims_mod.collect_claims(None, "x/y", rows, cfg, ASOF)
        self.assertEqual(
            [c["token"] for c in out],
            ["EARLIER-ID-TOKEN-1234", "LATER-ID-TOKEN-12345"],
            "ascending (created_at, github_id); the listing is its reverse",
        )
        # both claims on one issue are LISTED
        self.assertEqual(len(out), 2)
        self.assertTrue(all(c["active"] for c in out))


# ---------------------------------------------------------------------------
# Derived-tier legs over real stores


class _StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = self.root / "history.db"
        import os

        saved = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(lambda: os.chdir(saved))
        pin_env_for(self, self.root, db=self.db_path)

    def seed(self, seed_fn) -> None:
        conn = connect(self.db_path)
        init_schema(conn)
        src = FakeGitHubSource()
        seed_fn(src)
        sync_repo(conn, src, REPO)
        conn.execute(
            "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ? WHERE repo = ?",
            (ASOF, ASOF, REPO),
        )
        conn.commit()
        conn.close()

    def _sha(self) -> str:
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    @staticmethod
    def _comment_on(src, number, cid, body, *, author="alice", assoc="MEMBER", created=None):
        rec = comment(cid, body, author=author, created_at=created)
        rec["author_association"] = assoc
        rec["html_url"] = f"https://github.com/{REPO}/issues/{number}#issuecomment-{cid}"
        src.issue_comments.setdefault(number, []).append(rec)
        if number in src.issues:
            src.issues[number]["comments"] = len(src.issue_comments[number])


class AsOfWindowTests(_StoreTest):
    def test_as_of_view_gates_with_snapshot_sync_anchor(self) -> None:
        token = "ASOF-TOKEN-1234567890AB"

        def seed(src):
            src.add_issue(issue(42, title="as-of target", state="open"))
            self._comment_on(src, 42, 4201, _claim_body(token), created=_h(10))

        self.seed(seed)
        # The claim is 10h before the PINNED SYNC STAMP but months before
        # the wall clock; the as-of snapshot anchors the claim window at
        # its rewritten sync stamp, so the gate fires in the as-of view
        # too (the claim predates T and the window is measured from the
        # snapshot's own clock).
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(
                ["triage", "--as-of", ts(60), "--include-gated", "--limit", "50",
                 "--repo", REPO, "--format", "json"]
            )
        self.assertEqual(code, 0)
        data = (_env_of(out.getvalue()).get("data") or {})
        row = next((r for r in data.get("rows") or [] if r.get("number") == 42), None)
        self.assertIsNotNone(row)
        self.assertIn("claimed", row.get("gates") or [])


class BriefSuppressionTests(_StoreTest):
    def test_brief_keeps_gate_strips_claim_fields(self) -> None:
        token = "BRIEF-TOKEN-1234567890"

        def seed(src):
            src.add_issue(issue(52, title="brief target", state="open"))
            self._comment_on(src, 52, 5201, _claim_body(token), created=_h(1))

        self.seed(seed)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["brief", "52", "--repo", REPO, "--format", "json"])
        self.assertEqual(code, 0)
        packet = (_env_of(out.getvalue()).get("data") or {})
        features = packet.get("features") or {}
        self.assertIn("claimed", features.get("gates") or [], "gates keeps claimed")
        self.assertNotIn("claim_evidence", features, "brief strips claim_evidence")
        dump = json.dumps(packet)
        self.assertNotIn("claims_config", dump, "brief strips the claims config echo")
        self.assertNotIn("claims_config", json.dumps(features), "no claims echo anywhere")

        # triage/bundle KEEP the additive fields the brief strips.
        out_t = io.StringIO()
        with redirect_stdout(out_t):
            main(["triage", "--include-gated", "--limit", "50", "--repo", REPO, "--format", "json"])
        data_t = (_env_of(out_t.getvalue()).get("data") or {})
        row_t = next(r for r in data_t["rows"] if r.get("number") == 52)
        self.assertIn("claim_evidence", row_t, "the triage row carries claim evidence")
        self.assertIn("claims_config", data_t, "triage echoes claims_config")
        self.assertIn(
            "claims", (data_t.get("gated") or {}), "the gated block carries claims evidence"
        )


class BundleExclusionTests(_StoreTest):
    def test_claimed_issue_is_not_bundleable(self) -> None:
        token = "BUNDLE-TOKEN-1234567890"

        def seed(src):
            for n in (61, 62):
                src.add_issue(issue(n, title=f"bundle {n}", state="open"))
            self._comment_on(src, 61, 6101, _claim_body(token), created=_h(1))

        self.seed(seed)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["bundle", "--repo", REPO, "--format", "json"])
        self.assertEqual(code, 0)
        data = _env_of(out.getvalue()).get("data") or {}
        pool = data.get("pool") or []
        self.assertNotIn(61, pool, "a claimed issue is gated out of every bundle pool")
        gated = data.get("gated") or {}
        claims_evidence = gated.get("claims") or []
        self.assertTrue(
            any(e.get("token") == token for e in claims_evidence),
            f"the bundle gated block names the claim evidence: {claims_evidence}",
        )
        self.assertIn("claims_config", data)


class StoreByteIdentityTests(_StoreTest):
    def test_every_claims_command_leaves_the_store_byte_identical(self) -> None:
        token = "IDENT-TOKEN-1234567890"

        def seed(src):
            src.add_issue(issue(70, title="identity target", state="open"))
            self._comment_on(src, 70, 7001, _claim_body(token), created=_h(3))

        self.seed(seed)
        before = self._sha()
        outs: list[io.StringIO] = []

        def _run(argv):
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                main(argv)
            outs.append(buf)

        _run(["claims", "--repo", REPO, "--format", "json"])
        fake = FakeGh()
        with _patched_gh(self, "EXT-IDENT", fake):
            _run(["claims", "--live", "--repo", REPO, "--format", "json"])
            _run(["claim", "70", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"])
            _run(["release", "70", "--token", "NO-SUCH-TOKEN-1", "--repo", REPO, "--format", "json"])
            _run(["pr-guard", "70", "--repo", REPO, "--format", "json"])
        self.assertEqual(self._sha(), before, "no claims command writes the store")
        self.assertTrue(outs[1].getvalue().strip(), "the live listing answered")


class LivePrCommentFilterTests(_LiveCmdTest):
    def test_pr_conversation_comments_are_not_issue_claims(self) -> None:
        token = "PRFEED-TOKEN-123456789"

        class _PrThreadGh(FakeGh):
            # issue 5 is a PR thread: its issue record carries pull_request.
            def __call__(self, cmd):
                joined = " ".join(str(a) for a in cmd)
                if re.search(r"issues/5$", joined):
                    rec = {
                        "number": 5,
                        "state": "open",
                        "title": "a pull request thread",
                        "html_url": f"https://github.com/{REPO}/pull/5",
                        "pull_request": {"url": "x"},
                    }
                    return (0, json.dumps(rec), "")
                return super().__call__(cmd)

        fake = _PrThreadGh()
        fake.add_repo_comment(5, claim_body(token))
        with _patched_gh(self, "EXT-PRFEED", fake):
            _run_cli(["claims", "--live", "--repo", REPO, "--format", "json"])
        # (the listing above must not crash; assert through a fresh call)
        fake2 = _PrThreadGh()
        fake2.add_repo_comment(5, claim_body(token))
        fake2.add_repo_comment(6, claim_body("ISSUEFEED-TOKEN-12345"))
        with _patched_gh(self, "EXT-PRFEED2", fake2):
            code, out, _err = _run_cli(["claims", "--live", "--repo", REPO, "--format", "json"])
        self.assertEqual(code, 0)
        claims = (_env_of(out).get("data") or {}).get("claims") or []
        tokens = {c.get("token") for c in claims}
        self.assertNotIn(token, tokens, "a PR conversation comment is not an issue claim")
        self.assertIn("ISSUEFEED-TOKEN-12345", tokens, "the issue claim is listed")


# Round-1 review pins (F1-F4): token arbitration, explicit config,
# subprocess faults, live candidate-read failure


class _FixedPostStampGh(FakeGh):
    """Posts get FIXED created_at stamps so race arbitration is
    deterministic: the first claim wave posts at ``post_stamp`` and the
    retry wave at ``retry_stamp``, so a competitor's (created_at,
    comment_id) ordering against our own posts is fully controlled by the
    test."""

    post_stamp = "2026-11-15T12:00:00Z"
    retry_stamp = "2026-11-15T12:05:00Z"

    def __call__(self, cmd):
        argv = [str(a) for a in cmd]
        body = self._post_body(argv)
        if body is not None:
            if "**Agent claim" in body:
                wave = sum(1 for _n, b in self.posts if "**Agent claim" in b)
                stamp = self.post_stamp if wave == 0 else self.retry_stamp
            else:
                stamp = self.post_stamp
            saved = _tcc._iso_hours_ago
            _tcc._iso_hours_ago = lambda h: stamp if h == 0.0 else saved(h)
            try:
                return super().__call__(argv)
            finally:
                _tcc._iso_hours_ago = saved
        return super().__call__(argv)


class TokenArbitrationTests(_LiveCmdTest):
    """F1: race arbitration is BY SESSION TOKEN, never by login -- every
    session posts as one login, so a same-login different-token claim must
    be evaluated. The earliest active claim per (created_at, comment_id)
    wins; if it is not our own post we lose the issue."""

    def _same_login_competitor(self, cid: int, token: str, created: str) -> dict:
        return {
            "id": cid,
            "body": f"**Agent claim:** session={token} working this issue now.",
            "user": {"login": "me"},  # the SESSION LOGIN, different token
            "author_association": "MEMBER",
            "created_at": created,
            "updated_at": created,
            "html_url": f"https://github.com/{REPO}/issues/5#issuecomment-{cid}",
        }

    def test_same_login_earlier_token_beats_us(self) -> None:
        fake = _FixedPostStampGh()
        fake.reveal_after_post = {
            5: [
                self._same_login_competitor(
                    800_001, "SAMELOGIN-EARLIER-TOKEN", "2026-11-15T11:00:00Z"
                )
            ]
        }
        with _frozen_claims_clock("2026-11-15T12:06:00Z"), \
                _patched_gh(self, "EXT-F1-EARLIER", fake):
            code, out, _err = _run_cli(
                ["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        env = _env_of(out)
        self.assertEqual(code, 5, "the earlier same-login claim wins the race")
        data = env.get("data") or {}
        self.assertEqual(data.get("outcome"), "lost_race")
        details = data.get("details") or {}
        self.assertEqual(
            (details.get("winner") or {}).get("token"),
            "SAMELOGIN-EARLIER-TOKEN",
            "the same-login competitor is named the winner (no login skip)",
        )
        releases = [(n, b) for n, b in fake.posts if "**Claim released" in b]
        self.assertEqual([n for n, _ in releases], [5], "our losing wave is released")

    def test_same_login_later_token_loses_to_us(self) -> None:
        fake = _FixedPostStampGh()
        # Same stamp as our own post (the subclass pins it) but a HIGHER
        # comment id: our own post is the earliest active claim, so the
        # session KEEPS the issue -- no rollback, outcome claimed.
        fake.reveal_after_post = {
            5: [
                self._same_login_competitor(
                    999_999, "SAMELOGIN-LATER-TOKEN", _FixedPostStampGh.post_stamp
                )
            ]
        }
        with _frozen_claims_clock("2026-11-15T12:06:00Z"), \
                _patched_gh(self, "EXT-F1-LATER", fake):
            code, out, _err = _run_cli(
                ["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        env = _env_of(out)
        self.assertEqual(code, 0, f"the later same-login claim loses; env {json.dumps(env)[:300]}")
        data = env.get("data") or {}
        self.assertEqual(data.get("outcome"), "claimed")
        fake_posts = [(n, b) for n, b in fake.posts if "**Claim released" in b]
        self.assertEqual(fake_posts, [], "no rollback when our own post is earliest")


class RetryArbitrationTests(_LiveCmdTest):
    """F7 (round 2): the retry's arbitration must compare competitors
    against the RETRY wave's post, never the RELEASED first wave's post.
    A competitor that appears only after the first-wave release but before
    the retry post has a key between the two waves: it is EARLIER than the
    retry's post (lost_race), not later than the released first wave's
    (which the stale-key bug wrongly answered `claimed`)."""

    def test_competitor_between_release_and_retry_wins_the_retry(self) -> None:
        fake = _FixedPostStampGh()
        # Wave-1 posts at 12:00:00 (post_stamp), the retry at 12:05:00
        # (retry_stamp). The between-waves competitor Y is created at
        # 12:02:00: LATER than the released wave-1 post, EARLIER than the
        # retry post -- arbitration must use the RETRY's key.
        def _competitor(cid: int, token: str, created: str) -> dict:
            return {
                "id": cid,
                "body": f"**Agent claim:** session={token} working this issue now.",
                "user": {"login": "me"},
                "author_association": "MEMBER",
                "created_at": created,
                "updated_at": created,
                "html_url": f"https://github.com/{REPO}/issues/5#issuecomment-{cid}",
            }

        fake.reveal_timed = {
            5: [
                # X: visible at the first settle read (within 1.0s of the
                # FIRST claim post), gone before the retry's fresh read.
                (1, 1.0, [_competitor(9_100_001, "WAVE1-LOSER-TOKEN-0001", "2026-11-15T11:00:00Z")]),
                # Y: appears only once the RETRY post exists (2nd claim
                # post), created BETWEEN the two waves.
                (2, 30.0, [_competitor(9_000_015, "BETWEEN-WAVES-TOKEN-1", "2026-11-15T12:02:00Z")]),
            ]
        }
        with _frozen_claims_clock("2026-11-15T12:06:00Z"), \
                _patched_gh(self, "EXT-F7", fake):
            code, out, _err = _run_cli(
                ["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        env = _env_of(out)
        data = env.get("data") or {}
        self.assertEqual(
            data.get("outcome"),
            "lost_race",
            f"the between-waves competitor is EARLIER than the retry post"
            f" (the stale first-wave key would wrongly answer claimed):"
            f" {json.dumps(env)[:400]}",
        )
        self.assertEqual(code, 5)
        winner = ((data.get("details") or {}).get("winner") or {}).get("token")
        self.assertEqual(winner, "BETWEEN-WAVES-TOKEN-1")
        claim_posts = [(n, b) for n, b in fake.posts if "**Agent claim" in b]
        release_posts = [(n, b) for n, b in fake.posts if "**Claim released" in b]
        self.assertEqual(
            [n for n, _ in claim_posts], [5, 5], "exactly one retry wave"
        )
        self.assertEqual(
            [n for n, _ in release_posts], [5, 5], "both waves were released"
        )


class ClosingRefShapesTests(_LiveCmdTest):
    """F8 (round 2): no closing-reference shape loses a reference -- bare
    INTEGER elements are accepted inside every refs container (flat array
    and nodes wrapper alike) as same-repo issue numbers, beside the dict
    elements (real flat-array shape, url/repository-filtered)."""

    def test_every_shape_yields_its_references(self) -> None:
        fake = FakeGh()
        fake.open_prs = [
            {  # flat INT array under closingIssuesReferences
                "number": 30, "state": "open", "title": "int flat",
                "url": f"https://github.com/{REPO}/pull/30",
                "closingIssuesReferences": [5],
            },
            {  # INT inside a nodes wrapper under closingIssueReferences
                "number": 31, "state": "open", "title": "int nodes",
                "url": f"https://github.com/{REPO}/pull/31",
                "closingIssueReferences": {"nodes": [6]},
            },
            {  # real flat DICT array: one same-repo ref kept, one
               # cross-repo same-number ref rejected
                "number": 32, "state": "open", "title": "dict flat",
                "url": f"https://github.com/{REPO}/pull/32",
                "closingIssuesReferences": [
                    {
                        "number": 7,
                        "url": f"https://github.com/{REPO}/issues/7",
                        "repository": {"name": REPO.split("/")[1],
                                       "owner": {"login": REPO.split("/")[0]}},
                    },
                    {
                        "number": 8,
                        "url": "https://github.com/other/repo/issues/8",
                        "repository": {"name": "repo",
                                       "owner": {"login": "other"}},
                    },
                ],
            },
        ]
        with _patched_gh(self, "EXT-F8", fake):
            code, out, _err = _run_cli(
                ["pr-guard", "5", "6", "7", "--repo", REPO, "--format", "json"]
            )
        self.assertEqual(code, 4, "every shape's reference collides")
        env = _env_of(out)
        collisions = (env.get("data") or {}).get("collisions") or []
        self.assertEqual(
            sorted(pr.get("number") for pr in collisions),
            [30, 31, 32],
            f"no shape lost its reference: {json.dumps(collisions)[:300]}",
        )
        by_pr = {pr.get("number"): pr for pr in collisions}
        self.assertEqual(by_pr[30].get("closes"), [5], "flat int array")
        self.assertEqual(by_pr[31].get("closes"), [6], "int inside nodes")
        self.assertEqual(
            by_pr[32].get("closes"), [7], "same-repo dict kept, cross-repo rejected"
        )

    def test_cross_repo_int_never_flags_local_number(self) -> None:
        # The cross-repo DICT ref on issue 8 does not collide; 8 stays free.
        fake = FakeGh()
        fake.open_prs = [
            {
                "number": 32, "state": "open", "title": "dict flat",
                "url": f"https://github.com/{REPO}/pull/32",
                "closingIssuesReferences": [
                    {
                        "number": 8,
                        "url": "https://github.com/other/repo/issues/8",
                        "repository": {"name": "repo", "owner": {"login": "other"}},
                    }
                ],
            }
        ]
        with _patched_gh(self, "EXT-F8-CLEAN", fake):
            code, out, _err = _run_cli(
                ["pr-guard", "8", "--repo", REPO, "--format", "json"]
            )
        self.assertEqual(code, 0, "a cross-repo same-number ref never collides")
        data = _env_of(out).get("data") or {}
        self.assertEqual(data.get("collisions"), [])


class ExplicitConfigGateTests(_StoreTest):
    """F2: the explicit --config path threads into the [claims] loader --
    `triage --config f.toml` with `[claims] enabled = false` disables the
    claimed gate end-to-end."""

    def test_explicit_config_disables_claimed_gate(self) -> None:
        token = "EXPLICIT-CFG-TOKEN-1"

        def seed(src):
            src.add_issue(issue(80, title="explicit config target", state="open"))
            self._comment_on(src, 80, 8001, _claim_body(token), created=_h(1))

        self.seed(seed)
        cfgf = self.root / "no-claims.toml"
        cfgf.write_text("[claims]\nenabled = false\n", encoding="utf-8")

        def _gates(extra: list[str]) -> dict:
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                main(
                    ["triage", "--include-gated", "--limit", "50",
                     *extra, "--repo", REPO, "--format", "json"]
                )
            data = _env_of(out.getvalue()).get("data") or {}
            row = next(r for r in data["rows"] if r.get("number") == 80)
            return {"gates": row.get("gates") or [], "echo": data.get("claims_config")}

        with_flag = _gates(["--config", str(cfgf)])
        self.assertEqual(
            with_flag["gates"], [], "the explicit [claims] enabled=false disables the gate"
        )
        self.assertIs(with_flag["echo"].get("enabled"), False)
        self.assertEqual(with_flag["echo"].get("config_source"), "explicit")
        without_flag = _gates([])
        self.assertIn(
            "claimed", without_flag["gates"], "without the flag the gate fires"
        )
        self.assertIs(without_flag["echo"].get("enabled"), True)


class _Completed:
    def __init__(self, rc: int, out: str, err: str) -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class SubprocessFaultTests(_LiveCmdTest):
    """F3: subprocess faults (missing gh binary, timeout) translate to the
    github_unreachable path -- never a traceback -- and a timeout during
    the post-settle verification still rolls the posted wave back and
    reports it."""

    def test_missing_gh_binary_is_unreachable_not_traceback(self) -> None:
        import subprocess as _sp

        real_run = claims_mod.subprocess.run

        def _missing(*_a, **_k):
            raise FileNotFoundError("[WinError 2] gh: command not found")

        claims_mod.subprocess.run = _missing
        try:
            code, out, err = _run_cli(
                ["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        finally:
            claims_mod.subprocess.run = real_run
        self.assertEqual(code, 6, f"missing gh exits unreachable; stderr {err.strip()[:200]!r}")
        env = _env_of(out)
        self.assertEqual((env.get("data") or {}).get("outcome"), "github_unreachable")
        self.assertEqual((env.get("error") or {}).get("code"), "github_unreachable")
        self.assertNotIn("Traceback", err)

    def test_timeout_during_verification_rolls_back(self) -> None:
        import subprocess as _sp

        fake = FakeGh()
        real_run = claims_mod.subprocess.run
        real_run_gh = claims_mod.run_gh

        def _exec(argv, **_k):
            joined = " ".join(str(a) for a in argv)
            has_claim_post = any("**Agent claim" in b for _n, b in fake.posts)
            if "comments?page=" in joined and has_claim_post:
                # The post-settle verification read times out AFTER our
                # claim post landed.
                raise _sp.TimeoutExpired(cmd=joined, timeout=30)
            rc, out, err = fake(list(argv))
            return _Completed(rc, out, err)

        claims_mod.subprocess.run = _exec
        try:
            code, out, err = _run_cli(
                ["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        finally:
            claims_mod.subprocess.run = real_run
            claims_mod.run_gh = real_run_gh
        self.assertEqual(code, 6, f"timeout exits unreachable; stderr {err.strip()[:200]!r}")
        self.assertNotIn("Traceback", err)
        env = _env_of(out)
        data = env.get("data") or {}
        self.assertEqual(data.get("outcome"), "github_unreachable")
        details = data.get("details") or {}
        released = details.get("released") or []
        self.assertTrue(
            any("issues/5#issuecomment-" in url for url in released),
            f"the rollback release is reported: {json.dumps(details)[:300]}",
        )
        self.assertNotIn("unreleased_comments", details, "the release post itself succeeded")
        releases = [(n, b) for n, b in fake.posts if "**Claim released" in b]
        self.assertEqual([n for n, _ in releases], [5], "the posted wave was rolled back")


class PostedVocabularyTests(unittest.TestCase):
    """F5: the posted bodies are built from the CONFIGURED vocabulary and
    are recognized by the EFFECTIVE parser (the tool is self-consistent
    under overrides); the default table posts the documented canonical
    forms byte-for-byte."""

    def test_posted_bodies_parse_under_effective_config(self) -> None:
        over = claims_mod.load_claims_config(
            explicit_config={
                "patterns": ["Staking this"],
                "release_prefix": "**Abandoned",
                "token_prefix": "run=",
                "window_hours": 48,
                "settle_seconds": 1,
            }
        )
        token = "POSTED-TOKEN-1234567890"
        claim_body = claims_mod._claim_body(token, [9, 3], over)
        parsed = claims_mod.parse_claim(claim_body, over)
        self.assertIsNotNone(parsed, "the posted claim parses under the override")
        self.assertEqual(parsed["token"], token)
        self.assertEqual(parsed["named_issues"], [3, 9])
        self.assertIn("**Staking this:**", claim_body, "the configured lead is used")
        self.assertIn(f"run={token}", claim_body, "the configured token form is used")

        release_body = claims_mod._release_body(token, over)
        self.assertTrue(
            release_body.startswith("**Abandoned"),
            "the configured release prefix LEADS (anchored parse)",
        )
        prel = claims_mod.parse_release(release_body, over)
        self.assertEqual((prel or {}).get("token"), token)
        self.assertIn("**Claim released", release_body, "the canonical marker rides along")

        tokenless = claims_mod._release_body(None, over)
        self.assertNotIn("run=", tokenless, "the tokenless form carries no token segment")
        self.assertIsNone(
            (claims_mod.parse_release(tokenless, over) or {}).get("token")
        )

    def test_default_config_posts_canonical_forms(self) -> None:
        cfg = claims_mod.load_claims_config()
        token = "CANONICAL-TOKEN-1234567"
        self.assertTrue(
            claims_mod._claim_body(token, [5, 6], cfg).startswith(
                f"**Agent claim:** session={token} issues=#5,#6"
            ),
            "the default claim body is the Design-1 form",
        )
        self.assertTrue(
            claims_mod._release_body(token, cfg).startswith(
                f"**Claim released:** session={token}"
            ),
            "the default tokened release body is the Design-2 form",
        )
        self.assertTrue(
            claims_mod._release_body(None, cfg).startswith("**Claim released:**"),
            "the default tokenless release body is the Design-2 form",
        )


class _CandidateReadFailGh(FakeGh):
    """The issue-record read for issue 6 fails with a transport error."""

    def __call__(self, cmd):
        joined = " ".join(str(a) for a in cmd)
        if re.search(r"issues/6$", joined):
            return (1, "", "gh: HTTP 502 bad gateway")
        return super().__call__(cmd)


class LiveListingFailClosedTests(_LiveCmdTest):
    """F4: a candidate's issue-read failure fails the WHOLE live listing
    (github_unreachable) -- a silently omitted claim would make a partial
    list look complete."""

    def test_candidate_read_failure_fails_the_listing(self) -> None:
        fake = _CandidateReadFailGh()
        fake.add_repo_comment(5, claim_body("LIVE-OK-TOKEN-1234567890"))
        fake.add_repo_comment(6, claim_body("LIVE-BROKEN-TOKEN-12345"))
        with _patched_gh(self, "EXT-F4", fake):
            code, out, _err = _run_cli(
                ["claims", "--live", "--repo", REPO, "--format", "json"]
            )
        self.assertEqual(code, 6, "a candidate-read failure is unreachable, not partial success")
        env = _env_of(out)
        self.assertIs(env.get("ok"), False)
        self.assertEqual((env.get("data") or {}).get("outcome"), "github_unreachable")
        claims = (env.get("data") or {}).get("claims")
        self.assertNotIsInstance(
            claims, list, "no partial claims list is fabricated from a failed read"
        )




if __name__ == "__main__":
    unittest.main()
