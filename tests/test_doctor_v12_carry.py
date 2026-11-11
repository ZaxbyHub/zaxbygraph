"""What `doctor --consolidate` must CARRY (phase-4.5 review fixes).

Two obligations the adoption's replace-the-slug shape would otherwise drop,
because both live in tables the consolidation rewrites wholesale:

* the LISTING RECOVERY FLOOR (issue #65 AC1): a store whose last run could
  not quiesce holds an unresolved window obligation in `sync_state`. The
  candidate's row replaces it, so the fold must take the EARLIEST
  unresolved floor of the two (a NULL active floor means "from the
  beginning" and subsumes any timestamp) — dropping either side would let
  the next sync drain from the watermark and seal a window neither run
  quiesced.
* the ACTOR BOT VERDICT (issue #67): `actors.is_bot` must never
  DOWNGRADE on consolidation. `actors` is copied wholesale, so a store
  that already knows a login as a bot keeps that verdict (MAX on
  conflict), while a candidate-only actor's verdict adopts as-is.

Offline: no network, no live `gh`.
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from zaxbygraph.db import connect, init_schema  # noqa: E402

REPO = "probe/carry"
STAMP = "2026-02-01T00:00:00Z"


def _rmtree(path: Path) -> None:
    for _ in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        gc.collect()
        time.sleep(0.2)


def _child_env() -> dict:
    env = dict(os.environ)
    parts = [str(ROOT / "src")]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _parse_envelope(stdout: bytes) -> dict | None:
    text = stdout.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    try:
        return json.loads("\n".join(lines))
    except ValueError:
        return None


class _CarryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="zg-carry-"))
        self.addCleanup(_rmtree, self.scratch)

    def _db(self, name: str) -> Path:
        path = self.scratch / name / "history.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(path)
        init_schema(conn)
        conn.close()
        return path

    @staticmethod
    def _seed_item(conn: sqlite3.Connection, gid: int) -> None:
        conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, state,"
            " updated_at, raw_json) VALUES (?, ?, 1, 'issue', 'one',"
            " 'open', ?, '{}')",
            (gid, REPO, STAMP),
        )

    @staticmethod
    def _seed_floor(conn: sqlite3.Connection, floor: str | None,
                    active: int) -> None:
        # last_full_sync_at makes the candidate COMPLETE: doctor adopts the
        # freshest COMPLETE candidate, so a candidate without it is scanned
        # and skipped (the adoption never runs, and nothing is carried).
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " listing_floor, listing_floor_active) VALUES (?, ?, ?, ?, ?)",
            (REPO, STAMP, STAMP, floor, active),
        )

    @staticmethod
    def _seed_actor(conn: sqlite3.Connection, login: str, is_bot: int) -> None:
        conn.execute(
            "INSERT INTO actors(login, html_url, is_bot) VALUES (?, ?, ?)",
            (login, f"https://github.com/{login}", is_bot),
        )

    def _candidate(self, *, floor: str | None = None, active: int = 0,
                   actors: tuple[tuple[str, int], ...] = ()) -> Path:
        path = self._db("cand")
        conn = connect(path)
        self._seed_item(conn, 100)
        if floor is not None or active:
            self._seed_floor(conn, floor, active)
        else:
            conn.execute(
                "INSERT INTO sync_state(repo, issues_since, last_full_sync_at)"
                " VALUES (?, ?, ?)",
                (REPO, STAMP, STAMP),
            )
        for login, bot in actors:
            self._seed_actor(conn, login, bot)
        conn.commit()
        conn.close()
        return path

    def _store(self, *, floor: str | None = None, active: int = 0,
               actors: tuple[tuple[str, int], ...] = ()) -> Path:
        path = self._db("store")
        conn = connect(path)
        self._seed_item(conn, 1)
        if floor is not None or active:
            self._seed_floor(conn, floor, active)
        else:
            conn.execute(
                "INSERT INTO sync_state(repo, issues_since, last_full_sync_at)"
                " VALUES (?, ?, ?)",
                (REPO, STAMP, STAMP),
            )
        for login, bot in actors:
            self._seed_actor(conn, login, bot)
        conn.commit()
        conn.close()
        return path

    def _consolidate(self, store: Path, candidate: Path) -> dict:
        scan = self.scratch / "scan"
        scan.mkdir(exist_ok=True)
        shutil.copy(candidate, scan / "history.db")
        proc = subprocess.run(
            [sys.executable, "-m", "zaxbygraph", "doctor", "--repo", REPO,
             "--db", str(store), "--scan", str(scan), "--consolidate",
             "--format", "json"],
            capture_output=True, env=_child_env(), cwd=str(ROOT), timeout=180,
        )
        envelope = _parse_envelope(proc.stdout) or {}
        data = envelope.get("data") or {}
        self.assertTrue(
            data.get("adopted"),
            f"the candidate must be adopted (rc={proc.returncode}):"
            f" {envelope} stderr={proc.stderr.decode('utf-8', 'replace')[-400:]}",
        )
        return envelope

    @staticmethod
    def _floor(conn: sqlite3.Connection) -> tuple[str | None, int]:
        row = conn.execute(
            "SELECT listing_floor, listing_floor_active FROM sync_state"
            " WHERE repo = ?", (REPO,),
        ).fetchone()
        return (row["listing_floor"], row["listing_floor_active"]) if row else (None, -1)


class FloorCarryTests(_CarryCase):
    def test_earliest_unresolved_floor_of_store_and_candidate_wins(self) -> None:
        """Both sides hold an active floor: the ADOPTED one is the earlier
        window, because the next run must re-drain the union — the later
        floor alone would leave the earlier window sealed."""
        store = self._store(floor="2026-01-20T00:00:00Z", active=1)
        cand = self._candidate(floor="2026-01-10T00:00:00Z", active=1)
        self._consolidate(store, cand)
        conn = connect(store)
        try:
            floor, active = self._floor(conn)
        finally:
            conn.close()
        self.assertEqual(active, 1, "an unresolved floor stays unresolved")
        self.assertEqual(floor, "2026-01-10T00:00:00Z",
                         "the earliest window of the two must win")

    def test_store_floor_survives_a_later_candidate_floor(self) -> None:
        """The mirror case: the STORE's window is the earlier one, so the
        candidate's later floor must not replace it."""
        store = self._store(floor="2026-01-05T00:00:00Z", active=1)
        cand = self._candidate(floor="2026-01-25T00:00:00Z", active=1)
        self._consolidate(store, cand)
        conn = connect(store)
        try:
            floor, active = self._floor(conn)
        finally:
            conn.close()
        self.assertEqual(active, 1)
        self.assertEqual(floor, "2026-01-05T00:00:00Z")

    def test_null_active_floor_subsumes_a_timestamp(self) -> None:
        """A NULL floor with the flag set is a FULL run's floor: re-drain
        from the beginning, which covers any timestamp the other side
        names. Folding to the timestamp would silently drop that."""
        store = self._store(floor=None, active=1)
        cand = self._candidate(floor="2026-01-10T00:00:00Z", active=1)
        self._consolidate(store, cand)
        conn = connect(store)
        try:
            floor, active = self._floor(conn)
        finally:
            conn.close()
        self.assertEqual(active, 1)
        self.assertIsNone(floor, "from-the-beginning subsumes any window")

    def test_store_floor_survives_a_candidate_without_one(self) -> None:
        """The reviewer's loss shape, through the real CLI: the store holds
        an ACTIVE floor and the winning candidate (complete, so it is
        adopted) has none. The candidate's sync_state row REPLACES the
        store's, so without the fold the floor — an OBLIGATION, not a
        freshness stamp — would vanish and the next sync would drain from
        the watermark."""
        store = self._store(floor="2026-01-15T00:00:00Z", active=1)
        cand = self._candidate(floor=None, active=0)
        self._consolidate(store, cand)
        conn = connect(store)
        try:
            floor, active = self._floor(conn)
        finally:
            conn.close()
        self.assertEqual(active, 1,
                         "an active floor must survive an adoption whose"
                         " candidate carried none")
        self.assertEqual(floor, "2026-01-15T00:00:00Z")

    def test_inactive_floor_stays_resolved(self) -> None:
        """Neither side unresolved: nothing to carry, and no floor may be
        conjured into existence by the fold."""
        store = self._store(floor=None, active=0)
        cand = self._candidate(floor=None, active=0)
        self._consolidate(store, cand)
        conn = connect(store)
        try:
            floor, active = self._floor(conn)
        finally:
            conn.close()
        self.assertEqual(active, 0)
        self.assertIsNone(floor)


