"""AGENTS.md count-freshness invariant check (issue #36 AC4).

New file for the 40-48-36-followups trace. 36-AC4 is frozen in executable
form (check C12): the acceptance-table format allows at most one
DOCS_ONLY row per table, and this criterion IS mechanically checkable as
a repo-file text scan (the sibling precedent is the 31 trace's ci.yml /
CONTRIBUTING text-scan checks). Wording of the bullet stays free; the
content facts are pinned: a bullet in AGENTS.md's invariants names count
freshness (finish-time snapshot, may lag after an interrupted run) with a
docs/schema.md cross-reference.
"""

from __future__ import annotations

import unittest
from pathlib import Path

AGENTS_MD = Path(__file__).resolve().parents[1] / "AGENTS.md"


class AgentsCountFreshnessTests(unittest.TestCase):
    def test_agents_md_states_count_freshness(self) -> None:
        """RED at base, GREEN after the fix (36-AC4). sync_state counts
        are finish-time snapshots exact after every completed run and may
        lag after an interrupted run; AGENTS.md's invariants must say so,
        cross-referencing docs/schema.md. At base AGENTS.md never mentions
        counts (grep-verified; issue #36 claim 4), so `status.item_count`
        reads as corpus truth to an agent reading AGENTS.md alone."""
        text = AGENTS_MD.read_text(encoding="utf-8")
        bullets = [
            ln.strip()
            for ln in text.splitlines()
            if ln.lstrip().startswith(("-", "*"))
        ]
        hits = [b for b in bullets if "count" in b.lower()]
        self.assertTrue(
            hits,
            "AGENTS-COUNT-FRESHNESS-MISSING: AGENTS.md carries no bullet"
            " mentioning counts",
        )
        ok = any(
            ("finish" in b.lower() or "sync" in b.lower())
            and (
                "interrupt" in b.lower()
                or "lag" in b.lower()
                or "stale" in b.lower()
                or "snapshot" in b.lower()
            )
            and "docs/schema.md" in b.lower()
            for b in hits
        )
        self.assertTrue(
            ok,
            "AGENTS-COUNT-FRESHNESS-MISSING: no counts bullet names"
            " finish-time snapshots and the interrupted-run lag with a"
            " docs/schema.md cross-reference",
        )


if __name__ == "__main__":
    unittest.main()
