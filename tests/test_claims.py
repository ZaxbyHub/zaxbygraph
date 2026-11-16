"""Acceptance checks for issue-tracer trace 77-74-claims-gate, file 2 of 2:
the derived claim tier of issue #74 (the `claimed` gate semantics, the
deterministic `claims` listing, the config defaults/overrides #77 defines,
and the bundled skill's claim documentation).

Authored at arm's length from the issue bodies ONLY (issue #74 Summary +
AC1-AC10, riding the parser/values issue #77 Design 8 fixes). No
implementation was read -- none exists at the base commit. Every test is
RED at base (the `claims` command, the `claimed` gate and the
`zaxbygraph.claims` module do not exist there), and each test's FIRST
failing assertion carries its C-tag ("C20:", "C21:", ...) for --expect
pinning.

AC -> -k method map (issue #74). The two DISCRIMINATING store-probe ACs
(AC1 test_claimed_issue_is_gated / AC2 test_claim_covers_named_issues) are
pinned by their own trace legs and are deliberately NOT duplicated here;
this file carries the remaining ACs plus two suite-coverage tests:

  AC3  C20 test_expired_and_released_claims
  AC4  C21 test_claim_config_defaults_and_overrides   (values owned by #77)
  AC5  C22 test_claims_command
  AC6  C23 test_claim_gate_is_deterministic
  AC8  C25 test_skill_names_claims
  AC10 C26 test_untrusted_author_does_not_gate
  AC7      PRESERVING -- not authored here: the existing
           tests/test_similar.py::test_derived_tier_never_writes stays
           GREEN at base and after (the derived claims tier writes nothing).
  suite    test_claimed_gate_evidence_fields  (gate evidence parsed fields)
  suite    test_claims_mcp_tool_lists_claims  (MCP claims tool envelope)

CONTRACT THESE TESTS PIN (issue-derived, or flagged [TRACE-DESIGN] where
the issue left the mechanical detail open):

1. `zaxbygraph.claims` exposes:
   - load_claims_config(store_config=None, cwd_config=None,
     explicit_config=None) -> dict with keys enabled(bool), patterns(
     list[str]), release_prefix(str), token_prefix(str), window_hours(int),
     settle_seconds(float). [TRACE-DESIGN ASSUMPTION: the three layer
     parameters accept config DICTS for the direct legs; the CLI legs only
     rely on the no-argument call applying built-ins when no config file
     exists.]
   - parse_claim(body, cfg) -> dict | None with token (str|None),
     named_issues (list[int], sorted), matched_text (str: the matched LINE)
     -- named issues come from the `issues=#a,#b` segment AND from #N
     references in the matched line (the recorded prose form, issue #74
     claim 2).
   - parse_release(body, cfg) -> dict | None with token (str|None); a
     release only matches a body that STARTS WITH cfg's release_prefix.
   - collect_claims(conn, repo, comments_rows, cfg, asof) -> list[dict];
     comments_rows are (number, author, author_association, created_at,
     body, html_url) tuples; the window is measured from `asof` (in the
     derived tier, the store's last sync time) and NEVER from the wall
     clock; a claim is ACTIVE when trusted association, within window, no
     release matching its token (tokenless release ends only tokenless
     claims; a token release never ends a tokenless claim), issue open
     (open-ness is the CALLER's row selection -- these tests pass only
     open issues' comments).
   Built-ins (no config): patterns "Agent claim" + "Working this issue"
   case-insensitive; release comment starts with "**Claim released";
   token form session=<token> with charset [A-Za-z0-9-]{16,64}; window
   72 hours; settle 2.0 s; enabled True.

2. CLI `claims` (derived, no --live) and the MCP `claims` tool answer the
   repo envelope {ok, db, repo, freshness, data, truncated} with data =
   {claims: [...], claims_config, config_source, derived: true,
   stale_claims: [...]}; per-claim fields number, title, author,
   author_association, trusted, token, created_at, age_hours,
   named_issues, url, text ({"untrusted_text": matched line}), active;
   newest first.

3. `triage --include-gated` gains gate `claimed` with evidence parsed
   fields (author, token, time, url) -- the vocabulary of the other gates
   is unchanged.

Hermetic: FakeGitHubSource stores under tempfile dirs, comment fixtures
gain author_association manually (fixtures.py untouched), no network, no
gh subprocess, Windows-safe.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, comment, issue, ts
from test_mcp import McpSession, pin_env_for, tool_envelope
from zaxbygraph.cli import main
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

_SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "zaxbygraph" / "SKILL.md"

#: The store's pinned last-sync time: every fixture claim age and every
#: window in this file is measured from ASOF, never from the wall clock.
ASOF = ts(0)


def _h(hours: float) -> str:
    """A created_at `hours` before ASOF."""
    return ts(-int(hours * 3600))


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """In-process cli.main under redirected stdio (the run_cli pattern)."""
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            if isinstance(exc.code, int):
                code = exc.code
            elif exc.code is None:
                code = 0
            else:
                code = 2
    return code, out.getvalue(), err.getvalue()


def _comment_on(
    src: FakeGitHubSource,
    number: int,
    cid: int,
    body: str,
    *,
    author: str = "alice",
    association: str = "MEMBER",
    created_at: str | None = None,
) -> dict:
    """A stored comment carrying author_association (fixtures.comment has
    none; the sync stores it verbatim)."""
    rec = comment(cid, body, author=author, created_at=created_at)
    rec["author_association"] = association
    rec["html_url"] = f"https://github.com/{REPO}/issues/{number}#issuecomment-{cid}"
    src.issue_comments.setdefault(number, []).append(rec)
    if number in src.issues:
        src.issues[number]["comments"] = len(src.issue_comments[number])
    return rec


def _claim_body(token: str | None = None, named: str = "") -> str:
    if token is None:
        return "Working this issue now (collated PR in progress)."
    return f"**Agent claim:** session={token}{named} Working this issue now."


def _release_body(token: str | None = None) -> str:
    if token is None:
        return "**Claim released:** scope split."
    return f"**Claim released:** session={token} done."


def _find_dict_containing(obj, pairs: dict):
    """Depth-first search for a dict holding every key/value in `pairs`."""
    if isinstance(obj, dict):
        if all(k in obj and obj[k] == v for k, v in pairs.items()):
            return obj
        for value in obj.values():
            found = _find_dict_containing(value, pairs)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_dict_containing(value, pairs)
            if found is not None:
                return found
    return None


class _ClaimsStoreTest(unittest.TestCase):
    """Scratch store + pinned env + scratch cwd (no config file nearby)."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = self.root / "history.db"
        self._saved_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(lambda: os.chdir(self._saved_cwd))
        pin_env_for(self, self.root, db=self.db_path)

    def seed(self, seed_fn) -> None:
        conn = connect(self.db_path)
        init_schema(conn)
        src = FakeGitHubSource()
        seed_fn(src)
        sync_repo(conn, src, REPO)
        # Pin the derived tier's asof: the store's last sync time.
        conn.execute(
            "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ? WHERE repo = ?",
            (ASOF, ASOF, REPO),
        )
        conn.commit()
        conn.close()

    def run_claims(self, tag: str) -> dict:
        code, out, err = _run_cli(["claims", "--repo", REPO, "--format", "json"])
        try:
            env = json.loads(out)
        except ValueError:
            self.fail(
                f"{tag}: the `claims` command must exist and print one JSON envelope "
                f"(issue #74 AC5); got exit {code}, stdout {out[:120]!r}, "
                f"stderr {err.strip()[:160]!r}"
            )
        self.assertIsInstance(env, dict, f"{tag}: the envelope must be a JSON object")
        self.assertEqual(code, 0, f"{tag}: the derived claims listing must exit 0; got {code} ({err.strip()[:160]!r})")
        return env

    def run_triage_rows(self, tag: str) -> dict:
        code, out, err = _run_cli(
            ["triage", "--include-gated", "--limit", "200", "--repo", REPO, "--format", "json"]
        )
        self.assertEqual(code, 0, f"{tag}: triage must succeed (it exists at base); exit {code}, stderr {err.strip()[:160]!r}")
        env = json.loads(out)
        return env.get("data") or {}

    @staticmethod
    def _row(data: dict, number: int) -> dict | None:
        return next((r for r in data.get("rows") or [] if r.get("number") == number), None)