class ActorBotCarryTests(_CarryCase):
    def test_consolidation_never_downgrades_a_known_bot(self) -> None:
        """The store knows `copilot` as a bot; the candidate (whose
        payload never carried the signal) has it as a human. The store's
        verdict stands — a downgrade would silently un-mark every bot an
        earlier sync classified."""
        store = self._store(actors=(("copilot", 1), ("alice", 0)))
        cand = self._candidate(actors=(("copilot", 0), ("alice", 0)))
        self._consolidate(store, cand)
        conn = connect(store)
        try:
            got = {
                str(r[0]): r[1]
                for r in conn.execute("SELECT login, is_bot FROM actors")
            }
        finally:
            conn.close()
        self.assertEqual(got.get("copilot"), 1,
                         "a known bot must never be downgraded by adoption")
        self.assertEqual(got.get("alice"), 0)

    def test_candidate_bot_verdict_adopts(self) -> None:
        """A candidate-only actor the candidate classified as a bot adopts
        its verdict (the copy is wholesale, so this is the only chance to
        record it)."""
        store = self._store(actors=(("alice", 0),))
        cand = self._candidate(actors=(("alice", 0), ("graphbot", 1)))
        self._consolidate(store, cand)
        conn = connect(store)
        try:
            got = {
                str(r[0]): r[1]
                for r in conn.execute("SELECT login, is_bot FROM actors")
            }
        finally:
            conn.close()
        self.assertEqual(got.get("graphbot"), 1,
                         "the candidate's own bot verdict must adopt")
        self.assertEqual(got.get("alice"), 0)


