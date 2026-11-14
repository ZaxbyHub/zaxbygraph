"""Regenerate tests/eval_fixture_backtest/events.jsonl - the NEIGHBORHOOD
subcorpus recorded from the real user-level ZaxbyHub/zaxbygraph store for
the C17 acceptance gate (issues #68/#69 backtest).

Neighborhood rule (see README.md in this directory):

  1. fix PRs     - every MERGED PR whose closes UNION closes_keyword edges
                   close >= 2 distinct issues;
  2. closed      - every issue closed by one of those PRs (either stream);
  3. open pool   - every issue OPEN at some fix PR's creation (kind='issue',
                   created_at <= that PR's created_at, and either still open
                   now or closed at/after that PR's creation);
  4. children    - those items' comments/reviews/pr_files ride the recorded
                   item bundles (the tests/fixtures_eval._bundle_for_item
                   machinery); actors repopulate from the payloads. Fix PRs
                   additionally carry their stored pr_commits section so the
                   commit-message closes_keyword stream replays through the
                   real ingest path (PR 49's #32 link exists ONLY as a
                   timeline closes edge plus a commit-message keyword edge;
                   the commit half must survive the recording).
  5. revisions   - the neighborhood items' revisions rows (the store's own
                   text pre-images: item_title/item_body/comment/review
                   displacement history) are recorded VERBATIM as one extra
                   {"resource": "revisions"} record (amendment wave 3).
                   Sync alone cannot replay them - the ingest path writes a
                   pre-image only when it OBSERVES a change between fetches
                   - so tests/fixtures_backtest.py's BacktestReplaySource
                   collects the record and replay_backtest_store() re-inserts
                   the rows after the sync pass. The SQLite-assigned id is
                   excluded (export.py convention); content is scrubbed with
                   the same _scrub_markers normalization as every other
                   recorded text.

Run from the repository root:

    python tests/eval_fixture_backtest/record_backtest_corpus.py [STORE]

Without an argument the user-level store for the origin repo is used. The
store is opened read-only (open_existing, never connect()).

Disclosed unrecoverable families (the README carries the full list):
timeline-sourced closes edges and every link-stream edge (duplicate_of,
blocked_by, parent, open_prs) are not re-derivable from item bundles, so a
PR whose multi-issue closure is timeline-only would drop out of the
neighborhood on replay - none exists today (PR 49's #32 link is recovered
through pr_commits), and the pinned numbers in
tests/test_selection_quality.py are computed FROM THE REPLAYED STORE, so
they stay honest by construction.
"""

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from fixtures_eval import _bundle_for_item, _scrub_markers  # noqa: E402
from zaxbygraph.db import open_existing  # noqa: E402

REPO = "zaxbyhub/zaxbygraph"


def _neighborhood(conn) -> tuple[dict[int, set[int]], set[int], set[int], set[int]]:
    """(fix_pr_closed_map, fix_prs, closed_issues, open_pool)."""
    # created_at per merged PR with any closing edge (closes OR closes_keyword)
    fix_created: dict[int, str] = {}
    closed_map: dict[int, set[int]] = {}
    for row in conn.execute(
        "SELECT DISTINCT e.src_id, i.created_at FROM edges e"
        " JOIN items i ON i.repo = e.repo AND i.number = CAST(e.src_id AS INTEGER)"
        " WHERE e.rel IN ('closes', 'closes_keyword')"
        " AND i.kind = 'pr' AND i.merged_at IS NOT NULL",
    ):
        fix_created[int(row["src_id"])] = row["created_at"]
    for row in conn.execute(
        "SELECT src_id, dst_id FROM edges WHERE rel IN ('closes', 'closes_keyword')"
    ):
        pr = int(row["src_id"])
        if pr in fix_created:
            closed_map.setdefault(pr, set()).add(int(row["dst_id"]))
    fix_prs = {pr for pr, s in closed_map.items() if len(s) >= 2}
    closed_issues: set[int] = set()
    for pr in fix_prs:
        closed_issues |= closed_map[pr]
    open_pool: set[int] = set()
    for pr in sorted(fix_prs):
        created = fix_created[pr]
        for row in conn.execute(
            "SELECT number FROM items WHERE kind = 'issue'"
            " AND created_at <= ?"
            " AND (state = 'open' OR (closed_at IS NOT NULL AND closed_at >= ?))",
            (created, created),
        ):
            open_pool.add(int(row["number"]))
    return (
        {pr: closed_map[pr] for pr in fix_prs},
        fix_prs,
        closed_issues,
        open_pool,
    )