class ExpiredReleasedClaimsTests(_ClaimsStoreTest):
    """C20 (issue #74 AC3): window expiry does not gate and lands under
    stale_claims with its age; a release carrying the claim's token ends
    it at once; a tokenless release ends only tokenless claims; a token
    release never ends a tokenless claim."""

    def test_expired_and_released_claims(self) -> None:
        stale_token = "STALE-TOKEN-1234567890AB"
        ends_token = "ENDS-AT-ONCE-TOKEN-12345"
        other_token = "OTHER-TOKEN-1234567890AB"
        held_token = "HELD-TOKEN-1234567890ABC"

        def seed(src: FakeGitHubSource) -> None:
            # 10: claim 100h old -> past the 72h window -> stale, not gating.
            src.add_issue(issue(10, title="stale claim", state="open"))
            _comment_on(src, 10, 1010, _claim_body(stale_token), created_at=_h(100))
            # 11: claim released at once by its own token.
            src.add_issue(issue(11, title="released by token", state="open"))
            _comment_on(src, 11, 1110, _claim_body(ends_token), created_at=_h(2))
            _comment_on(src, 11, 1111, _release_body(ends_token), created_at=_h(1))
            # 12: tokenless claim ended by a tokenless release.
            src.add_issue(issue(12, title="tokenless released", state="open"))
            _comment_on(src, 12, 1210, _claim_body(None), created_at=_h(3))
            _comment_on(src, 12, 1211, _release_body(None), created_at=_h(2))
            # 13: tokenless claim + TOKEN release -> still active.
            src.add_issue(issue(13, title="tokenless held", state="open"))
            _comment_on(src, 13, 1310, _claim_body(None), created_at=_h(3))
            _comment_on(src, 13, 1311, _release_body(other_token), created_at=_h(1))
            # 14: tokened claim + TOKENLESS release -> still active.
            src.add_issue(issue(14, title="token held", state="open"))
            _comment_on(src, 14, 1410, _claim_body(held_token), created_at=_h(3))
            _comment_on(src, 14, 1411, _release_body(None), created_at=_h(2))

        self.seed(seed)
        env = self.run_claims("C20 (issue #74 AC3)")
        data = env.get("data") or {}
        claims = data.get("claims")
        self.assertIsInstance(claims, list, f"C20 (issue #74 AC3): data.claims must be a list; got {type(claims).__name__}")
        active = {c.get("number"): c for c in claims if isinstance(c, dict) and c.get("active")}

        # a token release never ends a tokenless claim (13 stays active, token null)
        self.assertIn(13, active, f"C20 (issue #74 AC3): issue 13's TOKENLESS claim must survive a TOKEN release; active numbers {sorted(active)}")
        self.assertIsNone(active[13].get("token"), "C20 (issue #74 AC3): a tokenless claim lists token null")
        # a tokenless release never ends a tokened claim (14 stays active)
        self.assertIn(14, active, f"C20 (issue #74 AC3): issue 14's TOKENED claim must survive a TOKENLESS release; active numbers {sorted(active)}")
        self.assertEqual(active[14].get("token"), held_token, "C20 (issue #74 AC3): the surviving claim names its session token")
        # released-at-once and tokenless-released claims are not active
        for gone in (10, 11, 12):
            self.assertNotIn(
                gone,
                active,
                f"C20 (issue #74 AC3): issue {gone} must NOT carry an active claim "
                f"(10 expired, 11 released by its token, 12 tokenless-released); active {sorted(active)}",
            )
        # expiry lands under stale_claims with the claim's age
        stale = data.get("stale_claims")
        self.assertIsInstance(stale, list, f"C20 (issue #74 AC3): data.stale_claims must be a list; got {type(stale).__name__}")
        entry10 = next((s for s in stale if isinstance(s, dict) and s.get("number") == 10), None)
        self.assertIsNotNone(entry10, f"C20 (issue #74 AC3): the expired claim must be reported under stale_claims WITH ITS AGE; stale_claims {stale}")
        age = entry10.get("age_hours")
        self.assertIsInstance(age, (int, float), f"C20 (issue #74 AC3): a stale_claims entry carries age_hours; got {age!r}")
        self.assertTrue(72 < float(age) < 200, f"C20 (issue #74 AC3): the 100h-old claim reports ~100 age_hours; got {age!r}")

        # gate legs: the survivors gate, the ended/expired do not.
        data_t = self.run_triage_rows("C20")
        for gated in (13, 14):
            row = self._row(data_t, gated)
            self.assertIsNotNone(row, f"C20 (issue #74 AC3): triage --include-gated must show issue {gated}")
            self.assertIn("claimed", row.get("gates") or [], f"C20 (issue #74 AC3): an ACTIVE claim gates issue {gated}; gates {row.get('gates')}")
        for open_issue in (10, 11, 12):
            row = self._row(data_t, open_issue)
            self.assertIsNotNone(row, f"C20 (issue #74 AC3): triage --include-gated must show issue {open_issue}")
            self.assertNotIn(
                "claimed",
                row.get("gates") or [],
                f"C20 (issue #74 AC3): an expired/released claim must NOT gate issue {open_issue}; gates {row.get('gates')}",
            )


