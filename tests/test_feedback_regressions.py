"""Committed regression pins for the PR #78 feedback round (closeout round 3).

Stage B's falsification probes lived in temp scripts; the closeout critic
(X-5) required the load-bearing legs as committed tests. One test per root
family, hermetic (FakeGitHubSource / temp stores, no network):

- ROOT-001: run_sql's byte budget counts UTF-8 bytes (not chars), hex rides
  the cell cap, and the over-budget row is EXCLUDED before append.
- ROOT-002: --offset at/over the _MAX_FETCH_LIMIT ceiling answers envelopes.
- ROOT-004: the pre-v12 gate refuses item/search/brief (direct + MCP).
- ROOT-005: NULL-author comments survive --no-bots (NOT EXISTS rewrite).
- ROOT-006: MCP search cursor pages stamp their own offset/matched_mode.
- ROOT-008/009: vocabulary bail + _sql_error deadline discipline.
- ROOT-010/012: _int_cursor and triage total rejection.
- ROOT-011: cursors at/over the _MAX_CURSOR ceiling are terminal.
- ROOT-013/X-1: brief --no-bots counts one bot PR ONCE (the double count the
  closeout critic reproduced against bdc859b) and never fans it to gh.
- ROOT-015: related budget-cut nodes leave no dangling edges.
- ROOT-024: _fetch_failure sideband anchoring, stderr scrub caps, LC_ALL pin.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fixtures import REPO, FakeGitHubSource, issue, pull
from test_mcp import run_cli
from zaxbygraph import derive
from zaxbygraph import query
from zaxbygraph import snapshot
import zaxbygraph.mcp_server as mcp_server
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo


class _Ctx:
    """Minimal MCP server context for direct handler calls."""

    default_repo = REPO
    default_db = None
    root = None
    modern = False
    roots_declared = False
    stale_after_s = 10 ** 9
    sync_runner = None


def _seed(path: Path, seeder) -> Path:
    conn = connect(path)
    init_schema(conn)
    src = FakeGitHubSource()
    seeder(src)
    sync_repo(conn, src, REPO)
    conn.close()
    return path


class FeedbackRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="fbreg_"))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------- ROOT-001
    def test_sql_byte_budget_is_utf8_and_excludes_over_budget_row(self) -> None:
        conn = connect(self.root / "budget.db")
        init_schema(conn)
        try:
            cap, marker = query._SQL_CELL_CAP, query._SQL_CELL_MARKER

            # A huge blob under the budget rides out CAPPED, not truncated.
            res = query.run_sql(conn, "SELECT zeroblob(2000000) AS z", limit=10, max_bytes=1048576)
            cell = res["rows"][0][0]
            self.assertIsInstance(cell, str)
            self.assertEqual(len(cell), cap + len(marker), "a capped cell is exactly cap+marker long")
            self.assertTrue(cell.endswith(marker), "the cap marker is appended")
            self.assertFalse(res["truncated"], "one capped cell under the budget is not a truncation")

            # Hex rides the same cap: a 2x-cap binary blob renders capped.
            capped = query._render_cell(b"\x00" * (2 * cap + 5))
            self.assertEqual(len(capped), cap + len(marker))
            self.assertTrue(capped.endswith(marker), "hex must ride the cell cap like str cells (ROOT-001)")

            # The budget counts UTF-8 BYTES, not chars: a 1000-char accented
            # string is 2000 bytes and busts a 1500-byte budget.
            accent = "é" * 1000
            res = query.run_sql(conn, f"SELECT '{accent}' AS t", limit=10, max_bytes=1500)
            self.assertTrue(res["truncated"], "2000 UTF-8 bytes vs a 1500 budget must truncate")
            self.assertEqual(res["rows"], [], "the over-budget row is excluded, not shipped")

            # ASCII control: 1000 bytes under a 1500 budget ships.
            ctrl = query.run_sql(conn, "SELECT '" + "e" * 1000 + "' AS t", limit=10, max_bytes=1500)
            self.assertEqual(len(ctrl["rows"]), 1)
            self.assertFalse(ctrl["truncated"])

            # The over-budget row is EXCLUDED (the old code appended it to
            # rows before flagging truncated).
            wide = "a" * 600
            res = query.run_sql(
                conn,
                f"SELECT '{wide}' AS t FROM (SELECT 1 UNION SELECT 2 UNION SELECT 3)",
                limit=10,
                max_bytes=1500,
            )
            self.assertTrue(res["truncated"])
            self.assertEqual(len(res["rows"]), 2, "2x600B ship, the 3rd (1800B total) is excluded")
        finally:
            conn.close()

    # ------------------------------------------------------------- ROOT-002
    def test_search_offset_ceiling_answers_envelopes(self) -> None:
        db_path = _seed(
            self.root / "offset.db",
            lambda s: [s.add_issue(issue(n, title=f"off {n}", state="open")) for n in (1, 2, 3)],
        )
        base = ["--format", "json", "--repo", REPO, "--db", str(db_path)]
        code, out, err = run_cli(["search", "off", "--offset", "9223372036854775808", *base])
        self.assertEqual(code, 2, f"offset 2^63 must be a bad_request envelope, not a crash: {err}")
        self.assertEqual((json.loads(out).get("error") or {}).get("code"), "bad_request")
        self.assertNotIn("Traceback", err)

        code, out, err = run_cli(["search", "off", "--offset", str(query._MAX_FETCH_LIMIT + 1), *base])
        self.assertEqual(code, 2)
        self.assertEqual((json.loads(out).get("error") or {}).get("code"), "bad_request",
                         "the ceiling must be shared with --limit: one constant, one envelope path")

        code, out, err = run_cli(["search", "off", "--offset", str(query._MAX_FETCH_LIMIT), *base])
        self.assertEqual(code, 0, f"an offset AT the ceiling is accepted (overshoot stays honest): {err}")
        self.assertIs(json.loads(out).get("ok"), True)

    # ------------------------------------------------------------- ROOT-004
    def test_pre_v12_store_refuses_bot_flags_on_all_surfaces(self) -> None:
        db_path = _seed(
            self.root / "v11.db",
            lambda s: [s.add_issue(issue(n, title=f"v11 item {n}", state="open")) for n in (1, 2)],
        )
        conn = connect(db_path)
        conn.execute("PRAGMA user_version = 11")
        conn.commit()
        try:
            for fn, call in (
                ("item", lambda: query.item(conn, 1, repo=REPO, exclude_bots=True)),
                ("search", lambda: query.search(conn, "v11", repo=REPO, exclude_bots=True)),
                ("brief", lambda: derive.brief(conn, REPO, 1, exclude_bots=True)),
            ):
                with self.subTest(fn=fn):
                    with self.assertRaises(ValueError) as cm:
                        call()
                    msg = str(cm.exception)
                    self.assertIn("v12", msg, "the gate names the schema version")
                    self.assertIn("sync", msg, "the gate names the remedy")
        finally:
            conn.close()

        # The MCP surface translates the gate to a structured bad_request.
        ctx = _Ctx()
        ctx.default_db = db_path
        ctx.root = str(self.root)
        ctx.sync_runner = mcp_server._default_sync_runner
        for tool, args in (
            ("search", {"repo": REPO, "query": "v11", "exclude_bots": True}),
            ("get_item", {"repo": REPO, "number": 1, "exclude_bots": True}),
        ):
            with self.subTest(tool=tool):
                with self.assertRaises(mcp_server.ToolError) as cm:
                    mcp_server._TOOL_HANDLERS[tool](args, ctx)
                exc = cm.exception
                self.assertEqual(exc.code_key, "bad_request")
                self.assertIn("sync", exc.hint or "", "the hint names the remedy")

    # ------------------------------------------------------------- ROOT-005
    def test_null_author_survives_no_bots(self) -> None:
        conn = connect(self.root / "nullauthor.db")
        init_schema(conn)
        src = FakeGitHubSource()
        src.add_issue(issue(1, title="parent target", body="parent body text", state="open", comments=1))
        src.comment_on(1, "human comment ghostrare needle", author="alice")
        sync_repo(conn, src, REPO)
        # Ghost (NULL-author) + bot comments, direct inserts.
        for gid, author, body in (
            (9101, None, "ghost author comment ghostrare needle"),
            (9102, "dependabot[bot]", "bot comment ghostrare needle"),
        ):
            conn.execute(
                "INSERT INTO comments(github_id, repo, number, kind, author, author_association,"
                " created_at, updated_at, body, html_url, raw_json)"
                " VALUES (?, ?, 1, 'issue_comment', ?, 'NONE', '2026-01-02T00:00:00Z',"
                " '2026-01-02T00:00:00Z', ?, 'u:', '{}')",
                (gid, REPO, author, body),
            )
        conn.execute("INSERT OR IGNORE INTO actors(login, is_bot) VALUES ('dependabot[bot]', 1)")
        conn.commit()
        try:
            # item: the ghost survives, the bot drops, bots_omitted counts 1.
            rec = query.item(conn, 1, repo=REPO, exclude_bots=True)
            authors = [c.get("author") for c in rec["comments"]]
            self.assertIn(None, authors, "a NULL-author comment is not an established bot and survives")
            self.assertNotIn("dependabot[bot]", authors)
            self.assertEqual(rec.get("bots_omitted"), 1)

            # Control: without the filter everything ships.
            rec_all = query.item(conn, 1, repo=REPO, exclude_bots=False)
            self.assertIn("dependabot[bot]",
                          [c.get("author") for c in rec_all["comments"]])

            # search: the item is still found THROUGH the ghost comment.
            sr = query.search(conn, "ghostrare needle", repo=REPO, exclude_bots=True)
            items = sr.get("items") or []
            self.assertEqual(len(items), 1, f"fixture sanity: one item matches; got {[(i.get('number')) for i in items]}")
            self.assertEqual(int(items[0]["number"]), 1)
            self.assertGreaterEqual(items[0].get("matching_comments", 0), 1)
            self.assertIn("ghost", items[0].get("comment_snippet", ""),
                          "the surviving comment's snippet is present")

            # brief: the ghost ranks, the bot is out.
            pb = derive.brief(conn, REPO, 1, exclude_bots=True)
            bauthors = [c.get("author") for c in pb.get("comments") or []]
            self.assertIn(None, bauthors)
            self.assertNotIn("dependabot[bot]", bauthors)
        finally:
            conn.close()

    # ------------------------------------------------------------- ROOT-006
    def test_mcp_search_cursor_pages_stamp_their_own_offset(self) -> None:
        db_path = _seed(
            self.root / "page.db",
            lambda s: [s.add_issue(issue(n, title=f"pager needle {n}", state="open")) for n in range(1, 13)],
        )
        ctx = _Ctx()
        ctx.default_db = db_path
        ctx.root = str(self.root)
        ctx.sync_runner = mcp_server._default_sync_runner
        r1 = mcp_server._tool_search({"repo": REPO, "query": "needle", "limit": 5}, ctx)
        d1 = r1.get("data") or r1
        cursor = d1.get("next_cursor", "5")
        r2 = mcp_server._tool_search({"repo": REPO, "query": "needle", "limit": 5, "cursor": cursor}, ctx)
        d2 = r2.get("data") or r2
        nums1 = [i["number"] for i in d1.get("items") or []]
        nums2 = [i["number"] for i in d2.get("items") or []]
        self.assertEqual(d1.get("offset"), 0, "page 1 stamps offset 0")
        self.assertEqual(d2.get("offset"), 5, "page 2 stamps ITS OWN offset (the old code stamped 0)")
        self.assertTrue(set(nums1).isdisjoint(nums2), "cursor pages are disjoint")
        self.assertEqual(d1.get("matched_mode"), d2.get("matched_mode"),
                         "matched_mode is page-computed, not a page-1 constant")

    # ------------------------------------------------------------- ROOT-008/009
    def test_sql_deadline_discipline(self) -> None:
        c = connect(self.root / "vocab.db")
        init_schema(c)
        try:
            # An expired deadline bails BEFORE the vocabulary fan-out.
            with self.assertRaises(query.SqlTimeoutError):
                query._vocabulary_warnings(c, "SELECT 1 FROM items WHERE body = 'x'",
                                           time.monotonic() - 1, 5.0)
            # A live deadline returns a list on trivial SQL.
            w = query._vocabulary_warnings(c, "SELECT 1 FROM items WHERE body = 'x'",
                                           time.monotonic() + 60, 5.0)
            self.assertIsInstance(w, list)

            future = time.monotonic() + 60
            past = time.monotonic() - 1
            e_int = sqlite3.OperationalError("interrupted")
            e_ident = sqlite3.OperationalError("no such column: interrupted_at")
            self.assertIsInstance(query._sql_error(e_int, future, 5.0), ValueError,
                                  "'interrupted' + live deadline = a caller fault, not a timeout")
            self.assertIsInstance(query._sql_error(e_int, past, 5.0), query.SqlTimeoutError,
                                  "'interrupted' + expired deadline = the deadline fired")
            r = query._sql_error(e_ident, future, 5.0)
            self.assertIsInstance(r, ValueError)
            self.assertIn("no such column", str(r), "the identifier text keeps its diagnostic")
        finally:
            c.close()

    # ------------------------------------------------------------- ROOT-010/012
    def test_int_cursor_and_triage_totals(self) -> None:
        for raw, want, label in (
            ("-5", "ToolError", "negative -> bad_request"),
            ("0", 0, "zero -> page 1"),
            ("99999999999", mcp_server._MAX_CURSOR, "huge -> clamped to the ceiling"),
        ):
            with self.subTest(raw=raw):
                try:
                    got = mcp_server._int_cursor(raw, "search")
                    self.assertEqual(got, want, label)
                except mcp_server.ToolError as exc:
                    self.assertEqual(want, "ToolError")
                    self.assertEqual(exc.code_key, "bad_request")

        for cur in ("nan:5", "inf:5", "-inf:5"):
            with self.subTest(cur=cur):
                with self.assertRaises(mcp_server.ToolError) as cm:
                    mcp_server._tool_triage({"cursor": cur}, None)
                self.assertEqual(cm.exception.code_key, "bad_request")

    # ------------------------------------------------------------- ROOT-011
    def test_cursor_ceiling_is_terminal(self) -> None:
        db_path = self.root / "mass.db"
        conn = connect(db_path)
        init_schema(conn)
        conn.executemany(
            "INSERT INTO items (id, repo, number, kind, title, body, state, raw_json)"
            " VALUES (?, ?, ?, 'issue', 'massneedle pad', 'filler text', 'open', '{}')",
            [(10000 + n, REPO, n) for n in range(1, 10051)],
        )
        conn.execute(
            "INSERT INTO sync_state (repo, last_full_sync_at, item_count) VALUES (?, '2026-01-01T00:00:00Z', 10050)",
            (REPO,),
        )
        conn.commit()
        conn.close()

        ctx = _Ctx()
        ctx.default_db = db_path
        ctx.root = str(self.root)
        ctx.sync_runner = mcp_server._default_sync_runner

        def page(cursor: str) -> tuple[dict, dict]:
            r = mcp_server._tool_search({"repo": REPO, "query": "massneedle", "limit": 50, "cursor": cursor}, ctx)
            return r.get("data") or r, r

        d_mid, _ = page("9900")
        self.assertEqual(d_mid.get("next_cursor"), "9950", "a cursor under the ceiling still mints")
        d_past, r_past = page("9990")
        self.assertEqual(len(d_past.get("items") or []), 50)
        self.assertIs(r_past.get("truncated"), True, "9990+50 > 10000: an overshoot page is terminal")
        self.assertNotIn("next_cursor", d_past, "no next_cursor past the ceiling: str(offset) would self-loop")
        d_at, r_at = page("10000")
        self.assertEqual(len(d_at.get("items") or []), 50)
        self.assertIs(r_at.get("truncated"), True)
        self.assertNotIn("next_cursor", d_at)

    # ------------------------------------------------------------- ROOT-013 + X-1
    def test_brief_excludes_bot_pr_once_and_never_fans_it(self) -> None:
        def seed_fn(src: FakeGitHubSource) -> None:
            src.add_issue(issue(1, title="live hub", body="the hub", state="open"))
            src.add_pr(issue(10, title="human pr", state="open", kind="pr"), pull(10))
            src.add_pr(issue(11, title="bot pr", state="open", kind="pr", author="dependabot[bot]",
                             body="Fixes #1"), pull(11))

        db_path = _seed(self.root / "live.db", seed_fn)
        conn = connect(db_path)
        conn.execute("INSERT OR IGNORE INTO actors(login, is_bot) VALUES ('dependabot[bot]', 1)")
        # A timeline cross-reference makes PR 11 BOTH a linked_pr edge (1->11)
        # and a closes_keyword edge (11->1, derived from the body keyword).
        conn.execute(
            "INSERT INTO edges (repo, src_type, src_id, rel, dst_type, dst_id, confidence, evidence, source)"
            " VALUES (?, 'item', '1', 'linked_pr', 'item', '11', 'EXTRACTED', 'regression', 'timeline')",
            (REPO,),
        )
        conn.commit()
        kw = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE rel = 'closes_keyword' AND src_id = '11' AND dst_id = '1'"
        ).fetchone()[0]
        self.assertEqual(kw, 1, "fixture sanity: the PR body 'Fixes #1' must derive a closes_keyword edge")
        bots = conn.execute("SELECT is_bot FROM actors WHERE login = 'dependabot[bot]'").fetchone()
        self.assertEqual(bots[0], 1, "fixture sanity: the bot flag must be set")

        packet = derive.brief(conn, REPO, 1, exclude_bots=True)
        self.assertEqual(packet.get("bots_omitted"), 1,
                         "ONE bot PR referenced by TWO edges is counted ONCE (X-1: the double count reproduced at bdc859b)")
        nums = [e.get("number") for e in packet.get("linked_prs") or []]
        self.assertNotIn(11, nums, "the bot PR must not appear in the linked PR list")
        conn.close()

        # The --live fan-out must never request the omitted bot PR.
        conn = connect(db_path)
        requested = []

        def fake_run(cmd, *a, **k):
            path = cmd[2] if len(cmd) > 2 else ""
            requested.append(path)
            payload = {}
            if "/pulls/" in path and path.endswith("/reviews"):
                payload = []
            elif "/pulls/" in path:
                payload = {"head": {"sha": "sha0"}, "title": "t"}
            elif path.endswith("/status"):
                payload = {"state": "success", "statuses": []}
            return mock.Mock(returncode=0, stdout=json.dumps(payload).encode())

        with mock.patch("subprocess.run", side_effect=fake_run):
            pk = derive.brief(conn, REPO, 1, live=True, exclude_bots=True)
        live = pk.get("live") or {}
        live_prs = [e.get("number") for e in live.get("prs", [])]
        pull_calls = [p for p in requested if "/pulls/" in p and not p.endswith("/reviews")]
        self.assertNotIn(11, live_prs, "the omitted bot PR stays out of the live block")
        self.assertFalse(any(p.endswith("/pulls/11") for p in pull_calls),
                         "the gh fan-out must never query an omitted bot PR")
        self.assertEqual(pk.get("bots_omitted"), 1, "the count stays 1 under --live too")
        conn.close()

    # ------------------------------------------------------------- ROOT-015
    def test_related_budget_cut_leaves_no_dangling_edges(self) -> None:
        db_path = _seed(
            self.root / "rel.db",
            lambda s: [
                s.add_issue(issue(1, title="hub", body="star " + " ".join(f"#{n}" for n in range(2, 30)), state="open")),
                *(s.add_issue(issue(n, title=f"sat {n}", state="open")) for n in range(2, 30)),
            ],
        )
        conn = connect(db_path)
        try:
            rel = query.related(conn, 1, repo=REPO, max_bytes=2000)
            emitted = {str(n["number"]) for n in rel["nodes"]}
            dangling = []
            for e in rel["edges"]:
                for kind, ident in ((e["src_type"], e["src_id"]), (e["dst_type"], e["dst_id"])):
                    if kind == "item" and isinstance(ident, str) and ident.isdigit() and ident not in emitted:
                        dangling.append((e["rel"], ident))
            self.assertTrue(rel["truncated"], "fixture sanity: a 29-node neighborhood over a 2000B budget truncates")
            self.assertFalse(dangling, "budget-cut local item endpoints must drop their edges, not dangle")
            self.assertLess(len(emitted), 29, "fixture sanity: the budget actually cut nodes")
        finally:
            conn.close()


    # ------------------------------------------------- cubic round 2: MCP sql
    def test_mcp_sql_budget_wired(self) -> None:
        # cubic round 2 (ext:506 analog): the MCP sql adapter must honor
        # ZAXBYGRAPH_SQL_MAX_BYTES too -- an MCP path that ignores the env
        # knob could ship unnoticed while the CLI leg alone stays green.
        db_path = _seed(
            self.root / "mcpsql.db",
            lambda s: [s.add_issue(issue(n, title=f"wide {n}", body="y" * 900, state="open")) for n in range(1, 6)],
        )
        ctx = _Ctx()
        ctx.default_db = db_path
        ctx.root = str(self.root)
        ctx.sync_runner = mcp_server._default_sync_runner
        with mock.patch.dict(os.environ, {"ZAXBYGRAPH_SQL_MAX_BYTES": "2000"}):
            env = mcp_server._TOOL_HANDLERS["sql"](
                {"repo": REPO, "statement": "SELECT number, body FROM items ORDER BY number"}, ctx
            )
        self.assertIs(env.get("ok"), True, f"the MCP sql tool must succeed: {env.get('error')!r}")
        data = env.get("data") or {}
        self.assertIs(env.get("truncated"), True, "a 2000-byte budget over 5x900B rows must truncate")
        self.assertEqual(data.get("total_rows"), 5, "the honest unfiltered total rides the MCP payload")
        self.assertLess(len(data.get("rows") or []), 5, "truncation excludes over-budget rows")

    # ------------------------------------------------------------- ROOT-024
    def test_fetch_failure_sideband_and_stderr_scrub(self) -> None:
        # A remote-controlled sideband line must not classify as absent-ref.
        sideband = b"remote: couldn't find remote ref refs/heads/x\n"
        r = snapshot._fetch_failure(sideband, "refs/heads/x", "rest")
        self.assertIsNotNone(r)
        self.assertEqual(r.code, "source_unreachable",
                         "remote: sideband text is remote-influenced, never absent")

        # An anchored fatal: line still classifies absent.
        self.assertIsNone(snapshot._fetch_failure(b"fatal: couldn't find remote ref refs/heads/x\n",
                                                  "refs/heads/x", "rest"))

        # A non-English fatal stays source_unreachable.
        r = snapshot._fetch_failure("fatal: Konnte Remote-Referenz nicht finden".encode(),
                                    "refs/heads/x", "rest")
        self.assertIsNotNone(r)

        # Actionable stderr: complete ANSI/OSC sequences stripped FIRST
        # (cubic round 2: isprintable alone left '[31m'/'0;title' fragments),
        # then printable-only, 200-char cap.
        ansi_line = b"fatal: \x1b[31mboom\x1b[0m\x1b]0;title\x07 " + b"A" * 600 + b"\n"
        msg = snapshot._actionable_stderr(ansi_line)
        self.assertNotIn("31m", msg, "the CSI fragment must be stripped")
        self.assertNotIn("0;title", msg, "the OSC fragment must be stripped")
        self.assertTrue(all(ch.isprintable() for ch in msg), f"no control chars: {msg!r}")
        self.assertLessEqual(len(msg), 204, "actionable lines are capped")

        # The fallback tail gets the same treatment.
        tail_only = b"warning: misc\nwarning: more misc " + b"B" * 600 + b"\n"
        msg2 = snapshot._actionable_stderr(tail_only)
        self.assertTrue(all(ch.isprintable() for ch in msg2))
        self.assertLessEqual(len(msg2), 200)

        # _run_git pins the locale so localized diagnostics stay classifiable.
        captured = {}

        def fake_run(cmd, *a, **k):
            captured.update(k.get("env") or {})
            return mock.Mock(returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(snapshot.subprocess, "run", side_effect=fake_run):
            snapshot._run_git(["--version"], cwd=str(self.root))
        self.assertEqual(captured.get("LC_ALL"), "C")
        self.assertEqual(captured.get("LANG"), "C")


if __name__ == "__main__":
    unittest.main()
