"""Fixture-side replay mapping for the C17 recorded backtest corpus
(tests/eval_fixture_backtest/, issues #68/#69).

The base ``tests/replay.py::JsonlReplaySource`` maps JSONL records to store
tables through the REAL sync path: ``item_bundle`` records re-ingest as
items + children (comments/reviews/pr_files ride the bundle sections), and
that is exactly how the neighborhood corpus replays. It has NO mapping for
the ``revisions`` table - and none is possible through sync alone: the
ingest path writes a pre-image row only when it OBSERVES a text change
between two fetches, and a recorded neighborhood cannot re-observe the
original edit sequence (the recording holds the displaced texts, not the
fetch history that displaced them).

Amendment wave 3 therefore extends the replay on the FIXTURE side (this
module; ``tests/fixtures_eval.py`` and ``tests/replay.py`` are frozen by
earlier traces and are not modified):

- The recorder (``tests/eval_fixture_backtest/record_backtest_corpus.py``)
  SELECTs the recorded items' ``revisions`` rows verbatim into ONE extra
  ``{"resource": "revisions", "payload": {"rows": [...]}}`` record at the
  end of ``events.jsonl``. The base JsonlReplaySource IGNORES unknown
  resources, so the older whole-file replay semantics are unchanged.
- ``BacktestReplaySource`` (below) additionally parses that record, and
  ``replay_backtest_store`` re-inserts the rows into the replayed store
  after the sync pass. The rows are the source store's own pre-images -
  entity/entity_id/kind/origin/content/valid_from/valid_to byte-identical
  (content scrubbed with the same ``_scrub_markers`` normalization the
  recording applies everywhere) - so the replayed store carries exactly
  the recorded text history and ``as_of`` text features (the revision
  pre-image whose ``valid_from <= stamp < valid_to``) resolve identically.

Disclosed limitation (README carries the full list): pre-images exist only
for edits the source store's sync OBSERVED. Coverage on this corpus is 3
of the 22 truth endpoints (61, 62, 72); the per-case disclose+exclude rule
that follows from incomplete coverage is pinned in the CHECK
(tests/test_selection_quality.py C17), not in this fixture.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from replay import JsonlReplaySource
from zaxbygraph.sync import sync_repo

#: Column order of a recorded revisions row (matches the recorder's SELECT
#: and the INSERT below; the SQLite-assigned ``id`` is excluded, as in
#: export.py - insertion order is not identity for revision rows).
_REVISION_COLUMNS = (
    "number", "entity", "entity_id", "kind", "origin",
    "content", "valid_from", "valid_to",
)


def _read_revisions(events_path: Path) -> list[tuple]:
    """The recorded ``revisions`` rows, in recorded order."""
    rows: list[tuple] = []
    with Path(events_path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("resource") != "revisions":
                continue
            payload = record.get("payload") or {}
            for row in payload.get("rows", []):
                # tolerate short rows fail-closed: a revision without its
                # window columns must never reach the store
                if len(row) != len(_REVISION_COLUMNS):
                    raise ValueError(
                        f"recorded revisions row has {len(row)} fields,"
                        f" expected {len(_REVISION_COLUMNS)}"
                        f" ({', '.join(_REVISION_COLUMNS)})"
                    )
                rows.append(tuple(row))
    return rows


class BacktestReplaySource(JsonlReplaySource):
    """JsonlReplaySource + the recorded ``revisions`` record.

    The item_bundle/release handling is entirely inherited; this subclass
    only collects the revisions rows the base source would ignore.
    """

    def __init__(self, events_path: Path | str) -> None:
        super().__init__(events_path)
        self.recorded_revisions: list[tuple] = _read_revisions(
            Path(events_path)
        )


def replay_backtest_store(
    conn: sqlite3.Connection, events_path: Path | str, repo: str
) -> int:
    """Replay the recorded backtest corpus into ``conn`` and apply the
    recorded revisions rows; returns the number of revision rows applied.

    One call = one deterministic store: the sync pass re-ingests the
    recorded bundles through the real ingest path, then the revisions rows
    are inserted in recorded order (the table has no unique key beyond its
    SQLite-assigned id, so a fixed row order is a fixed store).
    """
    source = BacktestReplaySource(events_path)
    sync_repo(conn, source, repo)
    applied = 0
    for row in source.recorded_revisions:
        conn.execute(
            "INSERT INTO revisions(repo, number, entity, entity_id, kind,"
            " origin, content, valid_from, valid_to)"
            f" VALUES (?, {', '.join('?' * len(_REVISION_COLUMNS))})",
            (repo, *row),
        )
        applied += 1
    conn.commit()
    return applied