class ClaimConfigTests(_ClaimsStoreTest):
    """C21 (issue #74 AC4 riding #77 Design 8/AC14): no config -> built-in
    defaults in effect and echoed; per-key overrides honoured and echoed."""

    def _claims_module(self):
        try:
            import zaxbygraph.claims as claims_mod
        except ImportError as exc:
            self.fail(
                f"C21 (issue #74 AC4 / #77 Design 8): the module zaxbygraph.claims "
                f"must exist with load_claims_config/parse_claim/parse_release/"
                f"collect_claims; import failed: {exc}"
            )
        return claims_mod

    def test_claim_config_defaults_and_overrides(self) -> None:
        mod = self._claims_module()

        # (a) built-in defaults
        cfg = mod.load_claims_config()
        self.assertIsInstance(cfg, dict, "C21 (issue #74 AC4): load_claims_config must return a dict")
        for key in ("enabled", "patterns", "release_prefix", "token_prefix", "window_hours", "settle_seconds"):
            self.assertIn(key, cfg, f"C21 (issue #74 AC4): the claims config must carry `{key}`; keys are {sorted(cfg)}")
        self.assertIs(cfg["enabled"], True, "C21: claims are ON by default (Design 8)")
        lowered = [str(p).lower() for p in cfg["patterns"]]
        self.assertIn("agent claim", lowered, f"C21 (issue #74 AC4): the built-in patterns include `Agent claim` (case-insensitive); patterns {cfg['patterns']}")
        self.assertIn("working this issue", lowered, f"C21 (issue #74 AC4): the built-in patterns include `Working this issue`; patterns {cfg['patterns']}")
        self.assertEqual(cfg["release_prefix"], "**Claim released", "C21 (issue #74 AC4): the built-in release pattern is the `**Claim released` prefix")
        self.assertEqual(cfg["token_prefix"], "session=", "C21 (issue #74 AC4): the built-in session-token form is `session=<token>`")
        self.assertEqual(cfg["window_hours"], 72, "C21 (issue #74 AC4): the built-in window is 72 hours (AC9 decision)")
        self.assertEqual(float(cfg["settle_seconds"]), 2.0, "C21 (issue #74 AC4/#77 AC13): the built-in settle delay is 2 seconds")

        # (b) per-key overrides echoed ([TRACE-DESIGN]: explicit_config is a dict layer)
        explicit = {
            "enabled": False,
            "patterns": ["Staking this"],
            "release_prefix": "**Abandoned",
            "token_prefix": "run=",
            "window_hours": 1,
            "settle_seconds": 0.5,
        }
        over = mod.load_claims_config(explicit_config=dict(explicit))
        self.assertIs(over["enabled"], False, "C21: the enabled override must be honoured")
        self.assertEqual(over["patterns"], ["Staking this"], "C21: the patterns override must be honoured")
        self.assertEqual(over["release_prefix"], "**Abandoned", "C21: the release_prefix override must be honoured")
        self.assertEqual(over["token_prefix"], "run=", "C21: the token_prefix override must be honoured")
        self.assertEqual(over["window_hours"], 1, "C21: the window_hours override must be honoured")
        self.assertEqual(float(over["settle_seconds"]), 0.5, "C21: the settle_seconds override must be honoured")

        # (c) the parser honours patterns in effect
        parsed = mod.parse_claim("Staking this #9 now", over)
        self.assertIsInstance(parsed, dict, "C21: an overridden pattern must match")
        self.assertEqual(parsed.get("named_issues"), [9], "C21: named issues parse from the matched line")
        self.assertIsNone(parsed.get("token"), "C21: a body without a token parses token null")
        self.assertEqual(parsed.get("matched_text"), "Staking this #9 now", "C21: matched_text is the matched line")
        self.assertIsNone(mod.parse_claim("Agent claim now", over), "C21: with patterns overridden, a built-in-pattern body no longer matches")
        default_token = "PARSE-TOKEN-1234567890"
        tokened = mod.parse_claim(f"**Agent claim:** session={default_token} issues=#12,#7 done", cfg)
        self.assertIsInstance(tokened, dict, "C21: the built-in pattern matches the Design-1 body")
        self.assertEqual(tokened.get("token"), default_token, "C21: the session token parses")
        self.assertEqual(tokened.get("named_issues"), [7, 12], "C21: named issues parse from the issues= segment, SORTED")
        prose = mod.parse_claim("Working this issue now (with #3491 as one collated PR)", cfg)
        self.assertEqual(prose.get("named_issues"), [3491], "C21: named issues also parse from #N references in prose (the recorded form)")
        ci = mod.parse_claim("working THIS issue now", cfg)
        self.assertIsInstance(ci, dict, "C21: built-in patterns match case-insensitively")
        self.assertIsNone(mod.parse_claim("nothing to see here", cfg), "C21: a non-claim body parses to None")
        rel = mod.parse_release(f"**Claim released:** session={default_token} done", cfg)
        self.assertIsInstance(rel, dict, "C21: a release body starting with the prefix parses")
        self.assertEqual(rel.get("token"), default_token, "C21: the release token parses")
        self.assertIsNone(
            mod.parse_release("PS: **Claim released:** later", cfg),
            "C21: a release only matches a body that STARTS WITH the release prefix",
        )

        # (d) collect_claims honours window/enabled/trust over hand-built rows
        collect_db = self.root / "collect.db"
        conn = connect(collect_db)
        init_schema(conn)
        csrc = FakeGitHubSource()
        csrc.add_issue(issue(70, title="collect target", state="open"))
        sync_repo(conn, csrc, REPO)
        conn.execute(
            "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ? WHERE repo = ?",
            (ASOF, ASOF, REPO),
        )
        conn.commit()
        self.addCleanup(conn.close)
        rows = [
            (
                70,
                "alice",
                "MEMBER",
                _h(2),
                _claim_body("COLLECT-TOKEN-1234567890"),
                f"https://github.com/{REPO}/issues/70#issuecomment-1",
            )
        ]
        res_default = mod.collect_claims(conn, REPO, list(rows), cfg, ASOF)
        self.assertIsInstance(res_default, list, "C21: collect_claims returns a list")
        self.assertTrue(
            any(c.get("active") for c in res_default),
            "C21: a trusted in-window tokened claim is ACTIVE under the default window",
        )
        narrow = dict(cfg, window_hours=1)
        res_narrow = mod.collect_claims(conn, REPO, list(rows), narrow, ASOF)
        self.assertFalse(
            any(c.get("active") for c in res_narrow),
            "C21: with window_hours=1 the 2h-old claim is OUTSIDE the window (not active)",
        )
        off = dict(cfg, enabled=False)
        res_off = mod.collect_claims(conn, REPO, list(rows), off, ASOF)
        self.assertFalse(any(c.get("active") for c in res_off), "C21: with claims disabled no claim is active")
        untrusted_rows = [(70, "rando", "NONE", _h(2), _claim_body("COLLECT-TOKEN-1234567890"), "https://github.com/x/issue")]
        res_untrusted = mod.collect_claims(conn, REPO, untrusted_rows, cfg, ASOF)
        self.assertFalse(any(c.get("active") for c in res_untrusted), "C21: an untrusted author never holds an active claim")

        # (e) the CLI listing echoes the values in effect (no config file present)
        def seed_echo(src: FakeGitHubSource) -> None:
            src.add_issue(issue(60, title="echo target", state="open"))
            _comment_on(src, 60, 6010, _claim_body("ECHO-TOKEN-1234567890AB"), created_at=_h(2))

        self.seed(seed_echo)
        env = self.run_claims("C21 (issue #74 AC4)")
        data = env.get("data") or {}
        echoed = data.get("claims_config")
        self.assertIsInstance(echoed, dict, f"C21 (issue #74 AC4): the payload must echo claims_config beside the claims; data keys {sorted(data)}")
        self.assertEqual(echoed.get("window_hours"), 72, "C21: the echoed window is the built-in 72h when no config exists")
        self.assertIs(echoed.get("enabled"), True, "C21: the echoed switch is ON when no config exists")
        self.assertIn("agent claim", [str(p).lower() for p in echoed.get("patterns") or []], "C21: the echoed patterns include the built-ins")
        self.assertEqual(echoed.get("release_prefix"), "**Claim released", "C21: the echoed release prefix is the built-in one")
        self.assertEqual(echoed.get("token_prefix"), "session=", "C21: the echoed token prefix is the built-in one")
        self.assertEqual(float(echoed.get("settle_seconds")), 2.0, "C21: the echoed settle delay is the built-in 2s")
        self.assertTrue(data.get("config_source"), "C21 (issue #74 AC4): the payload names config_source (where the values came from)")


