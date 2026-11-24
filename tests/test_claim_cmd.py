"""Acceptance checks for issue-tracer trace 77-74-claims-gate, file 1 of 2:
the live claim surface of issue #77 (`claim`, `release`, `pr-guard`,
`claims --live`, the MCP tool annotations and the bundled skill text).

Authored at arm's length from the issue body ONLY (issue #77 Summary +
Design 1-13 + AC1-AC11, plus the trace's exit-code decisions). No
implementation was read -- none exists at the base commit. Every test is
RED at base, and each test's FIRST failing assertion carries its C-tag
("C1:", "C2:", ...) so a frozen --expect can pin one line.

AC -> -k method map (issue #77):
  AC1  C1  test_claim_posts_on_every_named_issue
  AC2  C2  test_claim_refuses_taken_issue
  AC3  C3  test_claim_loser_releases_everything
  AC4  C4  test_partial_post_is_rolled_back
  AC5  C5  test_release_matches_token
  AC6  C6  test_pr_guard_finds_open_closer
  AC7  C7  test_claims_live_view
  AC8  C8  test_untrusted_author_cannot_hold_a_claim
  AC9  C9  test_comment_text_is_wrapped
  AC10 C10 test_mcp_write_tools_are_not_read_only
  AC11 C11 test_skill_requires_claim_and_pr_guard
  AC12     PRESERVING, GREEN at base -- NOT authored here. It is the
           existing tests/test_similar.py::test_derived_tier_never_writes
           (the derived tier never writes the store); this trace's legs
           run that argv; nothing in this file duplicates it.
  AC13     recorded product decision (settle 2s; ordering by creation time
           then comment id) -- no executable check.
  AC14 C13 test_claim_config_defaults_and_overrides -- the #77 command-
           surface half (claim/release/claims --live payloads echo the
           values in effect, built-in or config-chain). The #74 derived-
           tier half is tests/test_claims.py C21, the SAME selector name
           in a different file (the frozen argves are per-file).
  AC15 C14 test_claim_not_applicable_posts_nothing
  AC16 C15 test_outcomes_have_distinct_exit_codes
  AC17 C16 test_tokenless_claim_and_release
  AC18 C17 test_claims_post_in_issue_order_and_retry_once

CONTRACT THESE TESTS PIN (each point is issue-derived, or explicitly
flagged [TRACE-DESIGN] where the issue left the mechanical detail open):

1. Module seam: a new `zaxbygraph.claims` module performs every GitHub
   interaction through a gh runner, `run_gh(command_list) ->
   (rc, stdout, stderr)`, injectable per call and DEFAULTING to a
   module-level attribute `zaxbygraph.claims.run_gh` that the command
   functions resolve at CALL time (late binding). The tests redirect the
   CLI offline by assigning `zaxbygraph.claims.run_gh = fake`.
   [TRACE-DESIGN ASSUMPTION: the attribute NAME `run_gh`; the issue pins
   only the injectable parameter.]

2. gh command shapes the in-test FakeGh recognizes (routed on semantics,
   not exact argv):
   - per-issue comment listing: a read whose argv contains
     `issues/<N>/comments`;
   - repo-wide comment listing (Design 12): a read whose argv contains
     `issues/comments` with no `<N>`; payload entries carry `issue_url`;
   - issue record read: `issues/<N>` (or `issue <N>`), no `/comments`;
   - open-PR listing: argv containing `pulls` or a bare `pr` word;
   - comment post: an argv element containing `**Agent claim` or
     `**Claim released` (optionally `body=`-prefixed), with the target
     issue number in `issues/<N>/comments`, `issues/<N>`, `comment <N>`
     or `issue <N>`.
   Comment payloads carry id, body, user.login, author_association,
   created_at, html_url. PR payloads carry number, state, title, body
   (`closes #N`), user.login, closing_issues (ints) and
   closingIssueReferences.nodes -- the implementation may read any of
   these.

3. Exit codes [TRACE-DESIGN]: outcome claimed/not_applicable exit 0;
   taken / lost_race / github_unreachable each exit a DISTINCT code,
   none in {0, 1, 2, 3}. No literal is pinned anywhere.

4. CLI: subcommands `claim N... [--settle-seconds S]`, `release N...
   --token TOK`, `pr-guard N...`, `claims [--live]`, each printing the
   repo envelope {ok, db, repo, freshness, data, truncated} with
   data.outcome one of claimed/not_applicable/taken/lost_race/
   github_unreachable (Design 10). A refusal names the `sync --numbers`
   command line. [TRACE-DESIGN ASSUMPTION: the release token flag is
   `--token`.] The repo under test, acme/forgegate, does not exist on
   GitHub: even a seam miss cannot post anything real. Every claim
   command's data payload echoes data.claims_config (enabled, patterns,
   release_prefix, token_prefix, window_hours, settle_seconds) and
   data.config_source beside the outcome (AC14); the config chain is a
   `.zaxbygraph.toml` in the CWD with a `[claims]` section (contract
   point 8). An explicit --settle-seconds changes the value IN EFFECT
   and therefore the echo -- the C13 legs pass no flag so the built-in
   (2.0) and the config override (1) echo verbatim.

5. MCP tool names: claim, release, pr_guard, claims.

6. Token charset: session tokens match [A-Za-z0-9-]{16,64} (Design 1/8).

7. Tokenless claims/releases (Design 9 / AC17): a trusted in-window claim
   without a session token is an active claim held by NO token;
   `release --tokenless` posts `**Claim released:**` (no session= segment)
   and ends only tokenless claims; a token release never ends a tokenless
   claim and a tokenless release never ends a tokened one.

8. Login-trust precheck (Design 11 / AC15; probe settled in the
   orchestrating session on ZaxbyHub/zaxbygraph: the session login has
   viewerPermission ADMIN and orgs/<owner>/members/<login> answers 204,
   and its posted comments carry author_association MEMBER). The session
   login is a trusted claim author iff (a) login == the repo owner's
   login, OR (b) GET orgs/<owner>/members/<login> answers HTTP 204, OR
   (c) viewerPermission is ADMIN, MAINTAIN or WRITE. A login failing all
   three makes `claim` report outcome not_applicable with the reason and
   post NOTHING (exit 0). FakeGh serves the probe calls: `user` ->
   {"login": viewer_login}; orgs/<o>/members/<l> -> .org_member_status
   (204/404); graphql/viewerPermission queries -> .viewer_permission; a
   bare repos/<o>/<r> read -> {"owner": {"login": owner_login}}. FakeGh
   DEFAULTS are trusted (ADMIN + 204) so every other live test's claim
   proceeds; the not_applicable legs flip them (READ + 404).
   The claims-off config leg pins the config-file shape [TRACE-DESIGN]:
   a `.zaxbygraph.toml` in the CWD with a `[claims]` section and
   `enabled = false` (the repo's existing TOML-section convention).

9. Timed competitor reveals (C17): FakeGh.reveal_timed maps an issue to
   (min_claim_posts_to_issue, lifetime_seconds, [comments]) -- the
   competitor's comment is visible on a comments read only while at least
   `min_claim_posts` claim posts were made to that issue AND less than
   `lifetime_seconds` have passed since the Nth claim post. With
   --settle-seconds 0.05 and Design 3's random 1-5s pre-retry wait, a
   1.0s lifetime is visible at the settle read and gone at the retry
   read; a 30s lifetime from the SECOND claim wave models a fresh
   competitor for the second race.

Hermetic: FakeGh only (no gh subprocess, no network), scratch stores via
fixtures.FakeGitHubSource under tempfile dirs, Windows-safe.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, issue
from test_mcp import McpSession, pin_env_for
from zaxbygraph.cli import main
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

_SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "zaxbygraph" / "SKILL.md"
_TOKEN_RE = re.compile(r"session=([A-Za-z0-9-]{16,64})")


# --------------------------------------------------------------------------
# local helpers (duplicated per file by trace convention; fixtures.py and
# test_mcp.py are not modified)
# --------------------------------------------------------------------------


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


def _parse_env(test: unittest.TestCase, out: str, code: int, err: str, tag: str) -> dict:
    """Fail with `tag` unless stdout is one JSON envelope object."""
    try:
        env = json.loads(out)
    except ValueError:
        test.fail(
            f"{tag}: the command must exist and print one JSON envelope on stdout "
            f"(issue #77); got exit {code} and unparseable stdout {out[:120]!r} "
            f"(stderr {err.strip()[:160]!r})"
        )
    test.assertIsInstance(env, dict, f"{tag}: the envelope must be a JSON object; got {type(env).__name__}")
    return env


def _outcome(env: dict):
    data = env.get("data") if isinstance(env.get("data"), dict) else {}
    return data.get("outcome") or env.get("outcome")


def _iso_hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def claim_body(token: str | None = None, named: tuple[int, ...] = (), free: str = " Working this issue now.") -> str:
    """A Design-1 claim body (tokened) or a recorded-usage tokenless one."""
    if token is None and not named:
        # the recorded convention (issue #74 claim 1): pattern-2 wording only
        return "Working this issue now (collated PR in progress)."
    parts = ["**Agent claim:**"]
    if token:
        parts.append(f"session={token}")
    if named:
        parts.append("issues=" + ",".join(f"#{n}" for n in named))
    return " ".join(parts) + free


def release_body(token: str | None = None) -> str:
    if token is None:
        return "**Claim released:** scope split."
    return f"**Claim released:** session={token} done."


def _foreign_claim(cid: int, token: str | None, hours_ago: float = 1.0) -> dict:
    """A competitor's claim comment, EARLIER than any post we make 'now'."""
    return {
        "id": cid,
        "body": claim_body(token),
        "user": {"login": "alice"},
        "author_association": "MEMBER",
        "created_at": _iso_hours_ago(hours_ago),
        "updated_at": _iso_hours_ago(hours_ago),
        "html_url": f"https://github.com/{REPO}/issues/0#issuecomment-{cid}",
    }


