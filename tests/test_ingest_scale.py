"""Acceptance checks for issue #22: cold-sync ingest scale and indexed reads.

Phase 2.5 frozen checks, authored at arm's length from the acceptance
criteria and the verified root cause only (no fix plan consulted). Every
check here is DISCRIMINATING: RED at base, GREEN after the fix.

Plan assertions run on fresh, never-ANALYZEd databases so no check depends
on sqlite_stat1. Cost assertions count deterministic VDBE steps (progress
handler), never wall time. The fixture pattern is copied in miniature from
tests/fixtures.py on purpose: this module must stay self-contained so
discovery of this one file reproduces each check without importing the
shared fixtures.

Amended after plan-critic round 1 (all checks still RED at base, GREEN
after the fix): AC1 now captures the runtime DELETE from a real ingest
trace instead of EQP'ing a hardcoded copy; AC3 pins the finish-only recount
policy (exactly one call per run); AC4 adds a whole-run amortized-cost leg
so quadratic work cannot hide outside the item transaction; AC5 requires a
SEARCH binding the incident id (not merely "not repo-only") and pins UNION
ALL row multiplicity and provenance via an equivalence leg.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

REPO = "acme/forgegate"

#: VDBE steps for the single ingested item must stay within this factor when
#: the seeded corpus doubles (AC4); ~2x at base, ~1x once per-item statements
#: stop scanning the corpus.
FLAT_RATIO = 1.25

#: Corpus sizes for the per-item cost check (direct SQL seeding; the cost of
#: building the corpus must not be part of what is measured).
CORPUS_SMALL = 2000
CORPUS_LARGE = 4000

#: Listing page size for the whole-run amortized leg of the cost check.
PAGE_SIZE = 250


def ts(offset_s: int = 0) -> str:
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue(
    number: int,
    *,
    title: str = "title",
    body: str = "",
    state: str = "open",
    author: str = "alice",
    labels: list[dict] | None = None,
    comments: int = 0,
    updated_at: str | None = None,
    created_at: str | None = None,
    kind: str = "issue",
) -> dict:
    rec = {
        "id": 10_000 + number,
        "number": number,
        "node_id": f"I_{number}",
        "title": title,
        "body": body,
        "state": state,
        "user": {"login": author, "html_url": f"https://github.com/{author}"},
        "labels": labels or [],
        "comments": comments,
        "created_at": created_at or ts(number),
        "updated_at": updated_at or ts(number * 10),
        "closed_at": None if state == "open" else ts(number * 10 + 1),
        "locked": False,
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "url": f"https://api.github.com/repos/{REPO}/issues/{number}",
    }
    if kind == "pr":
        rec["pull_request"] = {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"}
        rec["html_url"] = f"https://github.com/{REPO}/pull/{number}"
    return rec


def pull(number: int, *, changed_files: int = 1, merged: bool = False) -> dict:
    return {
        "id": 20_000 + number,
        "number": number,
        "additions": 1,
        "deletions": 1,
        "changed_files": changed_files,
        "commits": 1,
        "draft": False,
        "merged_at": ts(number * 10 + 5) if merged else None,
        "merge_commit_sha": "abc" if merged else None,
        "base": {"ref": "main"},
        "head": {"ref": f"feat/{number}"},
        "user": {"login": "alice"},
    }


def comment(cid: int, body: str, author: str = "alice") -> dict:
    return {
        "id": cid,
        "body": body,
        "user": {"login": author, "html_url": f"https://github.com/{author}"},
        "created_at": ts(cid % 100_000),
        "updated_at": ts(cid % 100_000),
        "html_url": f"https://github.com/{REPO}/issues/1#issuecomment-{cid}",
    }


def review(rid: int, state: str, body: str = "", author: str = "bob") -> dict:
    return {
        "id": rid,
        "state": state,
        "body": body,
        "user": {"login": author},
        "submitted_at": ts(rid % 100_000),
        "html_url": f"https://github.com/{REPO}/pull/1#pullrequestreview-{rid}",
    }


def pr_file(path: str) -> dict:
    return {
        "filename": path,
        "status": "modified",
        "additions": 3,
        "deletions": 1,
        "changes": 4,
        "sha": "deadbeef",
        "patch": "@@ -1 +1 @@\n-old\n+new\n",
    }


class FakeSource:
    """Minimal in-memory GitHub source (pattern copied from tests/fixtures.py;
    deliberately not imported). ``pages`` forces whole-listing-page delivery
    (one list payload per page) instead of one item per yield."""

    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self.pulls: dict[int, dict] = {}
        self.issue_comments: dict[int, list[dict]] = {}
        self.reviews: dict[int, list[dict]] = {}
        self.review_comments: dict[int, list[dict]] = {}
        self.files: dict[int, list[dict]] = {}
        self.pages: list[list[dict]] | None = None

    def add_pr(self, rec: dict, pull_raw: dict, files: list[dict]) -> None:
        n = int(rec["number"])
        self.issues[n] = deepcopy(rec)
        self.pulls[n] = deepcopy(pull_raw)
        self.files[n] = deepcopy(files)
        self.issue_comments.setdefault(n, [])
        self.reviews.setdefault(n, [])
        self.review_comments.setdefault(n, [])

    def list_issues(self, since: str | None):
        if self.pages is not None:
            for page in self.pages:
                if since is not None:
                    page = [r for r in page if r.get("updated_at", "") >= since]
                yield deepcopy(page)
            return
        items = sorted(self.issues.values(), key=lambda r: (r["updated_at"], r["number"]))
        for rec in items:
            if since is None or rec["updated_at"] >= since:
                yield deepcopy(rec)

    def get_pull(self, number: int) -> dict:
        return deepcopy(self.pulls[number])

    def list_issue_comments(self, number: int) -> list[dict]:
        return deepcopy(self.issue_comments.get(number, []))

    def list_reviews(self, number: int) -> list[dict]:
        return deepcopy(self.reviews.get(number, []))

    def list_review_comments(self, number: int) -> list[dict]:
        return deepcopy(self.review_comments.get(number, []))

    def list_pr_files(self, number: int) -> list[dict]:
        return deepcopy(self.files.get(number, []))

    def list_releases(self) -> list[dict]:
        return []


def one_new_pr(n: int) -> FakeSource:
    """A source whose listing is exactly one structurally-identical new PR:
    2 issue comments, 1 review, 1 review comment, 2 files."""
    src = FakeSource()
    src.add_pr(
        issue(n, title=f"pr-{n}", comments=2, kind="pr", updated_at=ts(900_000 + n)),
        pull(n, changed_files=2),
        [pr_file(f"src/mod{n}/a.py"), pr_file(f"src/mod{n}/b.py")],
    )
    src.issue_comments[n] = [
        comment(50_000 + n * 10, "note 0"),
        comment(50_001 + n * 10, "note 1"),
    ]
    src.issues[n]["comments"] = 2
    src.reviews[n] = [review(60_000 + n, "APPROVED", body="ok")]
    src.review_comments[n] = [comment(70_000 + n, "inline note", author="bob")]
    return src


class IngestScaleChecks(unittest.TestCase):
    """Issue #22 acceptance checks. RED at base, GREEN after the fix."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db_path = Path(self._td.name) / "history.db"
        self.conn = connect(self.db_path)
        init_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._td.cleanup()

    def sync(self, src: FakeSource, **kwargs):
        return sync_repo(self.conn, src, REPO, **kwargs)

    # --- AC1: actor-edge DELETE plan -------------------------------------

    def test_actor_delete_plan_uses_dst_index(self) -> None:
        """RED at base, GREEN after the fix. The actor-edge DELETE that the
        real ingest path executes (store.delete_owned_edges, reached through
        sync_repo) must plan a full-key SEARCH on idx_edges_dst binding
        repo, dst_type AND dst_id. The statement is captured from the
        connection trace of a real small ingest and EXPLAIN QUERY PLAN'd on
        the same connection, so only a change to the runtime statement (for
        example an INDEXED BY pin) can flip this check - an ad-hoc copy of
        the SQL text could keep passing while the shipped DELETE keeps
        scanning the repo's edges."""
        src = FakeSource()
        src.pages = [[issue(1), issue(2)]]
        captured: list[str] = []
        self.conn.set_trace_callback(captured.append)
        try:
            self.sync(src)
        finally:
            self.conn.set_trace_callback(None)
        deletes = [
            s
            for s in captured
            if " ".join(s.split()).upper().startswith("DELETE FROM EDGES")
            and "dst_type" in s
            and "rel IN" in s
        ]
        self.assertTrue(
            deletes,
            "ACTOR-DELETE-PLAN: no runtime actor DELETE captured from the ingest trace",
        )
        details = [
            row[3].strip()
            for row in self.conn.execute("EXPLAIN QUERY PLAN " + deletes[0])
        ]
        ok = any(
            "idx_edges_dst" in d and "dst_type=?" in d and "dst_id=?" in d
            for d in details
        )
        self.assertTrue(
            ok,
            "ACTOR-DELETE-PLAN: " + " ; ".join(details),
        )

    # --- AC2: revert-title lookup plan ------------------------------------

    def test_title_lookup_plan_uses_title_index(self) -> None:
        """RED at base, GREEN after the fix. The revert-title lookup
        (store.py rebuild_edges: SELECT number FROM items WHERE repo = ? AND
        title = ? AND number != ?) must be served by an index whose constraint
        list binds title=; at base only idx_items_repo_merge (repo=?) serves
        it, so every items row (raw_json included) is read per lookup. The
        check deliberately does not name the new index - any items index that
        seeks on title satisfies it."""
        details = [
            row[3].strip()
            for row in self.conn.execute(
                "EXPLAIN QUERY PLAN SELECT number FROM items "
                "WHERE repo = ? AND title = ? AND number != ?",
                (REPO, "some title", 1),
            )
        ]
        ok = any(
            re.search(r"USING (COVERING )?INDEX [^ ]+ \([^)]*title=\?", d)
            for d in details
        )
        self.assertTrue(
            ok,
            "TITLE-LOOKUP-PLAN: " + " ; ".join(details),
        )

    # --- AC3: recount is finish-only ---------------------------------------

    def test_recount_not_per_item(self) -> None:
        """RED at base, GREEN after the fix. The frozen policy is a
        finish-only recount: store.recount runs exactly ONCE per sync_repo
        run, at finish - never per item, never per listing page, and never
        zero times (a run that recounts nowhere would leave the stored
        counts drifting forever). A 5-item listing delivered as two pages
        must produce exactly one call; at base the per-item recount (5) plus
        the finish recount (1) produce six."""
        import zaxbygraph.store as store_mod
        import zaxbygraph.sync as sync_mod

        src = FakeSource()
        src.pages = [
            [issue(1), issue(2), issue(3)],
            [issue(4), issue(5)],
        ]
        calls: list[str] = []
        real = store_mod.recount

        def counting(conn, repo):
            calls.append(repo)
            return real(conn, repo)

        store_mod.recount = counting
        sync_mod.recount = counting
        try:
            self.sync(src)
        finally:
            store_mod.recount = real
            sync_mod.recount = real
        self.assertEqual(
            len(calls),
            1,
            f"RECOUNT CALLS {len(calls)} != 1 (finish-only policy; "
            "5 items across 2 listing pages)",
        )

    # --- AC4: per-item ingest cost is flat in corpus size ------------------

    def _corpus_item_txn_steps(self, n_items: int) -> int:
        """Deterministic VDBE step count for ingesting ONE new PR into a
        directly-seeded corpus of n_items items. Only the item's own
        transaction is counted (armed at its INSERT INTO items, disarmed at
        the matching COMMIT) so sync prologue/epilogue work - which is
        legitimately O(corpus), e.g. the finish recount and PRAGMA optimize -
        cannot mask the per-item contract."""
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "history.db")
            init_schema(conn)
            conn.executemany(
                "INSERT INTO items(id, repo, number, kind, title, state, author,"
                " created_at, updated_at, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (1000 + i, REPO, i, "pr", f"pr-{i}", "closed", "alice", ts(i), ts(i), "{}")
                    for i in range(1, n_items + 1)
                ],
            )
            comment_rows = []
            edge_rows = []
            for i in range(1, n_items + 1):
                nid = str(i)
                comment_rows.extend(
                    [
                        (900_000 + i * 10, REPO, i, "issue_comment", "alice", ts(i), "{}"),
                        (900_001 + i * 10, REPO, i, "issue_comment", "alice", ts(i), "{}"),
                        (900_002 + i * 10, REPO, i, "review_comment", "bob", ts(i), "{}"),
                    ]
                )
                edge_rows.extend(
                    [
                        (REPO, "actor", "alice", "authored", "item", nid, "payload"),
                        (REPO, "actor", "alice", "commented", "item", nid, "payload"),
                        (REPO, "actor", "bob", "commented", "item", nid, "payload"),
                        (REPO, "actor", "bob", "reviewed", "item", nid, "payload"),
                        (REPO, "item", nid, "touches", "file", f"src/mod{i}/a.py", "payload"),
                        (REPO, "item", nid, "touches", "file", f"src/mod{i}/b.py", "payload"),
                    ]
                )
            conn.executemany(
                "INSERT INTO comments(github_id, repo, number, kind, author,"
                " created_at, raw_json) VALUES (?,?,?,?,?,?,?)",
                comment_rows,
            )
            conn.executemany(
                "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
                " confidence, evidence, source) VALUES (?,?,?,?,?,?,'EXTRACTED','seed',?)",
                edge_rows,
            )
            conn.commit()

            src = one_new_pr(n_items + 1)
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
            conn.close()
        self.assertEqual(
            result["ingested"],
            1,
            f"INGESTED {result['ingested']} != 1: measurement needs exactly one new item",
        )
        if not state["armed"] or not state["done"]:
            self.fail("ITEM-TXN-NEVER-ARMED: the item transaction window was not observed")
        return steps[0]

    def _full_sync_amortized_ticks(self, n_items: int) -> tuple[int, int]:
        """(progress ticks, items ingested) for a whole sync_repo run that
        fully ingests a fresh corpus of n_items zero-comment issues delivered
        as listing pages. The handler period is 100 VDBE steps (a
        deterministic count in a coarser unit) so the quadratic base run
        stays fast; both corpora use the same period, so the ratio compared
        by the caller is unaffected."""
        src = FakeSource()
        pages: list[list[dict]] = []
        for start in range(1, n_items + 1, PAGE_SIZE):
            stop = min(start + PAGE_SIZE, n_items + 1)
            pages.append(
                [issue(n, updated_at=ts(100_000 + n)) for n in range(start, stop)]
            )
        src.pages = pages
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "history.db")
            init_schema(conn)
            ticks = [0]

            def progress() -> None:
                ticks[0] += 1
                return None

            conn.set_progress_handler(progress, 100)
            try:
                result = sync_repo(conn, src, REPO)
            finally:
                conn.set_progress_handler(None, 100)
                conn.close()
        return ticks[0], int(result["ingested"])

    def test_per_item_cost_is_flat(self) -> None:
        """RED at base, GREEN after the fix. Two legs, both deterministic
        VDBE step counts (never wall time).
        Item-transaction leg: ingesting one structurally identical item must
        cost the same order of VDBE steps at 4k corpus size as at 2k; at
        base the per-item transaction scans the whole corpus (actor-edge
        DELETE, two title lookups, three COUNT aggregates) and doubles.
        Whole-run leg: total steps per ingested item across a FULL sync of
        each corpus must also stay within the same factor, so quadratic
        work cannot hide outside the item transaction (for example in
        per-page recounting) - the issue's headline claim is that the whole
        cold sync is linear, not just the per-item transaction."""
        small = self._corpus_item_txn_steps(CORPUS_SMALL)
        large = self._corpus_item_txn_steps(CORPUS_LARGE)
        self.assertGreater(small, 0, "RATIO UNDEFINED: zero steps measured at 2k corpus")
        self.assertLessEqual(
            large,
            FLAT_RATIO * small,
            f"RATIO {large / small:.3f} > {FLAT_RATIO} "
            f"(steps-4k={large} steps-2k={small}): per-item ingest cost grows with corpus size",
        )
        small_ticks, small_items = self._full_sync_amortized_ticks(CORPUS_SMALL)
        large_ticks, large_items = self._full_sync_amortized_ticks(CORPUS_LARGE)
        self.assertEqual(
            small_items,
            CORPUS_SMALL,
            f"INGESTED {small_items} != {CORPUS_SMALL} in the whole-run leg",
        )
        self.assertEqual(
            large_items,
            CORPUS_LARGE,
            f"INGESTED {large_items} != {CORPUS_LARGE} in the whole-run leg",
        )
        per_small = small_ticks / small_items
        per_large = large_ticks / large_items
        self.assertLessEqual(
            per_large,
            FLAT_RATIO * per_small,
            f"RATIO {per_large / per_small:.3f} > {FLAT_RATIO} "
            f"(whole-run amortized: per-item-4k={per_large:.1f} "
            f"per-item-2k={per_small:.1f} progress ticks): total sync cost "
            "grows super-linearly with corpus size",
        )

    # --- AC5: item/related/what_closed edge reads are indexed ---------------

    def test_item_edge_reads_are_indexed(self) -> None:
        """RED at base, GREEN after the fix. Two legs.
        Plan leg: every edge-table SELECT executed by the real read paths
        (query.item edges, query.related per frontier node, query.what_closed
        PR and commit rows) must EXPLAIN as an index SEARCH that binds the
        incident id: a plan row that is a SCAN, ends in a bare (repo=?), or
        carries neither src_id=? nor dst_id=? is a repo-edge scan in
        disguise. At base item/related plan exactly such a scan; on this
        build what_closed's rows already seek full-key, which is expected
        and fine - the assertion is conjunctive over every captured
        statement.
        Equivalence leg: the read paths must return each stored incident
        edge exactly once with its provenance fields - a self-incident edge
        (matched by both arms of a UNION ALL rewrite) and two same-pair rows
        differing only by source must each come back once (multiset equality
        against the edges table)."""
        from zaxbygraph import query
        from zaxbygraph.store import insert_edge

        self.conn.executemany(
            "INSERT INTO items(id, repo, number, kind, title, state, author,"
            " created_at, updated_at, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (101, REPO, 1, "issue", "issue one", "closed", "alice", ts(1), ts(1), "{}"),
                (102, REPO, 2, "pr", "pr two", "closed", "alice", ts(2), ts(2), "{}"),
                (103, REPO, 3, "issue", "issue three", "open", "alice", ts(3), ts(3), "{}"),
                (104, REPO, 4, "pr", "pr four", "open", "alice", ts(4), ts(4), "{}"),
            ],
        )
        self.conn.executemany(
            "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
            " confidence, evidence, source) VALUES (?,?,?,?,?,?,'EXTRACTED','seed',?)",
            [
                (REPO, "item", "2", "mentions", "item", "1", "keyword"),
                (REPO, "item", "3", "mentions", "item", "1", "keyword"),
                (REPO, "item", "2", "closes", "item", "1", "closing_ref"),
                (REPO, "actor", "alice", "authored", "item", "2", "payload"),
                (REPO, "actor", "bob", "commented", "item", "3", "payload"),
                (REPO, "item", "4", "touches", "file", "src/a.py", "payload"),
                (REPO, "actor", "alice", "reviewed", "item", "4", "payload"),
            ],
        )
        self.conn.commit()

        captured: list[str] = []
        self.conn.set_trace_callback(captured.append)
        try:
            query.item(self.conn, 1, repo=REPO)
            query.related(self.conn, 1, depth=2, repo=REPO)
            query.what_closed(self.conn, 1, repo=REPO)
        finally:
            self.conn.set_trace_callback(None)

        edge_stmts = [
            s for s in captured if " from edges" in " ".join(s.split()).lower()
        ]
        self.assertGreaterEqual(
            len(edge_stmts),
            4,
            f"TRACED-EDGE-STATEMENTS {len(edge_stmts)} < 4: read paths did not reach the edges table",
        )
        offenders = []
        for stmt in edge_stmts:
            concrete = []
            for row in self.conn.execute("EXPLAIN QUERY PLAN " + stmt):
                detail = str(row[3]).strip()
                # Only concrete edge-table nodes are judged; a compound query
                # (the UNION ALL shape the fix is expected to take) also
                # emits scaffolding rows (COMPOUND QUERY, LEFT-MOST
                # SUBQUERY, UNION ALL) that carry no plan of their own.
                if re.match(r"(SEARCH|SCAN)\s+edges\b", detail, re.I):
                    concrete.append(detail)
            if not concrete:
                offenders.append(
                    "no concrete edges plan node [" + " ".join(stmt.split())[:120] + "]"
                )
                continue
            for detail in concrete:
                why = []
                if not detail.upper().startswith("SEARCH"):
                    why.append("not-a-search")
                if re.search(r"\(repo=\?\)$", detail):
                    why.append("repo-only")
                if "src_id=?" not in detail and "dst_id=?" not in detail:
                    why.append("no-incident-id-bound")
                if why:
                    offenders.append(f"{detail} [{'+'.join(why)}]")
        self.assertFalse(
            offenders,
            "REPO-ONLY-EDGE-PLAN: " + " ; ".join(offenders),
        )

        # Equivalence leg: multiplicity and provenance under a UNION ALL
        # rewrite. The self-incident edge is matched by both the src arm and
        # the dst arm; the two same-pair rows are INCIDENT to the queried
        # item (src arm) and differ only by provenance stream. Each stored
        # row must surface exactly once, with its own source value.
        insert_edge(
            self.conn, REPO,
            ("item", "1", "mentions", "item", "1", "self mention", "keyword"),
        )
        insert_edge(
            self.conn, REPO,
            ("item", "1", "mentions", "item", "2", "pair keyword", "keyword"),
        )
        insert_edge(
            self.conn, REPO,
            ("item", "1", "mentions", "item", "2", "pair payload", "payload"),
        )
        self.conn.commit()

        cols = (
            "src_type", "src_id", "rel", "dst_type", "dst_id",
            "confidence", "evidence", "source",
        )
        expected = sorted(
            tuple(row[c] for c in cols)
            for row in self.conn.execute(
                f"SELECT {', '.join(cols)} FROM edges WHERE repo = ?", (REPO,)
            )
            if (row["src_type"] == "item" and row["src_id"] == "1")
            or (row["dst_type"] == "item" and row["dst_id"] == "1")
        )
        self.assertEqual(
            len(expected),
            6,
            f"EDGE-MULTISET-MISMATCH: fixture incident-edge count {len(expected)} != 6",
        )

        def normalize(records: list[dict]) -> list[tuple]:
            return sorted(tuple(rec[c] for c in cols) for rec in records)

        item_edges = normalize(query.item(self.conn, 1, repo=REPO)["edges"])
        self.assertEqual(
            item_edges,
            expected,
            "EDGE-MULTISET-MISMATCH: query.item edge multiset differs from the edges table",
        )
        pair_sources = sorted(
            t[7]
            for t in item_edges
            if t[:5] == ("item", "1", "mentions", "item", "2")
        )
        self.assertEqual(
            pair_sources,
            ["keyword", "payload"],
            "EDGE-MULTISET-MISMATCH: two-provenance pair not returned once per source",
        )
        related_edges = normalize(
            query.related(self.conn, 1, depth=1, repo=REPO)["edges"]
        )
        self.assertEqual(
            related_edges,
            expected,
            "EDGE-MULTISET-MISMATCH: query.related edge multiset differs from the edges table",
        )

    # --- AC6: sync runs PRAGMA optimize ------------------------------------

    def test_sync_runs_pragma_optimize(self) -> None:
        """RED at base, GREEN after the fix. sync_repo must issue PRAGMA
        optimize on its write connection before finishing; at base no such
        statement appears in the connection trace."""
        src = FakeSource()
        src.pages = [[issue(1), issue(2)]]
        traced: list[str] = []
        self.conn.set_trace_callback(traced.append)
        try:
            self.sync(src)
        finally:
            self.conn.set_trace_callback(None)
        normalized = [" ".join(s.split()).lower() for s in traced]
        self.assertTrue(
            any("pragma optimize" in s for s in normalized),
            "PRAGMA OPTIMIZE MISSING: sync_repo finished without issuing PRAGMA optimize",
        )


if __name__ == "__main__":
    unittest.main()