class ClaimsCommandTests(_ClaimsStoreTest):
    """C22 (issue #74 AC5): the CLI and MCP `claims` listing carries active
    and stale claims on open issues, newest first, with parsed fields, the
    matched text wrapped, marked derived:true."""

    def test_claims_command(self) -> None:
        token_a = "LIST-A-TOKEN-1234567890AB"
        token_b = "LIST-B-TOKEN-1234567890AB"

        def seed(src: FakeGitHubSource) -> None:
            src.add_issue(issue(20, title="claims listing 20", state="open"))
            _comment_on(src, 20, 2010, _claim_body(token_a, named=" issues=#21"), created_at=_h(2))
            src.add_issue(issue(21, title="claims listing 21", state="open"))
            src.add_issue(issue(22, title="claims listing 22", state="open"))
            _comment_on(src, 22, 2210, _claim_body("LIST-STALE-TOKEN-123456"), created_at=_h(100))
            src.add_issue(issue(23, title="claims listing 23", state="open"))
            _comment_on(src, 23, 2310, _claim_body(token_b), created_at=_h(1))

        self.seed(seed)
        env = self.run_claims("C22 (issue #74 AC5)")
        data = env.get("data") or {}
        self.assertIs(data.get("derived"), True, f"C22 (issue #74 AC5): the store-backed listing is marked derived:true; data keys {sorted(data)}")
        claims = data.get("claims")
        self.assertIsInstance(claims, list, "C22 (issue #74 AC5): data.claims must be a list")
        self.assertEqual(
            [c.get("number") for c in claims if c.get("active")],
            [23, 20],
            f"C22 (issue #74 AC5): active claims list NEWEST FIRST; got {[c.get('number') for c in claims]}",
        )
        entry = next((c for c in claims if c.get("number") == 20), None)
        self.assertIsNotNone(entry, f"C22 (issue #74 AC5): issue 20's claim must be listed; claims {claims}")
        self.assertEqual(entry.get("title"), "claims listing 20", "C22: the claim entry carries the issue title")
        self.assertEqual(entry.get("author"), "alice", "C22: the claim entry carries the author")
        self.assertEqual(entry.get("author_association"), "MEMBER", "C22: the claim entry carries the author association")
        self.assertIs(entry.get("trusted"), True, "C22: a MEMBER claim lists trusted:true")
        self.assertEqual(entry.get("token"), token_a, "C22: the claim entry carries the session token")
        self.assertEqual(entry.get("created_at"), _h(2), "C22: the claim entry carries the comment time")
        age = entry.get("age_hours")
        self.assertIsInstance(age, (int, float), f"C22: the claim entry carries age_hours; got {age!r}")
        self.assertTrue(1.0 <= float(age) <= 3.0, f"C22: the 2h-old claim reports ~2 age_hours measured from the store's sync time; got {age!r}")
        self.assertEqual(entry.get("named_issues"), [21], "C22: the claim entry carries the NAMED issues as parsed ints")
        self.assertEqual(entry.get("url"), f"https://github.com/{REPO}/issues/20#issuecomment-2010", "C22: the claim entry carries the comment URL")
        text = entry.get("text")
        self.assertIsInstance(text, dict, f"C22 (issue #74 AC5): the matched text is wrapped; got {text!r}")
        self.assertIn("untrusted_text", text, f"C22: the wrap key is untrusted_text; got {sorted(text)}")
        self.assertEqual(
            text.get("untrusted_text"),
            _claim_body(token_a, named=" issues=#21"),
            "C22: untrusted_text is the matched line verbatim",
        )
        stale = data.get("stale_claims")
        self.assertIsInstance(stale, list, "C22: data.stale_claims is a list")
        entry22 = next((s for s in stale if s.get("number") == 22), None)
        self.assertIsNotNone(entry22, f"C22 (issue #74 AC5): the stale claim on 22 is listed under stale_claims; got {stale}")
        self.assertTrue(72 < float(entry22.get("age_hours") or 0) < 200, "C22: the stale entry carries its age")

        # MCP leg: the claims tool answers the same derived listing.
        session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(self.db_path), stale_after_s=10**9)
        self.addCleanup(session.stop)
        session.initialize()
        _result, mcp_env = tool_envelope(
            self, session.call_tool("claims", {"repo": REPO}), "C22 (issue #74 AC5)"
        )
        mcp_data = mcp_env.get("data") or {}
        self.assertIs(mcp_data.get("derived"), True, f"C22 (issue #74 AC5): the MCP claims tool answers the derived listing; data keys {sorted(mcp_data)}")
        mcp_claims = mcp_data.get("claims") or []
        mcp_entry = next((c for c in mcp_claims if c.get("number") == 20), None)
        self.assertIsNotNone(mcp_entry, f"C22 (issue #74 AC5): the MCP claims listing includes issue 20's claim; claims {len(mcp_claims)}")
        self.assertIs(mcp_entry.get("trusted"), True, "C22: the MCP listing carries the trusted flag")
        session.stop()