class FakeGh:
    """Offline stand-in for the module gh runner (contract point 2 above).

    Records every call in `.calls`; recognized posts land in `.posts` as
    (issue_number, body); reads are served from seeded state. The repo
    under test does not exist on GitHub, so nothing here can touch a real
    repository even if the seam is missed.
    """

    def __init__(self, repo: str = REPO) -> None:
        self.repo = repo
        self.calls: list[list[str]] = []
        self.comments: dict[int, list[dict]] = {}
        self.repo_comments: list[dict] | None = None
        self.open_prs: list[dict] = []
        self.issue_state: dict[int, str] = {}
        self.posts: list[tuple[int, str]] = []
        self.fail_posts_for: set[int] = set()      # claim posts that fail
        self.fail_release_for: set[int] = set()    # release posts that fail
        self.reveal_after_post: dict[int, list[dict]] = {}  # persistent in-flight competitor
        # login-trust probe answers (contract point 8); defaults are TRUSTED
        self.viewer_login = "me"
        self.owner_login = repo.split("/")[0]
        self.viewer_permission = "ADMIN"
        self.org_member_status = 204
        self.fail_all = False  # every gh call answers a network-dead shape
        # timed competitor reveals (contract point 9):
        # issue -> [(min_claim_posts_to_issue, lifetime_seconds, [comments])]
        self.reveal_timed: dict[int, list[tuple[int, float, list[dict]]]] = {}
        self._claim_post_times: dict[int, list[float]] = {}
        self._next_id = 900_000

    # -- seeding ------------------------------------------------------------
    def add_comment(
        self,
        number: int,
        body: str,
        *,
        author: str = "alice",
        association: str = "MEMBER",
        hours_ago: float = 2.0,
    ) -> dict:
        rec = {
            "id": self._next_id,
            "body": body,
            "user": {"login": author, "html_url": f"https://github.com/{author}"},
            "author_association": association,
            "created_at": _iso_hours_ago(hours_ago),
            "updated_at": _iso_hours_ago(hours_ago),
            "html_url": f"https://github.com/{self.repo}/issues/{number}#issuecomment-{self._next_id}",
        }
        self._next_id += 1
        self.comments.setdefault(number, []).append(rec)
        return rec

    def add_claim(self, number: int, token: str | None, **kwargs) -> dict:
        return self.add_comment(number, claim_body(token), **kwargs)

    def add_repo_comment(self, number: int, body: str, **kwargs) -> dict:
        """Seed both the per-issue view and the repo-wide (Design 12) view."""
        rec = self.add_comment(number, body, **kwargs)
        if self.repo_comments is None:
            self.repo_comments = []
        wide = dict(rec)
        wide["issue_url"] = f"https://api.github.com/repos/{self.repo}/issues/{number}"
        wide["issue_number"] = number
        self.repo_comments.append(wide)
        return rec

    # -- routing --------------------------------------------------------------
    def _post_body(self, cmd: list[str]) -> str | None:
        for arg in cmd:
            if "**Agent claim" in arg or "**Claim released" in arg:
                for prefix in ("body=", "--body=", "-f body=", "--field body="):
                    if arg.startswith(prefix):
                        return arg[len(prefix):]
                return arg
        return None

    def _target_number(self, joined: str) -> int | None:
        for pattern in (
            r"issues/(\d+)/comments",
            r"issues/(\d+)",
            r"\bcomment\s+(\d+)",
            r"\bissues?\s+(\d+)",
        ):
            m = re.search(pattern, joined)
            if m:
                return int(m.group(1))
        return None

    @staticmethod
    def _is_pr_read(joined: str) -> bool:
        return "pulls" in joined or "pr list" in joined or re.search(r"(^|\s)pr(\s|$)", joined) is not None

    def _visible(self, number: int) -> list[dict]:
        visible = list(self.comments.get(number, []))
        if self.posts and number in self.reveal_after_post:
            visible = visible + self.reveal_after_post[number]
        times = self._claim_post_times.get(number, [])
        now = time.monotonic()
        for min_posts, lifetime, entries in self.reveal_timed.get(number, ()):
            if len(times) >= min_posts and (now - times[min_posts - 1]) < lifetime:
                visible = visible + entries
        return visible

    def __call__(self, cmd) -> tuple[int, str, str]:
        cmd = [str(a) for a in cmd]
        self.calls.append(list(cmd))
        if self.fail_all:
            return (1, "", "gh: dial tcp: lookup api.github.com: no such host (network unreachable)")
        joined = " ".join(cmd)
        body = self._post_body(cmd)
        if body is not None:
            number = self._target_number(joined)
            if number is None:
                return (1, "", "gh: fake found no issue target in post")
            if "**Claim released" in body and number in self.fail_release_for:
                return (1, "", "gh: release post failed HTTP 502")
            if number in self.fail_posts_for:
                return (1, "", "gh: claim post failed HTTP 502")
            created = {
                "id": self._next_id,
                "body": body,
                "user": {"login": self.viewer_login},
                "author_association": "MEMBER",
                "created_at": _iso_hours_ago(0.0),
                "html_url": f"https://github.com/{self.repo}/issues/{number}#issuecomment-{self._next_id}",
            }
            self._next_id += 1
            self.comments.setdefault(number, []).append(created)
            self.posts.append((number, body))
            if "**Agent claim" in body:
                self._claim_post_times.setdefault(number, []).append(time.monotonic())
            return (0, json.dumps(created), "")
        # -- login-trust probes (contract point 8) --------------------------
        if re.search(r"orgs/[\w.-]+/members/", joined):
            if self.org_member_status == 204:
                return (204, "", "")
            return (404, "", "Not Found")
        if "graphql" in joined or "viewerpermission" in joined.lower():
            payload = {
                "data": {"repository": {"viewerPermission": self.viewer_permission}},
                "viewerPermission": self.viewer_permission,
            }
            return (0, json.dumps(payload), "")
        if re.search(r"(^|\s)user(\s|$)", joined):
            return (0, json.dumps({"login": self.viewer_login}), "")
        # -- issue/comment reads --------------------------------------------
        m = re.search(r"issues/(\d+)/comments", joined)
        if m:
            return (0, json.dumps(self._visible(int(m.group(1)))), "")
        if "issues/comments" in joined:
            wide = self.repo_comments
            if wide is None:
                wide = []
                for n, recs in self.comments.items():
                    for rec in recs:
                        entry = dict(rec)
                        entry["issue_url"] = f"https://api.github.com/repos/{self.repo}/issues/{n}"
                        entry["issue_number"] = n
                        wide.append(entry)
            return (0, json.dumps(wide), "")
        m = re.search(r"issues/(\d+)", joined) or re.search(r"(?:^|\s)issues?\s+(\d+)", joined)
        if m:
            n = int(m.group(1))
            rec = {
                "number": n,
                "state": self.issue_state.get(n, "open"),
                "title": f"issue {n}",
                "html_url": f"https://github.com/{self.repo}/issues/{n}",
            }
            return (0, json.dumps(rec), "")
        m = re.search(r"repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?![\w/-])", joined)
        if m:
            return (
                0,
                json.dumps(
                    {
                        "owner": {"login": self.owner_login},
                        "full_name": f"{m.group(1)}/{m.group(2)}",
                        "name": m.group(2),
                    }
                ),
                "",
            )
        if self._is_pr_read(joined):
            return (0, json.dumps(self.open_prs), "")
        return (1, "", f"gh: fake cannot route: {joined[:140]}")


@contextlib.contextmanager
def _patched_gh(test: unittest.TestCase, tag: str, fake: FakeGh):
    """Point zaxbygraph.claims.run_gh at `fake` for the duration."""
    try:
        import zaxbygraph.claims as claims_mod
    except ImportError as exc:
        test.fail(
            f"{tag}: the module zaxbygraph.claims must exist and expose the "
            f"module-level gh runner attribute `run_gh` (issue #77 Design 8, "
            f"injectable run_gh seam); import failed: {exc}"
        )
    saved = getattr(claims_mod, "run_gh", None)
    claims_mod.run_gh = fake
    try:
        yield fake
    finally:
        if saved is None:
            with contextlib.suppress(AttributeError):
                delattr(claims_mod, "run_gh")
        else:
            claims_mod.run_gh = saved