class PendingSetCarryTests(_CarryCase):
    def test_consolidation_carries_the_pending_retry_set(self) -> None:
        """`sync_pending` rows are OBLIGATIONS, not evidence: consolidation
        must copy them, or an adopted corpus forgets that the next sync
        still owes those numbers a retry (the drift guard's rule — a table
        keyed `(repo, number)` is item-scoped and is copied — plus the
        plan's Cluster 7 "copied by doctor with its repo scope"). Both
        reasons and the recorded `computed_at` survive."""
        store = self._store()
        cand = self._db("cand")
        conn = connect(cand)
        self._seed_item(conn, 100)
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at)"
            " VALUES (?, ?, ?)",
            (REPO, STAMP, STAMP),
        )
        conn.execute(
            "INSERT INTO sync_pending(repo, number, reason, computed_at)"
            " VALUES (?, ?, ?, ?)",
            (REPO, 3, "deletion", STAMP),
        )
        conn.execute(
            "INSERT INTO sync_pending(repo, number, reason, computed_at)"
            " VALUES (?, ?, ?, ?)",
            (REPO, 4, "torn", STAMP),
        )
        conn.commit()
        conn.close()
        self._consolidate(store, cand)
        conn = connect(store)
        try:
            rows = sorted(
                (int(r[0]), str(r[1]), str(r[2]))
                for r in conn.execute(
                    "SELECT number, reason, computed_at FROM sync_pending"
                    " WHERE repo = ?",
                    (REPO,),
                )
            )
        finally:
            conn.close()
        self.assertEqual(
            rows,
            [(3, "deletion", STAMP), (4, "torn", STAMP)],
            "an adopted corpus keeps the retry debts it owes, with their"
            " reasons and the time they were recorded",
        )


if __name__ == "__main__":
    unittest.main()