class DeterministicGateTests(_ClaimsStoreTest):
    """C23 (issue #74 AC6): computing the claim tier twice on the same store
    is byte-identical apart from freshness, and the window is measured from
    the store's last sync time, never from the wall clock."""

    def test_claim_gate_is_deterministic(self) -> None:
        sync_token = "SYNC-TOKEN-1234567890AB"

        def seed(src: FakeGitHubSource) -> None:
            src.add_issue(issue(50, title="determinism target", state="open"))
            # created 10h before ASOF: inside the 72h window measured from the
            # store's sync time, but MONTHS past it measured from the wall clock.
            _comment_on(src, 50, 5010, _claim_body(sync_token), created_at=_h(10))

        self.seed(seed)
        env1 = self.run_claims("C23 (issue #74 AC6)")
        claims1 = (env1.get("data") or {}).get("claims")
        self.assertIsInstance(claims1, list, "C23 (issue #74 AC6): data.claims must be a list")
        entry = next((c for c in claims1 if c.get("number") == 50), None)
        self.assertIsNotNone(entry, f"C23 (issue #74 AC6): the seeded claim must be listed; claims {claims1}")
        self.assertIs(
            entry.get("active"),
            True,
            "C23 (issue #74 AC6): a claim 10h before the STORE'S LAST SYNC TIME is inside "
            "the 72h window -- the derived tier measures from the sync time, NOT the wall "
            "clock (this claim is months old in wall-clock terms)",
        )

        env2 = self.run_claims("C23 (issue #74 AC6)")
        norm1 = dict(env1)
        norm2 = dict(env2)
        norm1.pop("freshness", None)
        norm2.pop("freshness", None)
        self.assertEqual(
            json.dumps(norm1, sort_keys=True),
            json.dumps(norm2, sort_keys=True),
            "C23 (issue #74 AC6): the same store twice must give byte-identical claim "
            "output apart from freshness age",
        )


