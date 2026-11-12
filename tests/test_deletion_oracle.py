"""The PRODUCTION deletion oracle's corroboration truth table (issue #66
AC12), driven through the real `GraphQLSource` — never a test double.

The phase-4.5 review found the plan's Cluster 7 contract implemented only in
`tests/test_sync_verdicts.py`'s fake: `GraphQLSource.check_deleted` was still
the base one-argument method, so a real all-gone chunk raised the #66 error
in production while the frozen check passed against the double. These tests
pin the shipped class itself, plus sync's adapter actually handing it the
witness (the seam that was missing).

Offline: the source's `_graphql` and `probe_repository` are stubbed; no
network, no live `gh`.
"""
from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from fixtures import REPO, FakeGitHubSource, TempDBTest, issue, ts
from zaxbygraph.github import GitHubError
from zaxbygraph.graphql import GraphQLSource
from zaxbygraph.sync import _deletion_pass, sync_repo


def _alias_reader(query: str):
    """The (alias, number, kind) triples a check_deleted query asks for."""
    out = []
    for m in re.finditer(r"(\w+): issue\(number: (\d+)\)", query):
        out.append((m.group(1), int(m.group(2)), "issue"))
    for m in re.finditer(r"(\w+): pullRequest\(number: (\d+)\)", query):
        out.append((m.group(1), int(m.group(2)), "pull"))
    return out


class _OracleProbeSource(GraphQLSource):
    """The real class with its transport stubbed: `_graphql` answers from a
    scripted set of LIVE numbers, `probe_repository` from a flag, and every
    query it is asked is recorded."""

    def __init__(self, live: set[int], probe_ok: bool = True) -> None:
        super().__init__("acme", "forgegate", gh_bin="gh")
        self.live = set(live)
        self.probe_ok = probe_ok
        self.queries: list[str] = []
        self.probe_calls = 0

    def _graphql(self, query, aliases=None):
        self.queries.append(query)
        repo = {}
        for alias, number, _kind in _alias_reader(query):
            repo[alias] = {"number": number} if number in self.live else None
        # _graphql answers at the DATA level (the same shape the production
        # callers read: payload.get("repository")).
        return {
            "repository": repo,
            "rateLimit": {"remaining": 4000, "resetAt": "2026-10-03T13:00:00Z"},
        }

    def probe_repository(self) -> bool:
        self.probe_calls += 1
        if not self.probe_ok:
            return False
        return True