def main() -> int:
    store_arg = sys.argv[1] if len(sys.argv) > 1 else None
    if store_arg:
        conn = open_existing(Path(store_arg))
    else:
        candidate = (
            Path.home() / "AppData" / "Local" / "zaxbygraph" / "github.com"
            / "zaxbyhub" / "zaxbygraph" / "history.db"
        )
        if not candidate.exists():
            print(f"no user-level store found at {candidate}; pass a store path",
                  file=sys.stderr)
            return 2
        conn = open_existing(candidate)

    out_dir = Path(__file__).resolve().parent
    try:
        closed_map, fix_prs, closed_issues, open_pool = _neighborhood(conn)
        neighborhood = sorted(fix_prs | closed_issues | open_pool)

        lines: list[str] = []
        for number in neighborhood:
            payload = _bundle_for_item(conn, REPO, number)
            if payload is None:
                continue
            if number in fix_prs:
                # The commit-message closes_keyword stream: record the stored
                # pr_commits rows as a complete section so replay re-derives
                # commit-evidence keyword edges through the real ingest path.
                commits = [
                    {
                        "sha": row["sha"],
                        "message": _scrub_markers(row["message"] or ""),
                    }
                    for row in conn.execute(
                        "SELECT sha, message FROM pr_commits"
                        " WHERE repo = ? AND number = ? ORDER BY sha",
                        (REPO, number),
                    )
                ]
                payload["pr_commits"] = commits
                payload["children_complete"]["pr_commits"] = True
            lines.append(
                json.dumps(
                    {"resource": "item_bundle", "payload": payload},
                    sort_keys=True,
                    ensure_ascii=True,
                )
            )

        # The revisions recording (amendment wave 3): the neighborhood
        # items' text pre-images, VERBATIM (content scrubbed like every
        # other recorded text; the SQLite-assigned id excluded). One extra
        # record at the end of the sidecar; the base JsonlReplaySource
        # ignores unknown resources, and tests/fixtures_backtest.py's
        # BacktestReplaySource collects this one. The sort key covers the
        # whole row so the recording is byte-deterministic.
        revision_rows: list[list] = []
        item_bundle_lines = len(lines)
        for row in conn.execute(
            "SELECT number, entity, entity_id, kind, origin, content,"
            " valid_from, valid_to FROM revisions"
            " WHERE repo = ? AND number IN"
            f" ({', '.join('?' * len(neighborhood))})"
            " ORDER BY number, entity, entity_id, kind, origin,"
            " valid_from, valid_to, content",
            (REPO, *neighborhood),
        ):
            revision_rows.append([
                int(row["number"]),
                row["entity"],
                row["entity_id"],
                row["kind"],
                row["origin"],
                _scrub_markers(row["content"] or ""),
                row["valid_from"],
                row["valid_to"],
            ])
        revisions_by_entity: dict[str, int] = {}
        for r in revision_rows:
            revisions_by_entity[r[1]] = revisions_by_entity.get(r[1], 0) + 1
        lines.append(
            json.dumps(
                {"resource": "revisions",
                 "payload": {"rows": revision_rows}},
                sort_keys=True, ensure_ascii=True,
            )
        )

        (out_dir / "events.jsonl").write_text(
            "\n".join(lines) + ("\n" if lines else ""),
            encoding="utf-8", newline="\n",
        )
        digest = hashlib.sha256(
            (out_dir / "events.jsonl").read_bytes()
        ).hexdigest()
        manifest = {
            "repo": REPO,
            "recorded_items": item_bundle_lines,
            "fix_prs": {str(pr): sorted(closed_map[pr]) for pr in sorted(fix_prs)},
            "closed_issues": sorted(closed_issues),
            "open_pool": sorted(open_pool),
            "neighborhood": neighborhood,
            "revisions_rows": len(revision_rows),
            "revisions_by_entity": dict(sorted(revisions_by_entity.items())),
            "events_sha256": digest,
        }
        (out_dir / "recording_manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, ensure_ascii=True, indent=1)
            + "\n",
            encoding="utf-8", newline="\n",
        )
        print(
            f"recorded {item_bundle_lines} items ({len(fix_prs)} fix PRs,"
            f" {len(closed_issues)} closed issues, pool {len(open_pool)})"
            f" + {len(revision_rows)} revisions rows"
            f" ({revisions_by_entity}), events sha256 {digest}"
        )
        print(
            "re-pin the GT numbers in tests/test_selection_quality.py C17 after"
            " replaying and verifying"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