class SkillNamesClaimsTests(unittest.TestCase):
    """C25 (issue #74 AC8): the bundled skill states the claim/release
    formats and the window, and steers agents to `claims` instead of SQL
    sweeps and to `--max-age` before trusting an empty answer."""

    def test_skill_names_claims(self) -> None:
        self.assertTrue(_SKILL_PATH.is_file(), f"C25 (issue #74 AC8): the bundled skill must exist at {_SKILL_PATH}")
        text = _SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("**Agent claim:**", text, "C25 (issue #74 AC8): the skill must state the claim comment format the gate matches")
        self.assertIn("session=", text, "C25 (issue #74 AC8): the claim format names the session token form")
        self.assertIn("**Claim released", text, "C25 (issue #74 AC8): the skill must state the built-in release format")
        self.assertIsNotNone(
            re.search(r"(?i)72[^\n]{0,60}hours?|hours?[^\n]{0,40}72", text),
            "C25 (issue #74 AC8/AC9): the skill must state the 72-hour claim window in effect",
        )
        self.assertIsNotNone(
            re.search(r"(?is)\bclaims?\b[^.]{0,200}\b(sql|sweep)", text),
            "C25 (issue #74 AC8): the skill must tell agents to read `claims` instead of sweeping comments with SQL",
        )
        self.assertIsNotNone(
            re.search(r"(?is)max-age[^.]{0,200}\bempty\b|\bempty\b[^.]{0,200}max-age", text),
            "C25 (issue #74 AC8): the skill must tell agents to refresh with --max-age before trusting an empty claims answer",
        )