class RealOracleTruthTableTests(unittest.TestCase):
    """The four rows of the plan's Cluster-7 table, on the shipped class."""

    def test_no_witness_all_gone_keeps_the_base_refusal(self) -> None:
        """A one-argument call (or corroborator=None) is EXACTLY the base
        contract: a chunk that resolves nothing raises, because an all-null
        answer cannot be told apart from a repo-wide visibility change."""
        src = _OracleProbeSource(live=set())
        with self.assertRaises(GitHubError) as ctx:
            src.check_deleted([2, 3])
        self.assertIn(
            "deletion probe resolved no nodes for a whole chunk",
            str(ctx.exception),
        )
        self.assertEqual(src.probe_calls, 0,
                         "the legacy shape never consults the repository")

    def test_live_witness_corroborates_an_all_gone_chunk(self) -> None:
        """A witness that resolves vouches for the chunk: the candidates are
        genuinely gone, so they are marked — and the witness itself never
        becomes a verdict."""
        src = _OracleProbeSource(live={1})
        verdicts = src.check_deleted([2, 3], corroborator=lambda: 1)
        self.assertEqual(verdicts, {2: "deleted", 3: "deleted"},
                         "a corroborated all-gone chunk marks its candidates")
        self.assertNotIn(1, verdicts, "the live witness is never a verdict")
        self.assertTrue(
            any("iw1:" in q and "pw1:" in q for q in src.queries),
            "the witness must ride the SAME query, so a degraded response"
            " cannot masquerade as a dead witness",
        )
        self.assertEqual(src.probe_calls, 0,
                         "a resolving witness needs no repository read")

    def test_dead_witness_raises(self) -> None:
        """A witness that resolves NOTHING is the same evidentiary class as
        the base refusal — deleted since the listing, or a response that
        cannot be trusted — so the run refuses to mass-mark."""
        src = _OracleProbeSource(live=set())
        with self.assertRaises(GitHubError) as ctx:
            src.check_deleted([2, 3], corroborator=lambda: 1)
        self.assertIn("witness resolved no nodes", str(ctx.exception))
        self.assertEqual(src.probe_calls, 0,
                         "a dead witness raises; it is not a probe trigger")

    def test_no_witness_readable_repository_marks(self) -> None:
        """The all-gone window (no listed item left to vouch): ONE
        independent repository read decides — readable marks."""
        src = _OracleProbeSource(live=set(), probe_ok=True)
        verdicts = src.check_deleted([2, 3], corroborator=lambda: None)
        self.assertEqual(verdicts, {2: "deleted", 3: "deleted"})
        self.assertEqual(src.probe_calls, 1,
                         "exactly ONE repository read backs the verdicts")

    def test_no_witness_unreadable_repository_raises(self) -> None:
        """Unreadable ⇒ raise: an access failure must never fabricate a
        deletion."""
        src = _OracleProbeSource(live=set(), probe_ok=False)
        with self.assertRaises(GitHubError):
            src.check_deleted([2, 3], corroborator=lambda: None)
        self.assertEqual(src.probe_calls, 1)

    def test_partial_resolution_marks_only_the_missing(self) -> None:
        """A chunk that resolves SOME nodes needs no witness: the
        non-resolving members are the verdicts."""
        src = _OracleProbeSource(live={2})
        verdicts = src.check_deleted([2, 3], corroborator=lambda: None)
        self.assertEqual(verdicts, {3: "deleted"})
        self.assertEqual(src.probe_calls, 0,
                         "a partially-resolved chunk is not an all-gone one")

    def test_chunking_stays_at_the_documented_page(self) -> None:
        """The oracle owns chunking at GRAPHQL_DELETED_PAGE, so one query
        never carries more than the documented alias budget."""
        from zaxbygraph.graphql import GRAPHQL_DELETED_PAGE

        live = set(range(1, 4))
        src = _OracleProbeSource(live=live)
        src.check_deleted(list(range(1, GRAPHQL_DELETED_PAGE + 5)),
                          corroborator=lambda: 1)
        self.assertEqual(
            len(src.queries), 2,
            f"{GRAPHQL_DELETED_PAGE + 5} numbers must be two queries,"
            f" got {len(src.queries)}",
        )


