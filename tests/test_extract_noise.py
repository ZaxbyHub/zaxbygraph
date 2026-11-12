"""Frozen acceptance checks: extraction noise and new surfaces (closing-keyword
scopes, code-context references, comment-id/foreign tokens, actors.is_bot,
branch-lookup cost, migration v12 keyword re-derivation).

Authored at arm's length from the acceptance criteria and verified root
causes only. DISCRIMINATING checks fail at base for the SEMANTIC reason
named in each docstring; NEW-SURFACE checks (AC19, AC21) fail at base
because the surface does not exist. Offline: no network, no live `gh`.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fixtures import REPO, FakeGitHubSource, TempDBTest, comment, issue, review, ts
from zaxbygraph.db import connect, init_schema
from zaxbygraph.extract import (  # noqa: E501 -- long frozen payloads below
    edges_from_comment, edges_from_commit_messages, edges_from_item,
    edges_from_review, iter_closing_locations, iter_mention_locations,
)
from zaxbygraph.github import GitHubError
from zaxbygraph.graphql import GraphQLSource
from zaxbygraph.sync import sync_repo

#: The configured bot-list surface this check freezes (AC19): a login named
#: there is a bot even without a [bot] suffix or payload discriminator.
_BOT_LIST_ENV = "ZAXBYGRAPH_BOT_LOGINS"

def _item_refs(edges) -> set[tuple[str, str]]:
    """(rel, dst_id) for the item->item edges an emitter produced."""
    return {(e[2], e[4]) for e in edges if e[0] == "item"}

class ClosingKeywordScopeTests(unittest.TestCase):
    def test_closing_keywords_only_from_pr_body_and_commits(self) -> None:
        """RED at base, GREEN after the fix: a closing keyword in an issue
        body, a comment, or a review records a MENTION and never
        closes_keyword; closing keywords in a PR description or a commit
        message keep producing closes_keyword. At base all three non-PR
        surfaces yield closes_keyword ('fixes #5' in a comment closes
        #5)."""
        target = "5"
        cases = (
            ("an issue body", _item_refs(edges_from_item(REPO, issue(9, body="fixes #5")))),
            ("a comment", _item_refs(edges_from_comment(REPO, 9, comment(50_001, "fixes #5")))),
            ("a review", _item_refs(edges_from_review(REPO, 9, review(60_001, "APPROVED", body="fixes #5")))))
        for label, edges in cases:
            self.assertNotIn(("closes_keyword", target), edges, f"{label} must not produce closes_keyword: {edges}")
            self.assertIn(("mentions", target), edges, f"{label} closing keyword must record a mention: {edges}")
        pr_edges = _item_refs(edges_from_item(REPO, issue(9, body="fixes #5", kind="pr")))
        self.assertIn(("closes_keyword", target), pr_edges, f"a closing keyword in a PR description must keep producing closes_keyword: {pr_edges}")
        commit_edges = _item_refs(edges_from_commit_messages(REPO, 9, [{"sha": "abc123", "message": "fixes #5"}]))
        self.assertIn(("closes_keyword", target), commit_edges, f"a closing keyword in a commit message must keep producing closes_keyword: {commit_edges}")

class CodeContextReferenceTests(unittest.TestCase):
    def test_references_inside_code_are_ignored(self) -> None:
        """RED at base, GREEN after the fix: a #N token sitting inside
        inline code, a fenced block, a diff hunk, or a > quote emits no
        edge -- neither a mention nor a closing reference. At base every
        context here yields edges."""
        contexts = {
            "inline code": "see `fixes #5` in the docs",
            "fenced block": "example:\n```\nfixes #5\n```\ndone",
            "diff hunk": "@@ -1,2 +1,3 @@\n+ fixes #5\n- see #5",
            "quote": "> fixes #5",
        }
        for label, text in contexts.items():
            self.assertEqual(iter_closing_locations(text, REPO), [], f"a closing keyword inside {label} must emit no edge")
            self.assertEqual(iter_mention_locations(text, REPO), [], f"a #N mention inside {label} must emit no edge")
        self.assertEqual([n for n, _ in iter_mention_locations("see #5", REPO)], [5],
                         "the same token OUTSIDE any masked context still references")

class CommentIdForeignRefTests(unittest.TestCase):
    def test_comment_ids_and_spaced_foreign_refs_dropped(self) -> None:
        """RED at base, GREEN after the fix: text saying 'comment #<id>'
        (a comment id, not an item number) or '<owner>/<repo> #N' with a
        space for ANOTHER repository emits no local edge. At base both
        shapes produce mentions edges to items 6732496263 and 308."""
        for label, text in (("a comment id", "as noted in comment #308"),
                            ("a large comment id", "see comment #6732496263 above"),
                            ("a spaced foreign-repo reference", "that landed in other/repo #308"),
                            ("the unspaced foreign form (control)", "that landed in other/repo#308")):
            self.assertEqual(iter_mention_locations(text, REPO), [], f"{label} must not attach locally")
        self.assertEqual([n for n, _ in iter_mention_locations("and see #12 too", REPO)],
                         [12], "a genuine local reference still works")

class _BotGraphQLSource(GraphQLSource):
    """The REAL GraphQLSource driven fully offline: the REST listing, the
    per-item comment fallback and the releases ride the _api transport seam
    (self._api = self._rest); the capability probe SUCCEEDS offline with
    the full field set (a failed probe would DEGRADE the corrected sync to
    REST, whose fixture has no GraphQL bot comment to map); the bulk
    children ride the rendered-query seam (_graphql, recorded). Issue 5's
    comment nodes carry author __typename "Bot" -- the Actor interface's
    ONLY type discriminator, which the AC19 selection must carry into
    user.type; serve_typename/copilot_typed flip the payload discriminators
    off for the no-downgrade leg."""

    def __init__(self) -> None:
        super().__init__("acme", "forgegate", gh_bin="gh")
        self.queries: list[str] = []
        self.serve_typename = True
        self.copilot_typed = True
        self._api = self._rest  # inject at the _api transport seam

    def _rest(self, path, paginate=False, _enrich_rate=True):
        if path.startswith(f"repos/{REPO}/issues?"):
            return [issue(n, title=f"i{n}", author=a, comments=c, updated_at=ts(n * 10)) for n, a, c in
                    ((1, "cubic-dev-ai[bot]", 0), (2, "alice", 0), (3, "bob", 1), (4, "zed", 0), (5, "dave", 1))]
        if path.startswith(f"repos/{REPO}/issues/3/comments"):
            copilot = comment(50_001, "automated note", author="Copilot")
            if self.copilot_typed:
                copilot["user"]["type"] = "Bot"  # REST-typed bot actor
            return [copilot]
        if path.startswith(f"repos/{REPO}/releases"):
            return [{"id": 90_001, "tag_name": "v1.0.0", "author": {"login": "github-actions[bot]"},
                     "created_at": ts(700), "published_at": ts(701)}]
        return []

    def _graphql(self, query, aliases=None):
        self.queries.append(query)
        self.last_dropped_sections = {}  # the real transport resets it here
        if "__type(name:" in query:  # the capability probe (children queries
            _f = lambda *names: {"fields": [{"name": n} for n in names]}  # SUCCEED offline
            return {"i": _f("comments", "blockedBy", "parent", "duplicateOf",  # stays on GraphQL --
                            "closedByPullRequestsReferences", "timelineItems"),  # a failed probe
                    "p": _f("comments", "additions", "deletions", "changedFiles", "commits",  # would degrade
                            "mergedAt", "mergeCommit", "baseRefName", "headRefName", "isDraft",  # it to REST,
                            "mergedBy", "reviews", "files", "timelineItems",  # whose fixture
                            "closingIssuesReferences"),  # has no GraphQL bot comment to map
                    "ic": _f("databaseId", "body", "createdAt", "updatedAt", "url", "authorAssociation", "author"),
                    "rc": _f("databaseId", "body", "createdAt", "updatedAt", "url", "authorAssociation", "replyTo", "author"),
                    "t": {"enumValues": [{"name": e} for e in ("CLOSED_EVENT", "CROSS_REFERENCED_EVENT", "REOPENED_EVENT")]}}
        author = {"login": "graphbot", "url": "https://github.com/graphbot",
                  **({"__typename": "Bot"} if self.serve_typename else {})}
        return {"repository": {"i5": {"comments": {"totalCount": 1, "pageInfo": {"hasNextPage": False}, "nodes": [
            {"databaseId": 50_050, "body": "typed note", "createdAt": ts(500), "updatedAt": ts(500),
             "url": "https://x/c1", "authorAssociation": "NONE", "author": author}]}}}}

class ActorIsBotTests(TempDBTest):
    """NEW-SURFACE (AC19): actors.is_bot does not exist at base and the real
    GraphQL selection carries no __typename; passing on the fixed tree."""

    def _assert_is_bot(self, configured: bool) -> None:
        got = {str(r[0]): r[1] for r in self.conn.execute("SELECT login, is_bot FROM actors")}
        self.assertEqual(got.get("cubic-dev-ai[bot]"), 1, "a [bot]-suffix login")
        self.assertEqual(got.get("Copilot"), 1, "a REST payload type 'Bot' actor")
        self.assertEqual(got.get("graphbot"), 1, "a GraphQL __typename Bot actor mapped through the REAL selection")
        self.assertEqual(got.get("github-actions[bot]"), 1, "a release actor carrying the flag")
        self.assertEqual(got.get("alice"), 0, "a human author")
        self.assertEqual(got.get("bob"), 0, "a human author")
        self.assertEqual(got.get("dave"), 0, "a human author")
        if configured:
            self.assertEqual(got.get("zed"), 1, f"a login named in the configured comma-separated bot list ({_BOT_LIST_ENV})")
        else:
            # The payload-only contract, asserted WHILE the env var is set
            # (the migration leg runs inside patch.dict): a backfill that
            # consulted the configured list would mark zed as a bot and
            # this assertion is the only thing that notices (PR #76 review
            # PRR-024).
            self.assertEqual(
                got.get("zed"), 0,
                "the v12 backfill must IGNORE the configured bot list even "
                f"while {_BOT_LIST_ENV} is set — payload signals only",
            )

    def test_actors_carry_is_bot(self) -> None:
        """RED at base (missing surface: the real GraphQL author selection
        renders `author { login url }` with no __typename and the store has
        no actors.is_bot), GREEN after the fix: the selection is `author {
        __typename login url }` -- GitHub's Actor interface has NO type
        field, the standard __typename meta-field is the concrete type (Bot,
        User, ...) -- and the mapping carries it into user.type; ingesting
        items/comments/reviews/releases stores actors.is_bot = 1 for bot
        actors -- a login ending in [bot], a REST payload type Bot, a
        GraphQL __typename Bot, or a login in the configured bot list --
        and 0 for humans; migration v12 backfills the payload-derivable
        verdicts on a v11-shaped actors table from the stored payloads
        (no re-fetch, no source), and a later payload WITHOUT bot signals
        never downgrades a stored is_bot=1."""
        self.src = _BotGraphQLSource()
        with patch.dict("zaxbygraph.sync._GH_VERSION_CACHE", {"gh": "offline"}):
            # (a) the REAL mapping: the selection carries __typename into user.type.
            children = self.src.fetch_children([issue(5, title="bulk", author="dave", comments=1, updated_at=ts(50))])
            self.assertTrue(any(re.search(r"author\s*\{[^}]*__typename", q) for q in self.src.queries),
                            "the GraphQL author selection must carry the __typename meta-field "
                            "(GitHub's Actor interface has no type field)")
            self.assertEqual(children[5]["issue_comments"][0]["user"].get("type"), "Bot",
                             "the __typename discriminator must reach the mapped payload's user.type (a store-only change cannot pass)")
            with patch.dict(os.environ, {_BOT_LIST_ENV: "zed, zedbot"}):
                self.sync()
                self._assert_is_bot(configured=True)

                # Migration leg: rebuild actors at the v11 shape (no
                # is_bot), rewind to v11, let the migration backfill from
                # the stored payloads (raw_json user dicts).
                rows = self.conn.execute("SELECT login, html_url FROM actors").fetchall()
                self.conn.execute("DROP TABLE actors")
                self.conn.execute("CREATE TABLE actors (login TEXT PRIMARY KEY, html_url TEXT)")
                self.conn.executemany("INSERT INTO actors(login, html_url) VALUES (?, ?)", [(r["login"], r["html_url"]) for r in rows])
                self.conn.execute("PRAGMA user_version = 11")
                self.conn.commit()
                init_schema(self.conn)
                self._assert_is_bot(configured=False)

                # A later untyped payload must NOT downgrade is_bot=1.
                self.src.serve_typename = False
                self.src.copilot_typed = False
                self.sync(force=True)
                self._assert_is_bot(configured=True)

#: VDBE steps for the ingested issue's own transaction must stay within this
#: factor when the seeded PR corpus grows 1k -> 10k (AC20).
_FLAT_RATIO = 1.25
_SMALL_PRS = 1000
_LARGE_PRS = 10_000

def _scale_ts(offset_s: int) -> str:
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")

def _seed_pull(number: int, head: str) -> dict:
    return {"id": 20_000 + number, "number": number, "additions": 1,
            "deletions": 1, "changed_files": 0, "commits": 0, "draft": False,
            "merged_at": None, "merge_commit_sha": None, "base": {"ref": "main"},
            "head": {"ref": head}, "user": {"login": "alice"}}

class _PagedPRSource(FakeGitHubSource):
    """Zero-child PRs delivered as 100-item listing pages through the REAL
    ingest path, so every seeded row is exactly what the production write
    path stores (including any branch token it derives). Each head_ref
    carries one numeric token; the LAST PR's token names the issue the
    measurement ingests afterwards."""

    PAGE = 100

    def __init__(self, n_prs: int) -> None:
        super().__init__()
        for i in range(1, n_prs + 1):
            head = f"fix/{n_prs + 1}" if i == n_prs else f"fix/{i}-thing"
            self.add_pr(issue(i, title=f"pr-{i}", kind="pr", comments=0,
                              updated_at=ts(i)), _seed_pull(i, head), files=[])

    def list_issues(self, since):
        items = sorted(self.issues.values(), key=lambda r: (r["updated_at"], r["number"]))
        if since is not None:
            items = [r for r in items if r["updated_at"] >= since]
        for start in range(0, len(items), self.PAGE):
            yield [dict(r) for r in items[start:start + self.PAGE]]

class BranchLookupCostTests(unittest.TestCase):
    """AC20: the per-issue branch lookup must be an indexed equality lookup
    on a stored branch token, flat in corpus size (deterministic VDBE step
    counts, never wall time; the tests/test_ingest_scale.py pattern)."""

    @staticmethod
    def _issue_txn_steps(n_prs: int) -> tuple[int, int]:
        """(VDBE steps of the ingested issue's own transaction, branch_ref
        edges emitted) for one issue ingested after seeding n_prs PRs
        through the production ingest path. synchronous=NORMAL is
        fixture-speed only: VDBE steps are untouched by the pragma."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            conn = connect(Path(td) / "history.db")
            try:
                init_schema(conn)
                conn.execute("PRAGMA synchronous = NORMAL")
                seeded = sync_repo(conn, _PagedPRSource(n_prs), REPO)
                if seeded["ingested"] != n_prs:
                    raise AssertionError("seeding must ingest every PR")
                src = FakeGitHubSource()
                src.add_issue(issue(n_prs + 1, title="the issue",
                                    updated_at=ts(900_000)))
                state = {"armed": False, "done": False}
                steps = [0]

                def trace(stmt: str) -> None:
                    norm = " ".join(stmt.split()).upper()
                    if not state["armed"] and "INSERT INTO ITEMS" in norm:
                        state["armed"] = True
                    elif state["armed"] and not state["done"] and norm == "COMMIT":
                        state["done"] = True

                def progress() -> None:
                    if state["armed"] and not state["done"]:
                        steps[0] += 1
                    return None

                conn.set_trace_callback(trace)
                conn.set_progress_handler(progress, 1)
                try:
                    result = sync_repo(conn, src, REPO)
                finally:
                    conn.set_trace_callback(None)
                    conn.set_progress_handler(None, 1)
                if result["ingested"] != 1 or not state["armed"] or not state["done"]:
                    raise AssertionError("measurement needs exactly one observed item")
                branch_edges = int(conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE repo = ? AND rel = 'branch_ref'",
                    (REPO,)).fetchone()[0])
            finally:
                conn.close()
        return steps[0], branch_edges

    def test_branch_lookup_is_indexed(self) -> None:
        """RED at base, GREEN after the fix: when an issue is ingested, the
        branch-linked-PR lookup runs inside the item's transaction and at
        base scans the whole (repo, head_ref) index range per issue -- the
        plan is SEARCH items USING INDEX idx_items_repo_head
        (repo=? AND head_ref>?) and the per-item VDBE step count grows
        roughly with the PR corpus (~9x from 1k to 10k PRs here). The
        contract: an indexed equality lookup on the stored branch token,
        with per-item cost flat within 1.25x between 1k and 10k PRs, while
        the lookup still FINDS the branch-linked PR at both sizes."""
        small_steps, small_edges = self._issue_txn_steps(_SMALL_PRS)
        large_steps, large_edges = self._issue_txn_steps(_LARGE_PRS)
        self.assertEqual(small_edges, 1, "the branch-linked PR must be found at 1k")
        self.assertEqual(large_edges, 1, "the branch-linked PR must be found at 10k")
        self.assertGreater(small_steps, 0, "RATIO UNDEFINED: zero steps measured")
        self.assertLessEqual(
            large_steps, _FLAT_RATIO * small_steps,
            f"RATIO {large_steps / small_steps:.3f} > {_FLAT_RATIO} "
            f"(steps-10k={large_steps} steps-1k={small_steps}): the per-issue "
            "branch lookup cost grows with the PR corpus instead of an "
            "indexed equality lookup on a stored branch token")

#: A TRUE v11 store, hand-built from a frozen v11 DDL (the tables the v12
#: migration touches, plus the FTS indexes a genuine v11 store carries) --
#: never the current DDL first, so the v11 -> v12 upgrade itself is the
#: only thing that runs.
_V11_DDL = """
CREATE TABLE actors (login TEXT PRIMARY KEY, html_url TEXT);
CREATE TABLE items (id INTEGER PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('issue','pr')), node_id TEXT, title TEXT NOT NULL, body TEXT, labels_text TEXT, state TEXT NOT NULL, state_reason TEXT, author TEXT, created_at TEXT, updated_at TEXT, closed_at TEXT, merged_at TEXT, merge_commit TEXT, draft INTEGER NOT NULL DEFAULT 0, locked INTEGER NOT NULL DEFAULT 0, base_ref TEXT, head_ref TEXT, additions INTEGER, deletions INTEGER, changed_files INTEGER, commits INTEGER, html_url TEXT, api_url TEXT, reactions_total INTEGER, milestone TEXT, item_type TEXT, sub_issues_total INTEGER, sub_issues_completed INTEGER, dependencies_blocked_by INTEGER, dependencies_blocking INTEGER, dependencies_total_blocked_by INTEGER, dependencies_total_blocking INTEGER, author_association TEXT, merged_by TEXT, comment_count INTEGER NOT NULL DEFAULT 0, paths_text TEXT NOT NULL DEFAULT '', raw_json TEXT NOT NULL, UNIQUE (repo, number));
CREATE TABLE comments (pk INTEGER PRIMARY KEY AUTOINCREMENT, github_id INTEGER NOT NULL, repo TEXT NOT NULL, number INTEGER NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('issue_comment','review_comment')), author TEXT, author_association TEXT, created_at TEXT, updated_at TEXT, body TEXT, html_url TEXT, in_reply_to INTEGER, raw_json TEXT NOT NULL, UNIQUE (repo, kind, github_id));
CREATE TABLE reviews (id INTEGER PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL, author TEXT, state TEXT, submitted_at TEXT, body TEXT, html_url TEXT, raw_json TEXT NOT NULL);
CREATE TABLE pr_commits (repo TEXT NOT NULL, number INTEGER NOT NULL, sha TEXT NOT NULL, message TEXT, PRIMARY KEY (repo, number, sha));
CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, src_type TEXT NOT NULL, src_id TEXT NOT NULL, rel TEXT NOT NULL, dst_type TEXT NOT NULL, dst_id TEXT NOT NULL, confidence TEXT NOT NULL CHECK (confidence IN ('EXTRACTED')), evidence TEXT, source TEXT NOT NULL, UNIQUE (repo, src_type, src_id, rel, dst_type, dst_id, source));
CREATE TABLE sync_state (repo TEXT PRIMARY KEY, issues_since TEXT, last_full_sync_at TEXT, last_incr_sync_at TEXT, last_error TEXT, item_count INTEGER NOT NULL DEFAULT 0, comment_count INTEGER NOT NULL DEFAULT 0, edge_count INTEGER NOT NULL DEFAULT 0, include_patches INTEGER NOT NULL DEFAULT 0, full_sync_pending INTEGER NOT NULL DEFAULT 0, rate_limit_remaining INTEGER, rate_limit_reset_at TEXT, fetch_source TEXT);
CREATE VIRTUAL TABLE items_fts USING fts5(title, body, labels_text, paths_text, tokenize = 'porter unicode61', content='items', content_rowid='id');
CREATE VIRTUAL TABLE comments_fts USING fts5(body, tokenize = 'porter unicode61', content='comments', content_rowid='pk');
CREATE VIRTUAL TABLE reviews_fts USING fts5(body, tokenize = 'porter unicode61', content='reviews', content_rowid='id');
"""

class V12KeywordRederivationTests(unittest.TestCase):
    """NEW-SURFACE (AC21): no v12 exists at base, so the migration chain
    leaves the old-rule keyword edges untouched and the first assertion
    fails -- the expected base leg; the same assertions pass on the fixed
    tree."""

    @staticmethod
    def _seed_v11_store(conn) -> None:
        """Build a TRUE v11 store on a fresh database: run the frozen _V11_DDL,
        seed rows under the OLD extraction rules (comment/review-derived
        closes_keyword, noise edges, the legitimate PR-body/commit closes,
        branch_ref/reverts, the GitHub-reported streams, a malformed-payload
        row, a second repo), prime the FTS indexes, stamp user_version = 11.
        The upgrade is then ONE init_schema call."""

        def payload(number: int, title: str, body: str, kind: str = "issue") -> str:
            rec = {"id": 10_000 + number, "number": number, "title": title, "body": body,
                   "state": "open", "user": {"login": "alice"}, "labels": [], "comments": 0,
                   "created_at": ts(number), "updated_at": ts(number * 10)}
            if kind == "pr":
                rec["pull_request"] = {"url": f"https://x/{number}"}
            return json.dumps(rec, separators=(",", ":"))

        conn.executescript(_V11_DDL)
        rows = [  # (id, number, kind, title, body, head_ref) -- OLD-rule refs
            (10005, 5, "issue", "target", "the canonical target", None), (10006, 6, "issue", "blocked target", "blocked", None),
            (10007, 7, "issue", "timeline closer", "closes nothing", None), (10008, 8, "pr", "pr body", "merge it", "feat/8"),  # closing_ref + review
            (10009, 9, "issue", "comment owner", "owns comments", None), (10010, 10, "issue", "masked target", "a target", None),
            (10011, 11, "pr", "closer PR", "fixes #5", "feat/11"), (10012, 12, "issue", "code-span noise", "see `fixes #13` in the docs", None),
            (10014, 14, "issue", "quote noise", "> closes #15 quoted", None), (10016, 16, "issue", "foreign noise", "that landed in other/repo #17", None),
            (10018, 18, "issue", "duplicate marker", "duplicate of #19", None), (10019, 19, "issue", "duplicate target", "the duplicate", None),
            (10020, 20, "issue", "masked duplicate", "see `duplicate of #21`", None), (10022, 22, "pr", "commit-only closer", "no keyword here", "feat/22"),
            (10030, 30, "pr", 'Revert "target"', "revert body", "feat/30"), (10031, 31, "pr", "sha revert", "This reverts commit feedface0011", "feat/31"),
            (10032, 32, "pr", "branch linked", "branchy", "fix/5-thing"), (10040, 40, "issue", "malformed payload", "fixes #41", None),  # raw_json broken
            (10042, 42, "issue", "", "", None)]  # raw_json broken AND no structured text to derive from
        conn.executemany(
            "INSERT INTO items(id, repo, number, kind, title, body, state, author,"
            " created_at, updated_at, head_ref, raw_json)"
            " VALUES (?,?,?,?,?,?,'open','alice',?,?,?,?)",
            [(rid, repo, number, kind, title, body, ts(number), ts(number * 10), head, payload(number, title, body, kind))
             for rid, number, kind, title, body, head in rows for repo in (REPO,)] + [
            (11002, "acme/other", 2, "pr", "other closer", "fixes #1", ts(2), ts(20), "feat/2", payload(2, "other closer", "fixes #1", "pr")),
            (11001, "acme/other", 1, "issue", "other target", "target", ts(1), ts(10), None, payload(1, "other target", "target"))])
        conn.execute("UPDATE items SET raw_json = '{not valid json' WHERE number IN (40, 42)")
        for gid, body in ((50_001, "fixes #5"), (50_002, "see `fixes #10`")):
            conn.execute(
                "INSERT INTO comments(github_id, repo, number, kind, author, body,"
                " created_at, updated_at, raw_json) VALUES (?,?,?,'issue_comment','bob',?,?,?,?)",
                (gid, REPO, 9, body, ts(90), ts(90), json.dumps({"id": gid, "body": body, "user": {"login": "bob"}, "created_at": ts(90), "updated_at": ts(90)}, separators=(",", ":"))))
        conn.execute(
            "INSERT INTO reviews(id, repo, number, author, state, body, submitted_at, raw_json)"
            " VALUES (70001, ?, 8, 'bob', 'APPROVED', 'fixes #5', ?, ?)",
            (REPO, ts(95), json.dumps({"id": 70001, "state": "APPROVED", "body": "fixes #5", "user": {"login": "bob"}}, separators=(",", ":"))))
        conn.executemany("INSERT INTO pr_commits(repo, number, sha, message) VALUES (?,?,?,?)",
                         [(REPO, 11, "deadbeef", "fixes #5"), (REPO, 22, "deadbee2", "fixes #5")])
        conn.execute("INSERT INTO actors(login, html_url) VALUES ('alice', 'https://github.com/alice'), ('bob', 'https://github.com/bob')")
        conn.execute("INSERT INTO sync_state(repo, last_full_sync_at) VALUES (?, ?), ('acme/other', ?)", (REPO, ts(800), ts(801)))
        keyword_edges = [  # (src_id, rel, dst_id, evidence) -- OLD-rule + survivors
            ("9", "closes_keyword", "5", "comment:50001 closing keyword"), ("9", "closes_keyword", "10", "comment:50002 closing keyword"),
            ("8", "closes_keyword", "5", "review:70001 closing keyword"), ("11", "closes_keyword", "5", "body@4 closing keyword; commit deadbeef closing keyword"),
            ("12", "closes_keyword", "13", "body@4 closing keyword"), ("14", "closes_keyword", "15", "body@2 closing keyword"),
            ("16", "mentions", "17", "body@17"), ("18", "duplicate_keyword", "19", "body@0"),
            ("20", "duplicate_keyword", "21", "body@4"), ("22", "closes_keyword", "5", "commit deadbee2 closing keyword"),
            ("30", "reverts", "5", "title reverts quoted item title"),
            ("32", "branch_ref", "5", "head_ref fix/5-thing"),
            ("40", "closes_keyword", "41", "body@0 closing keyword"),
            ("42", "closes_keyword", "43", "body@0 closing keyword")]
        reported_edges = [  # GitHub-reported streams + payload: must survive v12
            ("7", "closes", "5", "timeline closed event 2026-01-01T00:01:30Z", "timeline"),
            ("8", "closes", "5", "closingIssuesReferences", "closing_ref"),
            ("9", "blocked_by", "6", "links.blocked_by", "link")]
        edge_sql = ("INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
                    " confidence, evidence, source) VALUES (?, 'item', ?, ?, 'item', ?, 'EXTRACTED', ?, ?)")
        conn.executemany(edge_sql, [(REPO, *e, "keyword") for e in keyword_edges] + [(REPO, *e) for e in reported_edges])
        conn.execute("INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id, confidence, evidence, source)"
                     " VALUES (?, 'item', '31', 'reverts', 'commit', 'feedface0011', 'EXTRACTED', 'body reverts commit sha', 'keyword')", (REPO,))
        conn.execute("INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id, confidence, evidence, source)"
                     " VALUES (?, 'actor', 'alice', 'authored', 'item', '5', 'EXTRACTED', 'user.login', 'payload')", (REPO,))
        conn.executemany(edge_sql, [
            ("acme/other", "2", "closes_keyword", "1", "body@0 closing keyword", "keyword"),
            ("acme/other", "2", "closes", "1", "timeline closed event 2026-01-01T00:01:40Z", "timeline")])
        for fts in ("items_fts", "comments_fts", "reviews_fts"):
            conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
        conn.execute("PRAGMA user_version = 11")
        conn.commit()

    def test_v12_rederives_keyword_edges(self) -> None:
        """RED at base (no v12: the old comment-derived closes_keyword edge
        survives the migration chain untouched), GREEN after the fix:
        migration v12 re-derives EVERY source='keyword' relation offline
        from the stored payloads -- comment/review-derived closes_keyword
        gone (the comment's and the review's 'fixes #5' become mentions),
        duplicate/code-span/quote/foreign noise gone, the PR-body and
        commit-message closes_keyword kept (a commit-only close included),
        the isolated positive duplicate_keyword kept, branch_ref and reverts
        preserved or correctly rebuilt -- while every timeline, closing_ref
        and link edge survives byte-identically, the malformed-row policy
        holds exactly (a row with unparseable raw_json but usable structured
        text re-derives from the structured column; a row with neither keeps
        its prior relation edge), a second repo's rows stay correct,
        and repeated provenance unions into ONE row (no source constructed:
        the network seam itself is pinned off)."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            conn = connect(Path(td) / "history.db")
            try:
                self._seed_v11_store(conn)
                reported_sql = ("SELECT repo, src_type, src_id, rel, dst_type, dst_id,"
                                " confidence, evidence, source FROM edges WHERE source != 'keyword'")
                reported_before = set(conn.execute(reported_sql))
                with patch("zaxbygraph.github.subprocess.run",
                           side_effect=AssertionError("the migration must not touch the network seam")):
                    init_schema(conn)  # the ONE upgrade call: v11 -> v12

                stale = conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE repo = ? AND rel = 'closes_keyword'"
                    " AND (evidence LIKE 'comment:%' OR evidence LIKE 'review:%')", (REPO,)).fetchone()[0]
                self.assertEqual(stale, 0, "comment/review-derived closes_keyword edges must be "
                                 "re-derived away (comments and reviews record mentions, never closes_keyword)")
                self.assertGreaterEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 12,
                                        "the v12 migration must run (a malformed stored payload row must not abort the chain)")
                for src_id in ("8", "9"):
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM edges WHERE repo = ? AND src_id = ? AND rel = 'closes_keyword' AND dst_id = '5'",
                        (REPO, src_id)).fetchone()[0], 0)
                    self.assertGreaterEqual(conn.execute(
                        "SELECT COUNT(*) FROM edges WHERE repo = ? AND src_id = ? AND rel = 'mentions' AND dst_id = '5' AND source = 'keyword'",
                        (REPO, src_id)).fetchone()[0], 1,
                        f"item {src_id}'s textual reference must be re-derived as a mention")
                for src_id, dst_id in (("9", "10"), ("12", "13"), ("14", "15"), ("16", "17"), ("20", "21")):
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM edges WHERE repo = ? AND src_id = ? AND dst_id = ?",
                        (REPO, src_id, dst_id)).fetchone()[0], 0,
                        f"the noise edge {src_id}->{dst_id} must be re-derived away")
                # Malformed-row policy, asserted exactly: item 40's raw_json is
                # unparseable but its STRUCTURED body "fixes #41" still derives
                # -- the mention lands and the old issue-body close does NOT
                # survive; item 42's raw_json is unparseable AND its structured
                # text is empty, so nothing can be faithfully re-derived and its
                # prior relation edge must SURVIVE (a parse failure never
                # discards evidence).
                self.assertGreaterEqual(conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE repo = ? AND src_id = '40' AND rel = 'mentions' AND dst_id = '41' AND source = 'keyword'",
                    (REPO,)).fetchone()[0], 1,
                    "item 40's mention must be re-derived from the structured body column despite the malformed raw_json")
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE repo = ? AND src_id = '40' AND rel = 'closes_keyword' AND dst_id = '41'",
                    (REPO,)).fetchone()[0], 0,
                    "item 40's old issue-body closes_keyword must not survive merely because its raw_json failed to parse")
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE repo = ? AND src_id = '42' AND rel = 'closes_keyword' AND dst_id = '43' AND source = 'keyword'",
                    (REPO,)).fetchone()[0], 1,
                    "item 42's prior relation edge must survive: no structured text can re-derive it, and a parse failure must not discard evidence")
                # PR-body + commit close: kept ONCE with the provenances merged.
                merged = conn.execute(
                    "SELECT COUNT(*), MAX(evidence) FROM edges WHERE repo = ? AND src_id = '11' AND rel = 'closes_keyword' AND dst_id = '5'",
                    (REPO,)).fetchone()
                self.assertEqual(merged[0], 1, "the PR-body/commit closes_keyword edge must survive the re-derivation exactly once")
                self.assertIn("body", merged[1] or "", "the merged evidence must keep the body provenance")
                self.assertIn("deadbeef", merged[1] or "", "the merged evidence must keep the commit provenance")
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE repo = ? AND src_id = '22' AND rel = 'closes_keyword' AND dst_id = '5'",
                    (REPO,)).fetchone()[0], 1, "a commit-only close (PR body without the keyword) must survive")
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE repo = ? AND src_id = '18' AND rel = 'duplicate_keyword' AND dst_id = '19'",
                    (REPO,)).fetchone()[0], 1, "the isolated positive duplicate_keyword must survive")
                for src_id, rel, dst_type, dst_id in (("30", "reverts", "item", "5"),
                                                      ("31", "reverts", "commit", "feedface0011"),
                                                      ("32", "branch_ref", "item", "5")):
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM edges WHERE repo = ? AND src_id = ? AND rel = ? AND dst_type = ? AND dst_id = ?",
                        (REPO, src_id, rel, dst_type, dst_id)).fetchone()[0], 1,
                        f"the {rel} edge must be preserved or correctly rebuilt")
                # Every GitHub-reported edge survives byte-identically.
                self.assertEqual(set(conn.execute(reported_sql)), reported_before,
                                 "every timeline/closing_ref/link/payload edge must survive v12 byte-identically")
                # A second repo's rows stay correct and undamaged.
                for rel, source in (("closes_keyword", "keyword"), ("closes", "timeline")):
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM edges WHERE repo = 'acme/other' AND"
                        " src_id = '2' AND rel = ? AND dst_id = '1' AND source = ?",
                        (rel, source)).fetchone()[0], 1,
                        f"the other repo's {source} edge must stay intact")
            finally:
                conn.close()

if __name__ == "__main__":
    unittest.main()