class UntrustedAuthorGateTests(_ClaimsStoreTest):
    """C26 (issue #74 AC10): an untrusted-author claim comment on an open
    issue never gates it and appears in `claims` with trusted:false."""

    def test_untrusted_author_does_not_gate(self) -> None:
        untrusted_token = "UNTRUSTED-GATE-TOKEN-1"

        def seed(src: FakeGitHubSource) -> None:
            src.add_issue(issue(30, title="untrusted target", state="open"))
            _comment_on(
                src,
                30,
                3010,
                _claim_body(untrusted_token),
                author="rando",
                association="NONE",
                created_at=_h(1),
            )

        self.seed(seed)
        env = self.run_claims("C26 (issue #74 AC10)")
        claims = (env.get("data") or {}).get("claims")
        self.assertIsInstance(claims, list, "C26 (issue #74 AC10): data.claims must be a list")
        entry = next((c for c in claims if c.get("number") == 30), None)
        self.assertIsNotNone(entry, f"C26 (issue #74 AC10): the untrusted claim must still be LISTED in claims; claims {claims}")
        self.assertIs(entry.get("trusted"), False, "C26 (issue #74 AC10): an untrusted-author claim lists trusted:false")
        self.assertIs(entry.get("active"), False, "C26 (issue #74 AC10): an untrusted-author claim is never active")

        data = self.run_triage_rows("C26")
        row = self._row(data, 30)
        self.assertIsNotNone(row, "C26 (issue #74 AC10): triage --include-gated must show issue 30")
        self.assertNotIn(
            "claimed",
            row.get("gates") or [],
            f"C26 (issue #74 AC10): a NONE-association claim must NOT gate issue 30; gates {row.get('gates')}",
        )