class SyncOffersTheWitnessTests(TempDBTest):
    """The seam the review found missing: sync's signature adapter must
    hand the corroborator to the REAL oracle (which now accepts it), so the
    truth table runs in production rather than only in a double."""

    def test_deletion_pass_drives_the_real_oracle_with_a_witness(self) -> None:
        # Three stored items; the listing delivered 1, so 1 is the live
        # witness and 2/3 are the all-gone candidates.
        for n in (1, 2, 3):
            self.conn.execute(
                "INSERT INTO items(id, repo, number, kind, title, state,"
                " updated_at, raw_json) VALUES (?, ?, ?, 'issue', ?, 'open',"
                " ?, '{}')",
                (10_000 + n, REPO, n, f"item {n}", ts(n * 10)),
            )
        self.conn.commit()
        src = _OracleProbeSource(live={1})
        _deletion_pass(self.conn, src, REPO, seen_numbers={1})
        states = {
            int(r[0]): r[1]
            for r in self.conn.execute(
                "SELECT number, state FROM items WHERE repo = ?", (REPO,)
            )
        }
        self.assertEqual(states, {1: "open", 2: "deleted", 3: "deleted"},
                         "the real oracle must mark the corroborated chunk")
        self.assertTrue(
            any("iw1:" in q for q in src.queries),
            "sync must offer the witness to the REAL oracle — a one-argument"
            " adapter call would leave the all-gone chunk unresolvable",
        )
        self.assertEqual(src.probe_calls, 0,
                         "a live witness means no repository read was needed")

    def test_deletion_pass_retry_shape_never_derives_from_absence(self) -> None:
        """`derive_new=False` (the later-run retry) probes exactly the
        pending set: an incremental window's silence never fabricates a
        deletion for an item the listing simply did not mention."""
        for n in (1, 2):
            self.conn.execute(
                "INSERT INTO items(id, repo, number, kind, title, state,"
                " updated_at, raw_json) VALUES (?, ?, ?, 'issue', ?, 'open',"
                " ?, '{}')",
                (10_000 + n, REPO, n, f"item {n}", ts(n * 10)),
            )
        self.conn.execute(
            "INSERT INTO sync_pending(repo, number, reason) VALUES (?, ?, 'deletion')",
            (REPO, 2),
        )
        self.conn.commit()
        # Item 1 is live but NOT pending (so it must never be probed);
        # item 2 is pending and gone from the source, so the retry is what
        # resolves it. No seen item exists, so the corroborator offers no
        # witness and the repository read decides.
        src = _OracleProbeSource(live={1})
        _deletion_pass(self.conn, src, REPO, seen_numbers=set(),
                       derive_new=False)
        states = {
            int(r[0]): r[1]
            for r in self.conn.execute(
                "SELECT number, state FROM items WHERE repo = ?", (REPO,)
            )
        }
        self.assertEqual(states[1], "open",
                         "an item outside the pending set is never probed")
        self.assertEqual(states[2], "deleted",
                         "the pending number is the one the retry resolves")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM sync_pending WHERE repo = ?", (REPO,)
            ).fetchone()[0],
            0, "a completed retry discharges the obligation",
        )
        asked = [
            int(m.group(1))
            for q in src.queries
            for m in re.finditer(r"i(\d+): issue\(number: \d+\)", q)
        ]
        self.assertEqual(sorted(set(asked)), [2],
                         "the retry probes ONLY the pending number")


class _WindowedDeletionSource(FakeGitHubSource):
    """A listing that honours `since` plus an oracle that reports every
    queried number gone — the shape that would fabricate deletions out of
    an incremental window's silence if candidates were re-derived from it."""

    def check_deleted(self, numbers) -> dict[int, str]:
        return {int(n): "deleted" for n in numbers}


class PlainRunRetryShapeTests(TempDBTest):
    def test_plain_run_never_derives_candidates_from_window_absence(self) -> None:
        """The CALL SITE's contract (the seam a direct `_deletion_pass`
        call cannot reach): a non-full run with an open deletion pass
        re-runs the oracle over exactly the PENDING set. An item that is
        merely outside the incremental window is "not updated", never
        "gone" — a mutant that re-derives candidates from absence marks it
        deleted on the strength of silence alone."""
        src = _WindowedDeletionSource()
        src.add_issue(issue(1, updated_at=ts(200)))  # inside the window
        src.add_issue(issue(2, updated_at=ts(10)))   # older than the watermark
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, state,"
            " updated_at, raw_json) VALUES"
            " (1, ?, 1, 'issue', 'in window', 'open', ?, '{}'),"
            " (2, ?, 2, 'issue', 'outside window', 'open', ?, '{}')",
            (REPO, ts(200), REPO, ts(10)),
        )
        # The watermark sits between the two: the listing delivers only 1.
        self.conn.execute(
            "INSERT INTO sync_state(repo, issues_since) VALUES (?, ?)",
            (REPO, ts(150)),
        )
        self.conn.execute(
            "INSERT INTO sync_pending(repo, number, reason) VALUES (?, ?, 'deletion')",
            (REPO, 3),
        )
        self.conn.commit()

        result = sync_repo(self.conn, src, REPO)
        self.assertIsNone(result["last_error"])
        states = {
            int(r[0]): r[1]
            for r in self.conn.execute(
                "SELECT number, state FROM items WHERE repo = ?", (REPO,)
            )
        }
        self.assertEqual(
            states.get(2), "open",
            "an item outside the incremental window is never a deletion"
            " candidate — silence is not evidence of absence",
        )
        self.assertEqual(states.get(1), "open",
                         "the delivered item is still live")
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM sync_pending WHERE repo = ?"
                       " AND reason = 'deletion'", (REPO,)),
            0, "the retry discharged the one obligation it owed",
        )


if __name__ == "__main__":
    unittest.main()
