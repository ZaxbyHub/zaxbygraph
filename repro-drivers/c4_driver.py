"""C4 (40-AC4, NEW-SURFACE) guard driver: the doctor --consolidate result
payload must disclose what adoption did to the slug's revision history.

Frozen contract (issue #40 AC4): ``doctor(..., consolidate_flag=True)``'s
returned result payload carries integer counts ``revisions_preserved``
(stored revision rows for slug items the adopted candidate holds NO
revision rows for, kept in place) and ``revisions_adopted`` (candidate
revision rows copied in, fresh ids). The counts live at the top level of
the doctor report envelope, beside ``adopted``/``scanned`` (doctor.py's
documented "Report payload").

At the frozen base the payload carries no such fields (verified: the
result dict has exactly ``ok``/``adopted``/``scanned``), so this driver
prints ``REPORT-FIELD-MISSING`` and exits nonzero - the base-leg signature
for the C4 NEW-SURFACE row. Only when the guard passes does the C4 leg run
the companion unittest ``test_adoption_reports_preserved_revision_counts``
(tests/test_consolidation_revisions.py, authored by the fix).

Isolated-mode safe: run as ``python -I repro-drivers/c4_driver.py`` from
the repo root; ``-I`` ignores PYTHONPATH, so imports resolve through the
explicit src/ insert below.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zaxbygraph.db import connect, init_schema  # noqa: E402
from zaxbygraph.doctor import doctor  # noqa: E402

REPO = "acme/forgegate"

#: The three slug items both databases hold (adoption needs a non-empty
#: candidate corpus; the exact titles are irrelevant to the check).
_ITEMS = [
    (1000 + n, REPO, n, "issue", f"item {n}", "open", "{}") for n in (1, 2, 3)
]


def _build(path: Path, revisions: list[tuple], complete: bool) -> None:
    conn = connect(path)
    init_schema(conn)
    conn.executemany(
        "INSERT INTO items(id, repo, number, kind, title, state, raw_json)"
        " VALUES (?,?,?,?,?,?,?)",
        _ITEMS,
    )
    if revisions:
        conn.executemany(
            "INSERT INTO revisions(repo, number, entity, origin, content,"
            " valid_from, valid_to) VALUES (?, ?, 'item_body', 'edit', ?, ?, ?)",
            revisions,
        )
    if complete:
        # inspect() requires last_full_sync_at NOT NULL and
        # full_sync_pending = 0, plus an issues_since row, for adoption.
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
            " VALUES (?,?,?,?,?,?,?)",
            (REPO, "2026-02-03T00:00:00Z", "2026-02-03T00:00:00Z", None, None, 0, 0),
        )
    conn.commit()
    conn.close()


def main() -> int:
    td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        store_db = Path(td.name) / "store.db"
        cand_db = Path(td.name) / "cand.db"
        # Store: revision history for item 2 (2 rows, observed by the
        # candidate) AND item 3 (1 row, never observed by the candidate).
        _build(
            store_db,
            [
                (REPO, 2, "S2-A", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
                (REPO, 2, "S2-B", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"),
                (REPO, 3, "S3-A", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
            ],
            complete=False,
        )
        # Candidate: revision rows for item 2 only, and complete sync state.
        _build(
            cand_db,
            [
                (REPO, 2, "C2-A", "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z"),
                (REPO, 2, "C2-B", "2026-02-02T00:00:00Z", "2026-02-03T00:00:00Z"),
            ],
            complete=True,
        )

        result = doctor(None, REPO, store_db, extra_scans=[cand_db], consolidate_flag=True)
        if result.get("adopted") is None:
            print("REPORT-FIELD-MISSING: doctor adopted no candidate (fixture broken)")
            raise AssertionError(f"no adoption happened: {result!r}")

        missing = [
            key
            for key in ("revisions_preserved", "revisions_adopted")
            if not isinstance(result.get(key), int) or isinstance(result.get(key), bool)
        ]
        if missing:
            print(
                "REPORT-FIELD-MISSING: doctor result payload lacks "
                + ", ".join(missing)
                + f" (top-level keys: {sorted(result)})"
            )
            raise AssertionError(
                "doctor result payload lacks revision disclosure fields "
                f"{missing}: {sorted(result)}"
            )

        preserved = result["revisions_preserved"]
        adopted = result["revisions_adopted"]
        if (preserved, adopted) != (1, 2):
            raise AssertionError(
                "doctor result payload reports the wrong revision counts: "
                f"revisions_preserved={preserved} (want 1: item 3's unobserved"
                f" row), revisions_adopted={adopted} (want 2: item 2's rows)"
            )
        print(f"REPORT-FIELDS-OK revisions_preserved={preserved} revisions_adopted={adopted}")
        return 0
    finally:
        shutil.rmtree(td.name, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