class ClaimedGateEvidenceTests(_ClaimsStoreTest):
    """Suite coverage (supports issue #74 AC1): the `claimed` gate carries
    the claim's author, token, time and URL as PARSED evidence fields."""

    def test_claimed_gate_evidence_fields(self) -> None:
        evidence_token = "EVIDENCE-TOKEN-1234567"
        claim_time = _h(1)
        claim_url = f"https://github.com/{REPO}/issues/40#issuecomment-4010"

        conn = connect(self.db_path)
        init_schema(conn)
        src = FakeGitHubSource()
        src.add_issue(issue(40, title="evidence target", state="open"))
        rec = comment(4010, _claim_body(evidence_token), author="bob", created_at=claim_time)
        rec["author_association"] = "MEMBER"
        rec["html_url"] = claim_url
        src.issue_comments.setdefault(40, []).append(rec)
        src.issues[40]["comments"] = 1
        sync_repo(conn, src, REPO)
        conn.execute(
            "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ? WHERE repo = ?",
            (ASOF, ASOF, REPO),
        )
        conn.commit()
        conn.close()

        data = self.run_triage_rows("SUITE-EVIDENCE (issue #74 AC1)")
        row = self._row(data, 40)
        self.assertIsNotNone(row, "SUITE-EVIDENCE (issue #74 AC1): triage --include-gated must show issue 40")
        self.assertIn(
            "claimed",
            row.get("gates") or [],
            f"SUITE-EVIDENCE (issue #74 AC1): an active trusted claim gates issue 40 with gate `claimed`; gates {row.get('gates')}",
        )
        evidence = _find_dict_containing(data, {"author": "bob", "token": evidence_token})
        self.assertIsNotNone(
            evidence,
            "SUITE-EVIDENCE (issue #74 AC1): the claimed gate must carry PARSED evidence "
            "fields (author and session token), not just the matched text",
        )
        ev_time = evidence.get("created_at") or evidence.get("time")
        self.assertEqual(ev_time, claim_time, "SUITE-EVIDENCE (issue #74 AC1): the gate evidence carries the claim's TIME as a parsed field")
        ev_url = evidence.get("url") or evidence.get("html_url")
        self.assertEqual(ev_url, claim_url, "SUITE-EVIDENCE (issue #74 AC1): the gate evidence carries the claim comment's URL")


class ClaimsMcpToolTests(_ClaimsStoreTest):
    """Suite coverage: the MCP `claims` tool returns the derived listing
    envelope and is advertised read-only."""

    def test_claims_mcp_tool_lists_claims(self) -> None:
        mcp_token = "MCP-LIST-TOKEN-1234567"

        def seed(src: FakeGitHubSource) -> None:
            src.add_issue(issue(80, title="mcp listing target", state="open"))
            _comment_on(src, 80, 8010, _claim_body(mcp_token), created_at=_h(1))

        self.seed(seed)
        session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(self.db_path), stale_after_s=10**9)
        self.addCleanup(session.stop)
        session.initialize()
        response = session.call_tool("claims", {"repo": REPO})
        self.assertNotIn(
            "error",
            response,
            f"SUITE-MCPLIST: the MCP `claims` tool must exist (issue #74 AC5); got {response!r}",
        )
        _result, env = tool_envelope(self, response, "SUITE-MCPLIST")
        self.assertIs(env.get("ok"), True, f"SUITE-MCPLIST: the claims tool answers ok:true; got {env}")
        data = env.get("data") or {}
        claims = data.get("claims")
        self.assertIsInstance(claims, list, f"SUITE-MCPLIST: the claims tool returns the claims listing envelope; data keys {sorted(data)}")
        entry = next((c for c in claims if c.get("number") == 80), None)
        self.assertIsNotNone(entry, f"SUITE-MCPLIST: the MCP listing includes the seeded claim; claims {claims}")
        self.assertIs(entry.get("active"), True, "SUITE-MCPLIST: the 1h-old trusted claim is active")

        # the claims tool is a read: advertised readOnlyHint true.
        req_id = session.request("tools/list")
        listing = session.wait_response(req_id)
        tools = (listing.get("result") or {}).get("tools") or []
        hints = {t.get("name"): (t.get("annotations") or {}).get("readOnlyHint") for t in tools if isinstance(t, dict)}
        self.assertIs(hints.get("claims"), True, f"SUITE-MCPLIST: the claims tool is read-only (readOnlyHint true); hints {hints}")
        session.stop()


if __name__ == "__main__":
    unittest.main()