class _LiveCmdTest(unittest.TestCase):
    """Scratch root + pinned env + a seeded minimal store, no network."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = self.root / "history.db"
        conn = connect(self.db_path)
        init_schema(conn)
        src = FakeGitHubSource()
        src.add_issue(issue(1, title="scratch"))
        sync_repo(conn, src, REPO)
        conn.close()
        self._saved_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(lambda: os.chdir(self._saved_cwd))
        pin_env_for(self, self.root, db=self.db_path)

    def _db_sha(self) -> str:
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()


class ClaimPostsTests(_LiveCmdTest):
    """C1 (issue #77 AC1): `claim` posts one Design-1 comment per named
    issue, prints the session token and the comment URLs, exits 0 with
    outcome claimed."""

    def test_claim_posts_on_every_named_issue(self) -> None:
        fake = FakeGh()
        with _patched_gh(self, "C1", fake):
            code, out, err = _run_cli(
                ["claim", "5", "6", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        env = _parse_env(self, out, code, err, "C1 (issue #77 AC1): the `claim` command")
        self.assertEqual(code, 0, f"C1 (issue #77 AC1): a successful claim must exit 0; got {code} ({err.strip()[:160]!r})")
        self.assertEqual(_outcome(env), "claimed", f"C1 (issue #77 AC1): the outcome must be `claimed`; got {_outcome(env)!r}")

        claim_posts = [(n, b) for n, b in fake.posts if "**Agent claim" in b]
        self.assertEqual(
            [n for n, _ in claim_posts],
            [5, 6],
            f"C1 (issue #77 AC1): claim must post ONE comment per named issue in ascending "
            f"order (Design 1/AC18); posts recorded: {fake.posts}",
        )
        tokens = set()
        for n, body in claim_posts:
            m = _TOKEN_RE.search(body)
            self.assertIsNotNone(
                m,
                f"C1 (issue #77 AC1): the posted body must carry `session=<token>` with charset "
                f"[A-Za-z0-9-]{{16,64}}; body was {body!r}",
            )
            tokens.add(m.group(1))
            seg = re.search(r"issues=(\S+)", body)
            self.assertIsNotNone(seg, f"C1 (issue #77 AC1): the posted body must carry the Design-1 `issues=#a,#b` segment; body was {body!r}")
            self.assertEqual(
                sorted(int(x) for x in re.findall(r"\d+", seg.group(1))),
                [5, 6],
                f"C1 (issue #77 AC1): the issues= segment must name every claimed issue; body was {body!r}",
            )
        self.assertEqual(len(tokens), 1, f"C1 (issue #77 AC1): one claim = one session token across all posts; saw {tokens}")
        token = tokens.pop()
        dump = json.dumps(env)
        self.assertIn(token, dump, "C1 (issue #77 AC1): the envelope must print the session token")
        for n in (5, 6):
            self.assertIn(
                f"issues/{n}#issuecomment-",
                dump,
                f"C1 (issue #77 AC1): the envelope must print the comment URL for every named issue (missing #{n})",
            )


class ClaimTakenTests(_LiveCmdTest):
    """C2 (issue #77 AC2): a live pre-read that finds a foreign active claim
    or an open closing PR posts nothing, reports `taken`, names the holder
    token (null for tokenless) or PR number, and exits the taken code."""

    def test_claim_refuses_taken_issue(self) -> None:
        # leg A: a foreign tokened claim holds issue 5
        fake_a = FakeGh()
        foreign_token = "FOREIGN-TOKEN-1234567890AB"
        fake_a.add_claim(5, foreign_token)
        with _patched_gh(self, "C2", fake_a):
            code_a, out, err = _run_cli(["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"])
        env_a = _parse_env(self, out, code_a, err, "C2 (issue #77 AC2): the `claim` command")
        self.assertEqual(_outcome(env_a), "taken", f"C2 (issue #77 AC2): the outcome must be `taken`; got {_outcome(env_a)!r}")
        self.assertNotIn(
            code_a,
            (0, 1, 2, 3),
            f"C2 (issue #77 AC2): `taken` must exit with its OWN new code outside {{0,1,2,3}}; got {code_a}",
        )
        self.assertEqual(fake_a.posts, [], "C2 (issue #77 AC2): a taken refusal must POST NOTHING")
        self.assertIn(foreign_token, json.dumps(env_a), "C2 (issue #77 AC2): the refusal must name the holder's session token")
        self.assertIn("sync --numbers", json.dumps(env_a), "C2 (issue #77 AC2): the refusal must name the `sync --numbers` command line (Design 10)")

        # leg B: an open PR that closes the named issue holds it
        fake_b = FakeGh()
        fake_b.open_prs = [
            {
                "number": 30,
                "state": "open",
                "title": "fix 5",
                "body": "closes #5",
                "user": {"login": "bob"},
                "closing_issues": [5],
                "closingIssueReferences": {"nodes": [{"number": 5}]},
            }
        ]
        with _patched_gh(self, "C2", fake_b):
            code_b, out_b, err_b = _run_cli(["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"])
        env_b = _parse_env(self, out_b, code_b, err_b, "C2 (issue #77 AC2): the `claim` command")
        self.assertEqual(_outcome(env_b), "taken", f"C2 (issue #77 AC2): an open closing PR must also refuse with `taken`; got {_outcome(env_b)!r}")
        self.assertEqual(code_b, code_a, "C2 (issue #77 AC2): both refusals are `taken` and must share ONE exit code")
        self.assertEqual(fake_b.posts, [], "C2 (issue #77 AC2): the PR-held refusal must post nothing")
        self.assertIn("30", json.dumps(env_b), "C2 (issue #77 AC2): the refusal must name the open closing PR's number")

        # leg C: a tokenless foreign claim counts (Design 9) and is named with a null token
        fake_c = FakeGh()
        fake_c.add_claim(5, None)
        with _patched_gh(self, "C2", fake_c):
            code_c, out_c, err_c = _run_cli(["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"])
        env_c = _parse_env(self, out_c, code_c, err_c, "C2 (issue #77 AC2): the `claim` command")
        self.assertEqual(_outcome(env_c), "taken", f"C2 (issue #77 AC2): a tokenless claim still holds the issue (Design 9); got {_outcome(env_c)!r}")
        self.assertEqual(code_c, code_a, "C2 (issue #77 AC2): the tokenless refusal is still `taken` (same exit code)")
        self.assertEqual(fake_c.posts, [], "C2 (issue #77 AC2): the tokenless refusal must post nothing")
        self.assertIsNotNone(
            re.search(r'"[^"]*token[^"]*"\s*:\s*null', json.dumps(env_c)),
            "C2 (issue #77 AC2): the refusal must name the holder's token as null when the claim carries none",
        )


class ClaimLostRaceTests(_LiveCmdTest):
    """C3 (issue #77 AC3): a read-after-post that finds an EARLIER foreign
    claim makes the loser release every issue it posted (release carrying
    its own token), name the winner, retry at most once, and exit the
    lost_race code when it does not hold all."""

    def test_claim_loser_releases_everything(self) -> None:
        winner_token = "WINNER-TOKEN-1234567890XYZ"
        fake = FakeGh()
        # The competitor's POST is in flight during our pre-read and lands
        # (earlier by creation time) before our re-read.
        fake.reveal_after_post = {
            6: [
                {
                    "id": 888_001,
                    "body": claim_body(winner_token),
                    "user": {"login": "alice"},
                    "author_association": "MEMBER",
                    "created_at": _iso_hours_ago(1.0),
                    "updated_at": _iso_hours_ago(1.0),
                    "html_url": f"https://github.com/{REPO}/issues/6#issuecomment-888001",
                }
            ]
        }
        with _patched_gh(self, "C3", fake):
            code, out, err = _run_cli(["claim", "5", "6", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"])
        env = _parse_env(self, out, code, err, "C3 (issue #77 AC3): the `claim` command")

        claim_posts = [(n, b) for n, b in fake.posts if "**Agent claim" in b]
        release_posts = [(n, b) for n, b in fake.posts if "**Claim released" in b]
        self.assertEqual(
            sorted(n for n, _ in claim_posts),
            [5, 6],
            f"C3 (issue #77 AC3): the session must post its claim on every named issue "
            f"and, after losing #6, RETRY AT MOST ONCE (no extra claim posts); posts: {fake.posts}",
        )
        self.assertEqual(len(claim_posts), 2, f"C3 (issue #77 AC3): at most one posting round; saw {len(claim_posts)} claim posts")
        m = _TOKEN_RE.search(claim_posts[0][1])
        self.assertIsNotNone(m, f"C3 (issue #77 AC3): the claim post must carry our session token; body {claim_posts[0][1]!r}")
        our_token = m.group(1)
        self.assertEqual(
            sorted(n for n, _ in release_posts),
            [5, 6],
            f"C3 (issue #77 AC3): the loser must RELEASE EVERY issue it posted (5 and 6), "
            f"not just the lost one; release posts: {release_posts}",
        )
        for n, body in release_posts:
            self.assertTrue(
                body.startswith(f"**Claim released:** session={our_token}"),
                f"C3 (issue #77 AC3): the rollback release on #{n} must carry OUR token ({our_token}); body {body!r}",
            )
        self.assertIn(winner_token, json.dumps(env), "C3 (issue #77 AC3): the loser must NAME THE WINNER (its session token)")
        self.assertEqual(_outcome(env), "lost_race", f"C3 (issue #77 AC3): the outcome must be `lost_race`; got {_outcome(env)!r}")
        self.assertNotIn(
            code,
            (0, 1, 2, 3),
            f"C3 (issue #77 AC3): `lost_race` must exit with its OWN new code outside {{0,1,2,3}}; got {code}",
        )

        # contrast leg: the same foreign claim visible BEFORE posting is `taken`,
        # and taken/lost_race must be DISTINCT codes.
        fake2 = FakeGh()
        fake2.add_claim(6, winner_token)
        with _patched_gh(self, "C3", fake2):
            code2, _out2, _err2 = _run_cli(["claim", "5", "6", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"])
        self.assertEqual(fake2.posts, [], "C3 (issue #77 AC3): a pre-read refusal must post nothing")
        self.assertNotEqual(
            code,
            code2,
            f"C3 (issue #77 AC3/AC16): taken ({code2}) and lost_race ({code}) must be DISTINCT exit codes",
        )


class PartialPostTests(_LiveCmdTest):
    """C4 (issue #77 AC4): a post that fails mid-bundle releases the claims
    already posted, exits non-zero, and lists any comment it could not
    release."""

    def test_partial_post_is_rolled_back(self) -> None:
        # leg 1: posting fails on the second of three named issues; the
        # release of the first succeeds.
        fake = FakeGh()
        fake.fail_posts_for = {6}
        with _patched_gh(self, "C4", fake):
            code, out, err = _run_cli(
                ["claim", "5", "6", "7", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        env = _parse_env(self, out, code, err, "C4 (issue #77 AC4): the `claim` command")
        claim_posts = [(n, b) for n, b in fake.posts if "**Agent claim" in b]
        release_posts = [(n, b) for n, b in fake.posts if "**Claim released" in b]
        self.assertEqual(
            [n for n, _ in claim_posts],
            [5],
            f"C4 (issue #77 AC4): posting in ascending order must stop at the failed issue "
            f"(#6) and never reach #7; claim posts: {claim_posts}",
        )
        self.assertEqual([n for n, _ in release_posts], [5], f"C4 (issue #77 AC4): the already-posted claim on #5 must be RELEASED; posts: {fake.posts}")
        m = _TOKEN_RE.search(claim_posts[0][1])
        self.assertTrue(
            m and release_posts and release_posts[0][1].startswith(f"**Claim released:** session={m.group(1)}"),
            f"C4 (issue #77 AC4): the rollback release must carry the session's own token; posts: {fake.posts}",
        )
        self.assertNotEqual(code, 0, "C4 (issue #77 AC4): a rolled-back partial post must exit non-zero")

        # leg 2: the release itself fails too -- the stranded comment must be listed.
        fake2 = FakeGh()
        fake2.fail_posts_for = {6}
        fake2.fail_release_for = {5}
        with _patched_gh(self, "C4", fake2):
            code2, out2, err2 = _run_cli(
                ["claim", "5", "6", "7", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        env2 = _parse_env(self, out2, code2, err2, "C4 (issue #77 AC4): the `claim` command")
        self.assertNotEqual(code2, 0, "C4 (issue #77 AC4): an unreleasable stranded comment must exit non-zero")
        self.assertIn(
            "issues/5#issuecomment-",
            json.dumps(env2),
            "C4 (issue #77 AC4): when a posted comment could NOT be released, the envelope must list it (by its comment URL)",
        )


class ReleaseTokenTests(_LiveCmdTest):
    """C5 (issue #77 AC5): `release --token` posts one Design-2 release on
    every named issue whose ACTIVE claim carries THAT token, and exits
    non-zero without posting when none matches."""

    def test_release_matches_token(self) -> None:
        token = "RT1-RELEASE-TOKEN-000001"
        other = "RT2-RELEASE-TOKEN-000002"
        fake = FakeGh()
        fake.add_claim(5, token)
        fake.add_claim(6, token)
        fake.add_claim(7, other)
        with _patched_gh(self, "C5", fake):
            code, out, err = _run_cli(
                ["release", "5", "6", "7", "--token", token, "--repo", REPO, "--format", "json"]
            )
        env = _parse_env(self, out, code, err, "C5 (issue #77 AC5): the `release` command")
        self.assertEqual(code, 0, f"C5 (issue #77 AC5): a matching release must exit 0; got {code} ({err.strip()[:160]!r})")
        release_posts = [(n, b) for n, b in fake.posts if "**Claim released" in b]
        self.assertEqual(
            sorted(n for n, _ in release_posts),
            [5, 6],
            f"C5 (issue #77 AC5): release must post exactly on the issues whose active claim "
            f"carries the token ({token}); posts: {fake.posts}",
        )
        for n, body in release_posts:
            self.assertTrue(
                body.startswith(f"**Claim released:** session={token}"),
                f"C5 (issue #77 AC5): the release body must be the Design-2 format for #{n}; got {body!r}",
            )

        # leg 2: no named issue carries an active claim with that token.
        fake2 = FakeGh()
        fake2.add_claim(7, other)
        with _patched_gh(self, "C5", fake2):
            code2, _out2, _err2 = _run_cli(
                ["release", "7", "--token", "NO-SUCH-TOKEN-1234567", "--repo", REPO, "--format", "json"]
            )
        self.assertNotEqual(code2, 0, "C5 (issue #77 AC5): a token matching NO active claim must exit non-zero")
        self.assertEqual(fake2.posts, [], "C5 (issue #77 AC5): a non-matching release must POST NOTHING")


class PrGuardTests(_LiveCmdTest):
    """C6 (issue #77 AC6): `pr-guard` lists each open PR closing a named
    issue and exits non-zero; exits 0 when there is none."""

    def test_pr_guard_finds_open_closer(self) -> None:
        fake = FakeGh()
        fake.open_prs = [
            {
                "number": 30,
                "state": "open",
                "title": "fix 5",
                "body": "closes #5",
                "user": {"login": "bob"},
                "closing_issues": [5],
                "closingIssueReferences": {"nodes": [{"number": 5}]},
            },
            {
                "number": 31,
                "state": "open",
                "title": "fix 9",
                "body": "closes #9",
                "user": {"login": "bob"},
                "closing_issues": [9],
                "closingIssueReferences": {"nodes": [{"number": 9}]},
            },
        ]
        with _patched_gh(self, "C6", fake):
            code, out, err = _run_cli(["pr-guard", "5", "8", "--repo", REPO, "--format", "json"])
        env = _parse_env(self, out, code, err, "C6 (issue #77 AC6): the `pr-guard` command")
        self.assertNotEqual(code, 0, "C6 (issue #77 AC6): an open closing PR for a named issue must exit non-zero")
        self.assertIn("30", json.dumps(env), "C6 (issue #77 AC6): the listing must name each open PR that closes a named issue (PR 30 closes #5)")

        fake2 = FakeGh()
        fake2.open_prs = list(fake.open_prs)  # PR 30 closes #5, PR 31 closes #9 -- neither closes #8
        with _patched_gh(self, "C6", fake2):
            code2, out2, err2 = _run_cli(["pr-guard", "8", "--repo", REPO, "--format", "json"])
        env2 = _parse_env(self, out2, code2, err2, "C6 (issue #77 AC6): the `pr-guard` command")
        self.assertEqual(code2, 0, "C6 (issue #77 AC6): no open closing PR for the named issue must exit 0")
        self.assertFalse(
            any(n in json.dumps(env2.get("data") or {}) for n in ("\"30\"", "\"31\"")),
            "C6 (issue #77 AC6): PRs that close OTHER issues must not be listed",
        )

        fake3 = FakeGh()  # no PRs at all
        with _patched_gh(self, "C6", fake3):
            code3, _out3, _err3 = _run_cli(["pr-guard", "5", "8", "--repo", REPO, "--format", "json"])
        self.assertEqual(code3, 0, "C6 (issue #77 AC6): an empty PR landscape must exit 0")


class ClaimsLiveTests(_LiveCmdTest):
    """C7 (issue #77 AC7): `claims --live` lists active and expired claims
    read from GitHub at call time, marked live:true / derived:false, ages
    from the wall clock, leaving the store byte-identical."""

    def test_claims_live_view(self) -> None:
        live_token = "LIVE-A-TOKEN-1234567890"
        dead_token = "LIVE-B-TOKEN-1234567890"
        fake = FakeGh()
        fake.add_repo_comment(5, claim_body(live_token), hours_ago=2.0)
        fake.add_repo_comment(6, claim_body(dead_token), hours_ago=100.0)
        before = self._db_sha()
        with _patched_gh(self, "C7", fake):
            code, out, err = _run_cli(["claims", "--live", "--repo", REPO, "--format", "json"])
        env = _parse_env(self, out, code, err, "C7 (issue #77 AC7): the `claims --live` command")
        self.assertEqual(code, 0, f"C7 (issue #77 AC7): claims --live must exit 0; got {code} ({err.strip()[:160]!r})")
        data = env.get("data") or {}
        self.assertIs(data.get("live"), True, f"C7 (issue #77 AC7): the live view must be marked live:true; data keys {sorted(data)}")
        self.assertIs(data.get("derived"), False, "C7 (issue #77 AC7): the live view is NOT derived -- derived must be false")
        claims = data.get("claims")
        self.assertIsInstance(claims, list, f"C7 (issue #77 AC7): data.claims must be a list; got {type(claims).__name__}")
        by_token = {c.get("token"): c for c in claims if isinstance(c, dict)}
        self.assertIn(live_token, by_token, f"C7 (issue #77 AC7): the live listing must include the fresh claim; claims {claims}")
        fresh = by_token[live_token]
        self.assertIs(fresh.get("active"), True, "C7 (issue #77 AC7): a 2h-old trusted claim is active in the live view")
        age = fresh.get("age_hours")
        self.assertIsInstance(age, (int, float), f"C7 (issue #77 AC7): live entries must carry age_hours from the WALL CLOCK; got {age!r}")
        self.assertTrue(1.0 <= float(age) <= 6.0, f"C7 (issue #77 AC7): a claim posted 2h ago must report ~2 age_hours (wall clock); got {age!r}")
        self.assertIn(dead_token, by_token, "C7 (issue #77 AC7): the live listing must also include the expired claim (active and expired)")
        self.assertIs(by_token[dead_token].get("active"), False, "C7 (issue #77 AC7): the 100h-old claim is past the 72h window and must be inactive")
        self.assertEqual(self._db_sha(), before, "C7 (issue #77 AC7): claims --live must leave the store byte-identical")


class UntrustedAuthorTests(_LiveCmdTest):
    """C8 (issue #77 AC8): a claim whose author association is not
    OWNER/MEMBER/COLLABORATOR is not an active claim in claim, release or
    claims --live, and is listed trusted:false."""

    def test_untrusted_author_cannot_hold_a_claim(self) -> None:
        untrusted_token = "UNTRUSTED-TOKEN-000001"
        fake = FakeGh()
        fake.add_repo_comment(5, claim_body(untrusted_token), author="rando", association="CONTRIBUTOR")

        # leg 1: the live listing shows it, but trusted:false and not active.
        with _patched_gh(self, "C8", fake):
            code, out, err = _run_cli(["claims", "--live", "--repo", REPO, "--format", "json"])
        env = _parse_env(self, out, code, err, "C8 (issue #77 AC8): the `claims --live` command")
        claims = (env.get("data") or {}).get("claims")
        self.assertIsInstance(claims, list, "C8 (issue #77 AC8): data.claims must be a list")
        entry = next((c for c in claims if isinstance(c, dict) and c.get("token") == untrusted_token), None)
        self.assertIsNotNone(entry, f"C8 (issue #77 AC8): the untrusted claim must still be LISTED; claims {claims}")
        self.assertIs(entry.get("trusted"), False, "C8 (issue #77 AC8): a CONTRIBUTOR claim must be listed trusted:false")
        self.assertIs(entry.get("active"), False, "C8 (issue #77 AC8): an untrusted claim is never an ACTIVE claim")

        # leg 2: claim succeeds over it (it does not hold the issue).
        fake2 = FakeGh()
        fake2.add_claim(5, untrusted_token, author="rando", association="NONE")
        with _patched_gh(self, "C8", fake2):
            code2, out2, err2 = _run_cli(["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"])
        env2 = _parse_env(self, out2, code2, err2, "C8 (issue #77 AC8): the `claim` command")
        self.assertEqual(code2, 0, f"C8 (issue #77 AC8): an untrusted foreign claim must NOT block a claim; got exit {code2}")
        self.assertEqual(_outcome(env2), "claimed", f"C8 (issue #77 AC8): the outcome over an untrusted claim must be `claimed`; got {_outcome(env2)!r}")

        # leg 3: release with that token finds no active claim -> posts nothing.
        fake3 = FakeGh()
        fake3.add_claim(5, untrusted_token, author="rando", association="NONE")
        with _patched_gh(self, "C8", fake3):
            code3, _out3, _err3 = _run_cli(["release", "5", "--token", untrusted_token, "--repo", REPO, "--format", "json"])
        self.assertNotEqual(code3, 0, "C8 (issue #77 AC8): releasing an untrusted token must exit non-zero (no active claim carries it)")
        self.assertEqual(fake3.posts, [], "C8 (issue #77 AC8): release over an untrusted claim must POST NOTHING")


class UntrustedTextWrapTests(_LiveCmdTest):
    """C9 (issue #77 AC9): every comment-derived string in a claims payload
    is wrapped {"untrusted_text": ...}; numbers, tokens, authors,
    associations and times ride as separate parsed fields."""

    def test_comment_text_is_wrapped(self) -> None:
        wrap_token = "WRAP-TOKEN-1234567890AB"
        first_line = f"**Agent claim:** session={wrap_token} working now."
        body = first_line + "\nsecond line of free prose"
        fake = FakeGh()
        fake.add_repo_comment(5, body)
        with _patched_gh(self, "C9", fake):
            code, out, err = _run_cli(["claims", "--live", "--repo", REPO, "--format", "json"])
        env = _parse_env(self, out, code, err, "C9 (issue #77 AC9): the `claims --live` command")
        claims = (env.get("data") or {}).get("claims")
        self.assertIsInstance(claims, list, "C9 (issue #77 AC9): data.claims must be a list")
        self.assertTrue(claims, "C9 (issue #77 AC9): the seeded claim must be listed")
        for entry in claims:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            self.assertIsInstance(
                text,
                dict,
                f"C9 (issue #77 AC9): every comment-derived string must be wrapped as "
                f"{{\"untrusted_text\": ...}}, never a bare string; got text={text!r}",
            )
            self.assertIn("untrusted_text", text, f"C9 (issue #77 AC9): the wrap key is `untrusted_text`; got {sorted(text)}")
            self.assertIsInstance(text.get("untrusted_text"), str, "C9 (issue #77 AC9): untrusted_text must be the string itself")
            self.assertIsInstance(entry.get("number"), int, "C9 (issue #77 AC9): the issue number must be a separate parsed int field")
            self.assertTrue(entry.get("token") is None or isinstance(entry.get("token"), str), "C9 (issue #77 AC9): the token must be a separate parsed field (str or null)")
            self.assertIsInstance(entry.get("author"), str, "C9 (issue #77 AC9): the author must be a separate parsed field")
            self.assertIsInstance(entry.get("author_association"), str, "C9 (issue #77 AC9): the association must be a separate parsed field")
            self.assertIsInstance(entry.get("created_at"), str, "C9 (issue #77 AC9): the time must be a separate parsed field")
            self.assertIsInstance(entry.get("named_issues"), list, "C9 (issue #77 AC9): named issues must be a separate parsed list field")
        wrapped = next((c for c in claims if isinstance(c, dict) and c.get("token") == wrap_token), None)
        self.assertIsNotNone(wrapped, f"C9 (issue #77 AC9): the multi-line claim must be listed; claims {claims}")
        self.assertEqual(
            (wrapped.get("text") or {}).get("untrusted_text"),
            first_line,
            "C9 (issue #77 AC9): the wrapped text is the MATCHED LINE only (matched_text), not the whole body",
        )


class McpWriteToolAnnotationTests(unittest.TestCase):
    """C10 (issue #77 AC10): tools/list advertises claim and release with
    readOnlyHint:false while pre-existing read tools keep true."""

    def test_mcp_write_tools_are_not_read_only(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        pin_env_for(self, root)
        session = McpSession(self, cwd=str(root), stale_after_s=10**9)
        self.addCleanup(session.stop)
        session.initialize()
        req_id = session.request("tools/list")
        response = session.wait_response(req_id)
        tools = (response.get("result") or {}).get("tools")
        self.assertIsInstance(tools, list, f"C10 (issue #77 AC10): tools/list must answer a tools array; got {response!r}")
        hints = {t.get("name"): (t.get("annotations") or {}).get("readOnlyHint") for t in tools if isinstance(t, dict)}
        self.assertIn("claim", hints, f"C10 (issue #77 AC10): the MCP tool list must expose a `claim` tool; tools are {sorted(hints)}")
        self.assertIn("release", hints, f"C10 (issue #77 AC10): the MCP tool list must expose a `release` tool; tools are {sorted(hints)}")
        self.assertIs(hints["claim"], False, "C10 (issue #77 AC10): `claim` writes to GitHub and must be advertised readOnlyHint:false")
        self.assertIs(hints["release"], False, "C10 (issue #77 AC10): `release` writes to GitHub and must be advertised readOnlyHint:false")
        self.assertIs(hints.get("search"), True, "C10 (issue #77 AC10): the pre-existing read tool `search` must keep readOnlyHint:true")
        self.assertIs(hints.get("triage"), True, "C10 (issue #77 AC10): the pre-existing read tool `triage` must keep readOnlyHint:true")
        session.stop()


class SkillClaimGateTests(unittest.TestCase):
    """C11 (issue #77 AC11): the bundled skill states the claim and release
    formats (incl. tokenless), makes claim before work and pr-guard before
    `gh pr create` required, documents not_applicable and the by-token
    takeover for a claim whose session has ended, and no longer says every
    work-selection command is read-only."""

    def test_skill_requires_claim_and_pr_guard(self) -> None:
        self.assertTrue(_SKILL_PATH.is_file(), f"C11 (issue #77 AC11): the bundled skill must exist at {_SKILL_PATH}")
        text = _SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn(
            "**Agent claim:** session=",
            text,
            "C11 (issue #77 AC11): the skill must state the Design-1 claim format `**Agent claim:** session=<token> issues=...`",
        )
        self.assertIn("issues=", text, "C11 (issue #77 AC11): the claim format line must include the `issues=...` segment")
        self.assertIn("**Claim released:", text, "C11 (issue #77 AC11): the skill must state the Design-2 release format `**Claim released:** session=<token>`")
        self.assertIn("tokenless", text.lower(), "C11 (issue #77 AC11): the skill must state the TOKENLESS claim and release forms (Design 9)")
        self.assertIsNotNone(
            re.search(r"(?is)\bclaim\b[^\n]{0,300}\brequired\b|\brequired\b[^\n]{0,300}\bclaim\b", text),
            "C11 (issue #77 AC11): the skill must make `claim` a REQUIRED step before work",
        )
        self.assertIn("pr-guard", text, "C11 (issue #77 AC11): the skill must name the `pr-guard` command")
        self.assertIsNotNone(
            re.search(r"(?is)pr-guard[^\n]{0,300}\brequired\b|\brequired\b[^\n]{0,300}pr-guard", text),
            "C11 (issue #77 AC11): the skill must make `pr-guard` required before `gh pr create`",
        )
        self.assertIn("gh pr create", text, "C11 (issue #77 AC11): the pr-guard requirement is relative to `gh pr create`")
        self.assertIn("not_applicable", text, "C11 (issue #77 AC11): the skill must say `claim` can report not_applicable where claims do not apply (Design 11)")
        self.assertIsNotNone(
            re.search(r"(?is)another session[^\n]{0,300}\btoken\b|\btoken\b[^\n]{0,300}another session", text),
            "C11 (issue #77 AC11): the skill must say a session may release ANOTHER session's claim by its token (Design 13)",
        )
        self.assertIn(
            "ended",
            text.lower(),
            "C11 (issue #77 AC11): the takeover rule is scoped to a claim whose session has ENDED",
        )
        self.assertNotIn(
            "every command is read-only",
            text,
            "C11 (issue #77 AC11): the sentence 'every command is read-only' must be GONE once a claim can be written",
        )


class ClaimNotApplicableTests(_LiveCmdTest):
    """C14 (issue #77 AC15): `claim` posts nothing and reports
    not_applicable (exit 0, reason echoed) when the config chain turns
    claims off or the session's login is not a trusted claim author under
    the settled login-trust mapping (contract point 8)."""

    def test_claim_not_applicable_posts_nothing(self) -> None:
        # Two sequential legs in ONE test (no subTest: pytest reports a test
        # whose failures all sit inside subTests as PASSED, which would break
        # the RED-at-base contract of this check file).
        # ---- leg (i): the config chain turns claims off --------------------
        config_file = self.root / ".zaxbygraph.toml"
        config_file.write_text("[claims]\nenabled = false\n", encoding="utf-8")
        fake = FakeGh()
        with _patched_gh(self, "C14", fake):
            code, out, err = _run_cli(
                ["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        config_file.unlink()
        env = _parse_env(self, out, code, err, "C14 (issue #77 AC15): the `claim` command")
        self.assertEqual(code, 0, f"C14 (issue #77 AC15): not_applicable is a quiet success and must exit 0; got {code}")
        self.assertEqual(_outcome(env), "not_applicable", f"C14 (issue #77 AC15): the outcome must be not_applicable when the config chain disables claims; got {_outcome(env)!r}")
        self.assertEqual(fake.posts, [], "C14 (issue #77 AC15): a not_applicable claim must POST NOTHING")
        dump = json.dumps(env)
        self.assertTrue(
            re.search(r'"[^"]*reason[^"]*"\s*:', dump) or "disabled" in dump.lower(),
            f"C14 (issue #77 AC15): the not_applicable envelope must echo the REASON (claims disabled by the config chain); got {dump[:300]}",
        )

        # ---- leg (ii): the session login fails all three trust checks ------
        # owner login 'acme' != session login 'me'; orgs members probe 404;
        # viewerPermission READ -> untrusted -> not_applicable, nothing posted.
        fake2 = FakeGh()
        fake2.viewer_permission = "READ"
        fake2.org_member_status = 404
        with _patched_gh(self, "C14", fake2):
            code2, out2, err2 = _run_cli(
                ["claim", "5", "--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]
            )
        env2 = _parse_env(self, out2, code2, err2, "C14 (issue #77 AC15): the `claim` command")
        self.assertEqual(code2, 0, f"C14 (issue #77 AC15): not_applicable for an untrusted login must exit 0; got {code2}")
        self.assertEqual(
            _outcome(env2),
            "not_applicable",
            f"C14 (issue #77 AC15): a login that is not owner, not an org member (404) and only viewerPermission READ must yield not_applicable; got {_outcome(env2)!r}",
        )
        self.assertEqual(fake2.posts, [], "C14 (issue #77 AC15): an untrusted login must POST NOTHING (every reader would ignore the claim)")
        dump2 = json.dumps(env2)
        self.assertTrue(
            re.search(r'"[^"]*reason[^"]*"\s*:', dump2) or any(kw in dump2.lower() for kw in ("trust", "login", "permission", "member")),
            f"C14 (issue #77 AC15): the not_applicable envelope must echo the REASON (untrusted login); got {dump2[:300]}",
        )


class OutcomeExitCodeTests(_LiveCmdTest):
    """C15 (issue #77 AC16): the five outcomes report exactly one of
    claimed/not_applicable/taken/lost_race/github_unreachable; the first
    two exit 0 and the last three exit DISTINCT codes, none in
    {0,1,2,3} (contract point 3)."""

    def test_outcomes_have_distinct_exit_codes(self) -> None:
        argv = ["--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]

        # claimed -> 0
        fake_ok = FakeGh()
        with _patched_gh(self, "C15", fake_ok):
            code_claimed, out, err = _run_cli(["claim", "5", *argv])
        env = _parse_env(self, out, code_claimed, err, "C15 (issue #77 AC16): the `claim` command")
        self.assertEqual(_outcome(env), "claimed", f"C15 (issue #77 AC16): a clean claim reports outcome `claimed`; got {_outcome(env)!r}")
        self.assertEqual(code_claimed, 0, "C15 (issue #77 AC16): `claimed` exits 0")

        # not_applicable -> 0 (untrusted login: READ + 404 + owner != login)
        fake_na = FakeGh()
        fake_na.viewer_permission = "READ"
        fake_na.org_member_status = 404
        with _patched_gh(self, "C15", fake_na):
            code_na, out_na, err_na = _run_cli(["claim", "5", *argv])
        env_na = _parse_env(self, out_na, code_na, err_na, "C15 (issue #77 AC16): the `claim` command")
        self.assertEqual(_outcome(env_na), "not_applicable", f"C15 (issue #77 AC16): an untrusted login reports `not_applicable`; got {_outcome(env_na)!r}")
        self.assertEqual(code_na, 0, "C15 (issue #77 AC16): `not_applicable` exits 0")
        self.assertEqual(fake_na.posts, [], "C15 (issue #77 AC16): a github-reachable not_applicable posts nothing")

        # taken -> its own new code
        fake_taken = FakeGh()
        fake_taken.add_claim(5, "OUTCOME-TAKEN-TOKEN-0001")
        with _patched_gh(self, "C15", fake_taken):
            code_taken, out_t, err_t = _run_cli(["claim", "5", *argv])
        env_t = _parse_env(self, out_t, code_taken, err_t, "C15 (issue #77 AC16): the `claim` command")
        self.assertEqual(_outcome(env_t), "taken", f"C15 (issue #77 AC16): a pre-held issue reports `taken`; got {_outcome(env_t)!r}")
        self.assertEqual(fake_taken.posts, [], "C15 (issue #77 AC16): a taken refusal posts nothing")

        # github_unreachable -> its own new code (every gh call dies with a
        # network error shape before anything is posted)
        fake_dead = FakeGh()
        fake_dead.fail_all = True
        with _patched_gh(self, "C15", fake_dead):
            code_dead, out_d, err_d = _run_cli(["claim", "5", *argv])
        env_d = _parse_env(self, out_d, code_dead, err_d, "C15 (issue #77 AC16): the `claim` command")
        self.assertEqual(_outcome(env_d), "github_unreachable", f"C15 (issue #77 AC16): a dead network reports `github_unreachable`; got {_outcome(env_d)!r}")
        self.assertEqual(fake_dead.posts, [], "C15 (issue #77 AC16): an unreachable GitHub means NOTHING is posted")

        # lost_race -> its own new code (the C3 shape: an earlier competitor
        # claim lands between our pre-read and our post-settle read)
        fake_lost = FakeGh()
        fake_lost.reveal_after_post = {6: [_foreign_claim(877_001, "OUTCOME-LOST-TOKEN-00001")]}
        with _patched_gh(self, "C15", fake_lost):
            code_lost, out_l, err_l = _run_cli(["claim", "5", "6", *argv])
        env_l = _parse_env(self, out_l, code_lost, err_l, "C15 (issue #77 AC16): the `claim` command")
        self.assertEqual(_outcome(env_l), "lost_race", f"C15 (issue #77 AC16): losing the read-after-post race reports `lost_race`; got {_outcome(env_l)!r}")

        codes = {"taken": code_taken, "github_unreachable": code_dead, "lost_race": code_lost}
        for name, code_value in codes.items():
            self.assertNotIn(
                code_value,
                (0, 1, 2, 3),
                f"C15 (issue #77 AC16): `{name}` must exit with its OWN NEW code outside {{0,1,2,3}}; got {code_value}",
            )
        self.assertEqual(
            len(set(codes.values())),
            3,
            f"C15 (issue #77 AC16): taken/github_unreachable/lost_race must be three DISTINCT exit codes; got {codes}",
        )


class TokenlessClaimReleaseTests(_LiveCmdTest):
    """C16 (issue #77 AC17): tokenless claims hold issues; `release
    --tokenless` ends exactly the tokenless claims; a token release and a
    tokenless release never cross (contract point 7)."""

    def test_tokenless_claim_and_release(self) -> None:
        # Four sequential legs in ONE test (no subTest: pytest reports a test
        # whose failures all sit inside subTests as PASSED, which would break
        # the RED-at-base contract of this check file).
        argv = ["--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]

        # (i) a tokenless claim is an active claim held by no token
        fake = FakeGh()
        fake.add_claim(5, None)
        with _patched_gh(self, "C16", fake):
            code, out, err = _run_cli(["claim", "5", *argv])
        env = _parse_env(self, out, code, err, "C16 (issue #77 AC17): the `claim` command")
        self.assertEqual(_outcome(env), "taken", f"C16 (issue #77 AC17): a trusted in-window TOKENLESS claim is an active claim (Design 9); got {_outcome(env)!r}")
        self.assertEqual(fake.posts, [], "C16 (issue #77 AC17): the tokenless holder blocks the claim")
        self.assertNotIn(code, (0, 1, 2, 3), f"C16 (issue #77 AC17): the refusal uses the taken exit code; got {code}")
        self.assertIsNotNone(
            re.search(r'"[^"]*token[^"]*"\s*:\s*null', json.dumps(env)),
            "C16 (issue #77 AC17): the refusal names the holder's token as NULL (the claim carries none)",
        )

        # (ii) release --tokenless ends it; the next claim succeeds
        fake2 = FakeGh()
        fake2.add_claim(5, None)
        with _patched_gh(self, "C16", fake2):
            code_rel, out_rel, err_rel = _run_cli(["release", "5", "--tokenless", "--repo", REPO, "--format", "json"])
            env_rel = _parse_env(self, out_rel, code_rel, err_rel, "C16 (issue #77 AC17): the `release` command")
            self.assertEqual(code_rel, 0, f"C16 (issue #77 AC17): release --tokenless over a tokenless claim exits 0; got {code_rel}")
            release_posts = [(n, b) for n, b in fake2.posts if "**Claim released" in b]
            self.assertEqual([n for n, _ in release_posts], [5], f"C16 (issue #77 AC17): release --tokenless posts ONE release on the tokenless-claimed issue; posts {fake2.posts}")
            for _n, body in release_posts:
                self.assertTrue(body.startswith("**Claim released:**"), f"C16 (issue #77 AC17): the --tokenless release body is the Design-2 format; got {body!r}")
                self.assertNotIn("session=", body, f"C16 (issue #77 AC17): the --tokenless release body carries NO token; got {body!r}")
            code_c, out_c, err_c = _run_cli(["claim", "5", *argv])
        env_c = _parse_env(self, out_c, code_c, err_c, "C16 (issue #77 AC17): the `claim` command")
        self.assertEqual(code_c, 0, "C16 (issue #77 AC17): after the tokenless release the issue is FREE -- the subsequent claim must succeed")
        self.assertEqual(_outcome(env_c), "claimed", f"C16 (issue #77 AC17): the post-release claim outcome is `claimed`; got {_outcome(env_c)!r}")

        # (iii) release --tokenless leaves a TOKENED claim active
        keep_token = "TOKENED-KEEP-TOKEN-00001"
        fake3 = FakeGh()
        fake3.add_claim(5, None, hours_ago=3.0)
        fake3.add_claim(5, keep_token, hours_ago=2.0)
        with _patched_gh(self, "C16", fake3):
            code_r3, out_r3, err_r3 = _run_cli(["release", "5", "--tokenless", "--repo", REPO, "--format", "json"])
            _parse_env(self, out_r3, code_r3, err_r3, "C16 (issue #77 AC17): the `release` command")
            release_posts3 = [(n, b) for n, b in fake3.posts if "**Claim released" in b]
            self.assertEqual([n for n, _ in release_posts3], [5], f"C16 (issue #77 AC17): the issue carries an active tokenless claim, so release --tokenless posts on it; posts {fake3.posts}")
            for _n, body in release_posts3:
                self.assertNotIn("session=", body, f"C16 (issue #77 AC17): the --tokenless release carries no token; got {body!r}")
            code_c3, out_c3, err_c3 = _run_cli(["claim", "5", *argv])
        env_c3 = _parse_env(self, out_c3, code_c3, err_c3, "C16 (issue #77 AC17): the `claim` command")
        self.assertEqual(_outcome(env_c3), "taken", f"C16 (issue #77 AC17): the TOKENED claim survives the tokenless release and still holds the issue; got {_outcome(env_c3)!r}")
        self.assertIn(keep_token, json.dumps(env_c3), "C16 (issue #77 AC17): the surviving holder is named by its session token")
        self.assertEqual(
            [n for n, b in fake3.posts if "**Agent claim" in b],
            [],
            "C16 (issue #77 AC17): the claim posts nothing against the surviving tokened claim",
        )

        # (iv) a TOKEN release does not end a TOKENLESS claim
        fake4 = FakeGh()
        fake4.add_claim(5, None, hours_ago=3.0)
        fake4.add_comment(5, release_body("OTHER-TOKEN-1234567890"), hours_ago=1.0)
        with _patched_gh(self, "C16", fake4):
            code_c4, out_c4, err_c4 = _run_cli(["claim", "5", *argv])
        env_c4 = _parse_env(self, out_c4, code_c4, err_c4, "C16 (issue #77 AC17): the `claim` command")
        self.assertEqual(_outcome(env_c4), "taken", f"C16 (issue #77 AC17): a release carrying a token NEVER ends a tokenless claim; got {_outcome(env_c4)!r}")
        self.assertEqual(fake4.posts, [], "C16 (issue #77 AC17): the still-held tokenless claim blocks posting")
        self.assertIsNotNone(
            re.search(r'"[^"]*token[^"]*"\s*:\s*null', json.dumps(env_c4)),
            "C16 (issue #77 AC17): the holder is still the tokenless claim (token null)",
        )

    def test_tokenless_release_finds_older_tokened_holder(self) -> None:
        """#90 AC3: `release --tokenless` must find the tokenless claim even
        when an OLDER tokened claim is also active on the named issue.

        Claims are scanned oldest-first and the earliest active claim is the
        holder, so a race loser's tokenless claim is normally the NEWER one.
        The release loop broke out of the scan at the first active entry, so a
        tokened holder hid the tokenless claim: the command answered `taken`
        ("no active claim matches this release") and posted nothing, breaking
        the documented release contract.

        RED at e6c8f41: outcome `taken`, zero posts.
        GREEN after the scan continues past a non-matching active entry.
        """
        argv = ["--repo", REPO, "--format", "json"]

        fake = FakeGh()
        # The tokened claim is ELDEST; the tokenless claim is the newer
        # race loser trying to withdraw.
        fake.add_claim(5, "OLDER-TOKENED-HOLDER-0001", hours_ago=3.0)
        fake.add_claim(5, None, hours_ago=1.0)

        with _patched_gh(self, "C3", fake):
            code, out, err = _run_cli(["release", "5", "--tokenless", *argv])
        env = _parse_env(self, out, code, err, "#90 AC3: the `release` command")
        self.assertEqual(
            code, 0,
            "#90 AC3: the tokenless release over an older tokened holder must"
            f" succeed; exit {code}, stderr {err.strip()[:200]!r}",
        )
        self.assertEqual(
            _outcome(env), "claimed",
            f"#90 AC3: the release outcome is `claimed`; got {_outcome(env)!r}",
        )
        releases = [(n, b) for n, b in fake.posts if "**Claim released" in b]
        self.assertEqual(
            [n for n, _ in releases], [5],
            "#90 AC3: exactly ONE release posts on the named issue;"
            f" posts {fake.posts}",
        )
        for _n, body in releases:
            self.assertNotIn(
                "session=", body,
                "#90 AC3: a --tokenless release carries no token;"
                f" got {body!r}",
            )


class PostOrderRetryTests(_LiveCmdTest):
    """C17 (issue #77 AC18): a multi-issue claim posts in ascending issue
    number, retries at most once after a lost race, posts the retry only
    after a fresh read shows no named issue taken, and exits lost_race on
    a second consecutive race."""

    def test_claims_post_in_issue_order_and_retry_once(self) -> None:
        argv = ["--settle-seconds", "0.05", "--repo", REPO, "--format", "json"]

        # leg 1: ascending post order regardless of argv order (30 20 10)
        fake = FakeGh()
        with _patched_gh(self, "C17", fake):
            code, out, err = _run_cli(["claim", "30", "20", "10", *argv])
        env = _parse_env(self, out, code, err, "C17 (issue #77 AC18): the `claim` command")
        self.assertEqual(code, 0, f"C17 (issue #77 AC18): the clean out-of-order claim succeeds; got {code}")
        claim_posts = [(n, b) for n, b in fake.posts if "**Agent claim" in b]
        self.assertEqual(
            [n for n, _ in claim_posts],
            [10, 20, 30],
            f"C17 (issue #77 AC18): the posts must go out in ASCENDING issue number even when named 30 20 10; saw {[n for n, _ in claim_posts]}",
        )

        # leg 2: still taken at the fresh read -> NO second posting wave
        fake2 = FakeGh()
        fake2.reveal_after_post = {20: [_foreign_claim(879_001, "ORDER-TAKEN-TOKEN-00001")]}
        with _patched_gh(self, "C17", fake2):
            code2, out2, err2 = _run_cli(["claim", "10", "20", "30", *argv])
        env2 = _parse_env(self, out2, code2, err2, "C17 (issue #77 AC18): the `claim` command")
        self.assertEqual(_outcome(env2), "lost_race", f"C17 (issue #77 AC18): the still-taken bundle exits lost_race; got {_outcome(env2)!r}")
        claim_posts2 = [(n, b) for n, b in fake2.posts if "**Agent claim" in b]
        self.assertEqual(
            len(claim_posts2),
            3,
            f"C17 (issue #77 AC18): when the fresh read still shows a named issue taken, the command must NOT post again -- exactly one posting wave; saw {[n for n, _ in claim_posts2]}",
        )
        release_posts2 = [(n, b) for n, b in fake2.posts if "**Claim released" in b]
        self.assertEqual(sorted(n for n, _ in release_posts2), [10, 20, 30], "C17 (issue #77 AC18): the lost bundle is fully released")

        # leg 3: the competitor vanishes before the retry read -> EXACTLY one
        # retry wave (after the fresh read clears), then success
        fake3 = FakeGh()
        fake3.reveal_timed = {20: [(1, 1.0, [_foreign_claim(879_002, "VANISH-TOKEN-00000001")])]}
        with _patched_gh(self, "C17", fake3):
            code3, out3, err3 = _run_cli(["claim", "10", "20", "30", *argv])
        env3 = _parse_env(self, out3, code3, err3, "C17 (issue #77 AC18): the `claim` command")
        self.assertEqual(code3, 0, f"C17 (issue #77 AC18): the delayed retry WINS when the fresh read shows every named issue free; got {code3}")
        self.assertEqual(_outcome(env3), "claimed", f"C17 (issue #77 AC18): the retry outcome is `claimed`; got {_outcome(env3)!r}")
        claim_posts3 = [(n, b) for n, b in fake3.posts if "**Agent claim" in b]
        self.assertEqual(
            [n for n, _ in claim_posts3],
            [10, 20, 30, 10, 20, 30],
            f"C17 (issue #77 AC18): exactly ONE retry wave in ascending order (never more); saw {[n for n, _ in claim_posts3]}",
        )
        release_posts3 = [(n, b) for n, b in fake3.posts if "**Claim released" in b]
        self.assertEqual(sorted(n for n, _ in release_posts3), [10, 20, 30], "C17 (issue #77 AC18): the first wave was released before the retry")

        # leg 4: a SECOND consecutive race after the retry -> lost_race, and
        # never a third posting wave
        fake4 = FakeGh()
        fake4.reveal_timed = {
            20: [
                (1, 1.0, [_foreign_claim(879_003, "RACE-A-TOKEN-0000001")]),
                (2, 30.0, [_foreign_claim(879_004, "RACE-B-TOKEN-0000002")]),
            ]
        }
        with _patched_gh(self, "C17", fake4):
            code4, out4, err4 = _run_cli(["claim", "10", "20", "30", *argv])
        env4 = _parse_env(self, out4, code4, err4, "C17 (issue #77 AC18): the `claim` command")
        self.assertEqual(_outcome(env4), "lost_race", f"C17 (issue #77 AC18): a second consecutive race exits lost_race; got {_outcome(env4)!r}")
        self.assertNotIn(code4, (0, 1, 2, 3), f"C17 (issue #77 AC18): the second-race lost_race keeps its own new exit code; got {code4}")
        claim_posts4 = [(n, b) for n, b in fake4.posts if "**Agent claim" in b]
        self.assertEqual(
            [n for n, _ in claim_posts4],
            [10, 20, 30, 10, 20, 30],
            f"C17 (issue #77 AC18): at most ONE retry wave even after a second race (no third posting); saw {[n for n, _ in claim_posts4]}",
        )


class ClaimConfigEchoTests(_LiveCmdTest):
    """C13 (issue #77 AC14): every claim command's payload echoes the claim
    config values in effect beside the outcome -- built-ins when no config
    exists, the config chain's per-key overrides when it does -- with
    config_source naming where the values came from (contract point 4)."""

    def _echo(self, env: dict, tag: str) -> dict:
        data = env.get("data") or {}
        cfg = data.get("claims_config")
        self.assertIsInstance(cfg, dict, f"{tag}: the payload must echo `claims_config` beside the outcome; data keys are {sorted(data)}")
        return cfg

    def _assert_values(
        self,
        cfg: dict,
        tag: str,
        *,
        patterns: list[str],
        release_prefix: str,
        token_prefix: str,
        window_hours: int,
        settle_seconds: float,
    ) -> None:
        self.assertEqual(
            sorted(str(p).lower() for p in cfg.get("patterns") or []),
            sorted(p.lower() for p in patterns),
            f"{tag}: the echoed patterns must be {patterns}; got {cfg.get('patterns')!r}",
        )
        self.assertEqual(cfg.get("release_prefix"), release_prefix, f"{tag}: the echoed release_prefix must be {release_prefix!r}; got {cfg.get('release_prefix')!r}")
        self.assertEqual(cfg.get("token_prefix"), token_prefix, f"{tag}: the echoed token_prefix must be {token_prefix!r}; got {cfg.get('token_prefix')!r}")
        self.assertEqual(cfg.get("window_hours"), window_hours, f"{tag}: the echoed window_hours must be {window_hours}; got {cfg.get('window_hours')!r}")
        self.assertEqual(float(cfg.get("settle_seconds")), settle_seconds, f"{tag}: the echoed settle_seconds must be {settle_seconds}; got {cfg.get('settle_seconds')!r}")
        self.assertIs(cfg.get("enabled"), True, f"{tag}: the echoed switch must be enabled:true; got {cfg.get('enabled')!r}")

    def test_claim_config_defaults_and_overrides(self) -> None:
        # Three sequential legs in ONE test (no subTest: pytest reports a
        # test whose failures all sit inside subTests as PASSED, which would
        # break the RED-at-base contract of this check file).

        # ---- leg (i): no config anywhere -> built-in defaults echoed ------
        # (no --settle-seconds flag: the value in effect is the built-in 2s)
        fake = FakeGh()
        with _patched_gh(self, "C13", fake):
            code, out, err = _run_cli(["claim", "5", "--repo", REPO, "--format", "json"])
        env = _parse_env(self, out, code, err, "C13 (issue #77 AC14): the `claim` command")
        self.assertEqual(code, 0, f"C13 (issue #77 AC14): the clean claim must exit 0; got {code}")
        self.assertEqual(_outcome(env), "claimed", f"C13 (issue #77 AC14): the defaults echo rides BESIDE the outcome `claimed`; got {_outcome(env)!r}")
        self._assert_values(
            self._echo(env, "C13 (issue #77 AC14) leg i (defaults)"),
            "C13 (issue #77 AC14) leg i (defaults)",
            patterns=["Agent claim", "Working this issue"],
            release_prefix="**Claim released",
            token_prefix="session=",
            window_hours=72,
            settle_seconds=2.0,
        )
        source_default = (env.get("data") or {}).get("config_source")
        self.assertTrue(source_default, "C13 (issue #77 AC14) leg i: the payload must name config_source (the built-in source)")

        # ---- leg (ii): a [claims] config overriding EVERY key -------------
        config_file = self.root / ".zaxbygraph.toml"
        config_file.write_text(
            "[claims]\n"
            'patterns = ["Staking this space"]\n'
            'release_prefix = "**Abandoned"\n'
            'token_prefix = "run="\n'
            "window_hours = 48\n"
            "settle_seconds = 1\n"
            "enabled = true\n",
            encoding="utf-8",
        )
        fake2 = FakeGh()
        fake2.add_comment(5, "Staking this space right now.")                # custom pattern, tokenless
        fake2.add_comment(6, "Working this issue now (built-in pattern).")   # built-in pattern only
        with _patched_gh(self, "C13", fake2):
            code_a, out_a, err_a = _run_cli(["claim", "5", "--repo", REPO, "--format", "json"])
            env_a = _parse_env(self, out_a, code_a, err_a, "C13 (issue #77 AC14) leg ii: the `claim` command")
            self.assertEqual(_outcome(env_a), "taken", f"C13 (issue #77 AC14) leg ii: a claim comment in the OVERRIDDEN pattern is seen as taken; got {_outcome(env_a)!r}")
            self.assertEqual(fake2.posts, [], "C13 (issue #77 AC14) leg ii: the overridden-pattern holder blocks posting")
            self._assert_values(
                self._echo(env_a, "C13 (issue #77 AC14) leg ii (overrides)"),
                "C13 (issue #77 AC14) leg ii (overrides)",
                patterns=["Staking this space"],
                release_prefix="**Abandoned",
                token_prefix="run=",
                window_hours=48,
                settle_seconds=1.0,
            )
            code_b, out_b, err_b = _run_cli(["claim", "6", "--repo", REPO, "--format", "json"])
        env_b = _parse_env(self, out_b, code_b, err_b, "C13 (issue #77 AC14) leg ii: the `claim` command")
        self.assertEqual(
            _outcome(env_b),
            "claimed",
            f"C13 (issue #77 AC14) leg ii: with patterns overridden, the BUILT-IN pattern no longer matches -- issue 6 is free; got {_outcome(env_b)!r}",
        )
        self.assertEqual(
            [n for n, b in fake2.posts if "**Agent claim" in b],
            [6],
            f"C13 (issue #77 AC14) leg ii: the claim posts only on the issue the override set free; posts {fake2.posts}",
        )

        # ---- leg (iii): release and claims --live echo the same values ----
        custom_token = "CFG-RELEASE-TOKEN-00001"
        fake3 = FakeGh()
        # a claim matching the OVERRIDDEN pattern, token in the OVERRIDDEN
        # token form run=<token>
        fake3.add_comment(5, f"Staking this space now. run={custom_token}")
        fake3.add_repo_comment(7, "Staking this space right now.")
        with _patched_gh(self, "C13", fake3):
            code_r, out_r, err_r = _run_cli(["release", "5", "--token", custom_token, "--repo", REPO, "--format", "json"])
            env_r = _parse_env(self, out_r, code_r, err_r, "C13 (issue #77 AC14) leg iii: the `release` command")
            self.assertEqual(code_r, 0, f"C13 (issue #77 AC14) leg iii: the release over the overridden-pattern claim must exit 0; got {code_r}")
            released = sorted(n for n, b in fake3.posts if "**Agent claim" not in b)
            self.assertEqual(released, [5], f"C13 (issue #77 AC14) leg iii: exactly one release post on issue 5; posts {fake3.posts}")
            self._assert_values(
                self._echo(env_r, "C13 (issue #77 AC14) leg iii (release echo)"),
                "C13 (issue #77 AC14) leg iii (release echo)",
                patterns=["Staking this space"],
                release_prefix="**Abandoned",
                token_prefix="run=",
                window_hours=48,
                settle_seconds=1.0,
            )
            source_release = (env_r.get("data") or {}).get("config_source")
            self.assertTrue(source_release, "C13 (issue #77 AC14) leg iii: the release payload must name config_source")
            code_l, out_l, err_l = _run_cli(["claims", "--live", "--repo", REPO, "--format", "json"])
        env_l = _parse_env(self, out_l, code_l, err_l, "C13 (issue #77 AC14) leg iii: the `claims --live` command")
        data_l = env_l.get("data") or {}
        self._assert_values(
            self._echo(env_l, "C13 (issue #77 AC14) leg iii (live echo)"),
            "C13 (issue #77 AC14) leg iii (live echo)",
            patterns=["Staking this space"],
            release_prefix="**Abandoned",
            token_prefix="run=",
            window_hours=48,
            settle_seconds=1.0,
        )
        source_override = data_l.get("config_source")
        self.assertTrue(source_override, "C13 (issue #77 AC14) leg iii: the claims payload must name config_source")
        self.assertNotEqual(
            str(source_override),
            str(source_default),
            f"C13 (issue #77 AC14) leg iii: config_source must distinguish the config chain ({source_override!r}) from the built-ins ({source_default!r})",
        )
        claims_l = data_l.get("claims") or []
        self.assertTrue(
            any(isinstance(c, dict) and c.get("number") == 7 for c in claims_l),
            f"C13 (issue #77 AC14) leg iii: the live listing PARSES the overridden pattern (issue 7's custom-pattern claim must be listed); claims {claims_l}",
        )
        config_file.unlink()


if __name__ == "__main__":
    unittest.main()
