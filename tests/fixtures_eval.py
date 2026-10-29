"""Fixtures for the derived read tier (issue #29) — deliberately SEPARATE
from tests/fixtures.py, whose blob is anchored by other traces' checkpoints
(new fixture modules must never extend that file).

Two corpora live here:

1. ``build_eval_store`` — a deterministic SYNTHETIC store built through the
   real sync/ingest path (FakeGitHubSource), then supplemented with
   GitHub-reported-shaped edges (duplicate_of / closes) exactly where GitHub
   would have reported them. Its geometry is the hand-derivation source for
   the synthetic floors asserted in tests/test_similar.py (the derivation
   table lives in that module's docstring).

2. ``record_real_corpus`` — synthesizes a REPLAYABLE ``events.jsonl``
   (item_bundle + release records, the shape tests/replay.py's
   JsonlReplaySource consumes) from an existing store, plus a canonical
   fidelity manifest. Bundle keys are restricted to what replay actually
   consumes: the item payload (labels ride inside it), the pull payload,
   and the sections replay serves (issue_comments, review_comments,
   reviews, files). ``item`` and ``pull`` both carry the stored merged
   ``raw_json`` verbatim: ingest re-applies the same pull fields it merged
   at sync time, and its own list-owns-identity guard protects
   ``id``/``updated_at``. Path references and signatures are NOT bundle
   sections — ingest regenerates them from text; the fidelity manifest
   compares them as targets. Children_complete is not recoverable from a
   stored row (it is never stored), so sections the store still carries are
   marked complete=True and absent sections False (fail closed, never
   fabricating facts). Timeline and link streams beyond what the tables
   retained are deliberately unrecoverable and disclosed here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from fixtures import (
    REPO,
    FakeGitHubSource,
    issue,
    pr_file,
    pull,
)
from zaxbygraph.sync import sync_repo


def _insert_edge(
    conn: sqlite3.Connection,
    repo: str,
    src: int,
    rel: str,
    dst: int,
    source: str = "link",
    evidence: str = "fixture-reported",
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO edges (repo, src_type, src_id, rel, dst_type,"
        " dst_id, source, evidence, confidence)"
        " VALUES (?, 'item', ?, ?, 'item', ?, ?, ?, 'EXTRACTED')",
        (repo, str(src), rel, str(dst), source, evidence),
    )


def build_eval_store(conn: sqlite3.Connection) -> FakeGitHubSource:
    """The synthetic eval geometry. Timeline (T0 = 2026-06-01):

    - PR 10 merged T0+1d: src/feature.py, src/hub.py   (historical vocab)
    - PR 11 merged T0+2d: src/feature.py, src/feature_test.py
    - PR 12 merged T0+3d: src/hub.py                    (hub file counter)
    - issue 101 created T0+4d: login crash, refs src/feature.py (body)
      - comment (T0+5d) refs src/feature.py  -> origin issue_comment, visible
        (the plan's review-comment fixture bait is deliberately NOT here:
        FakeGitHubSource's comment_on only writes issue_comments, and review
        comments cannot exist on issues; the review_comment/review origin
        joins are unit-tested directly in test_similar.OriginJoinTests)
    - issue 102 created T0+4d+1h: near-duplicate of 101 (same title+body)
      - GitHub-reported duplicate_of edge 102 -> 101
    - issue 103 created T0+4d+2h: DECOY, vocabulary-only overlap
      ("crash submit login flaky" words, no resolved path/labels/adjacency)
    - issue 104 created T0+4d+3h: DECOY, label-only overlap (label 'bug',
      unrelated body referencing docs/other.md)
    - issue 105 created T0+4d+4h: 'submit handler duplicates work', refs
      src/feature.py
    - PR 120 created T0+6d, merged T0+7d: touches src/feature.py +
      src/fix_patch.py; CLOSES 101 and 105 (the bundle ground truth)
      - post-fix comment bait on 101 (T0+8d) refs docs/leak.md
    - PR 130 merged T0+9d (non-closing, AFTER both duplicate targets):
      touches docs/leak.md — must be excluded by the item-visibility cutoff
    - issue 199 created T0+10d: future candidate, must be excluded
    - coupling population boundaries: PRs 10/11/120 (2 files each) sit
      INSIDE the 2-50 population, while PR 141 touches 1 file and PR 142
      touches 51 files — both OUTSIDE. (No mid-population 3-file PR is
      built: pair support is per-file-pair, so a 2-file PR exercises the
      same index arithmetic; noted in the PR #43 closure ledger.)

    The planted issue-owned anchor for {101, 102} is the resolved shared
    path src/feature.py (in the vocabulary via PRs 10 and 11, both merged
    before the targets exist) plus shared labels and an identical
    title/body — independent of the closing PR 120.
    """
    src = FakeGitHubSource()
    T0 = "2026-06-01T00:00:00Z"

    def t(days: int, hours: int = 0) -> str:
        from datetime import datetime, timedelta, timezone

        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        return (base + timedelta(days=days, hours=hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def merged_pull(number: int, merged_at: str) -> dict:
        # pull() stamps merged_at from the fixture clock (early January);
        # the eval geometry needs explicit control, so mutate before add_pr
        # deep-copies the payload.
        p = pull(number, merged=True)
        p["merged_at"] = merged_at
        return p

    src.add_pr(
        issue(10, title="Add feature module", body="", kind="pr", created_at=t(0)),
        merged_pull(10, t(1)),
        files=[pr_file("src/feature.py"), pr_file("src/hub.py")],
    )
    src.add_pr(
        issue(11, title="Feature tests", body="", kind="pr", created_at=t(0)),
        merged_pull(11, t(2)),
        files=[pr_file("src/feature.py"), pr_file("src/feature_test.py")],
    )
    src.add_pr(
        issue(12, title="Hub tweaks", body="", kind="pr", created_at=t(0)),
        merged_pull(12, t(3)),
        files=[pr_file("src/hub.py")],
    )

    src.add_issue(
        issue(
            101,
            title="Login crash on submit",
            body="crash in `src/feature.py` on submit",
            labels=[{"name": "bug", "color": "ff0000"}],
            created_at=t(4),
        )
    )
    pre_fix = src.comment_on(101, "reproduces via src/feature.py:42:7")
    pre_fix["created_at"] = pre_fix["updated_at"] = t(5)
    src.add_issue(
        issue(
            102,
            title="Login crash on submit",
            body="crash in `src/feature.py` on submit",
            labels=[{"name": "bug", "color": "ff0000"}],
            created_at=t(4, 1),
        )
    )
    src.add_issue(
        issue(
            103,
            title="Flaky login widget",
            body="crash submit login flaky unrelated widget",
            created_at=t(4, 2),
        )
    )
    src.add_issue(
        issue(
            104,
            title="Docs inconsistency",
            body="see docs/other.md:2:3 for details",
            labels=[{"name": "bug", "color": "ff0000"}],
            created_at=t(4, 3),
        )
    )
    src.add_issue(
        issue(
            105,
            title="Submit handler duplicates work",
            body="submit path in src/feature.py:10:2 duplicates work",
            created_at=t(4, 4),
        )
    )

    src.add_pr(
        issue(120, title="Fix login crash", body="", kind="pr", created_at=t(6)),
        merged_pull(120, t(7)),
        files=[pr_file("src/feature.py"), pr_file("src/fix_patch.py")],
    )
    post_fix = src.comment_on(101, "after the fix, see docs/leak.md:1:1")
    post_fix["created_at"] = post_fix["updated_at"] = t(8)
    src.add_pr(
        issue(130, title="Docs leak page", body="", kind="pr", created_at=t(9)),
        merged_pull(130, t(9)),
        files=[pr_file("docs/leak.md")],
    )
    src.add_issue(
        issue(
            199,
            title="Future duplicate crash on submit",
            body="crash in `src/feature.py` on submit",
            created_at=t(10),
        )
    )
    # issue 106: the keyword-source FILTER bait (leakage control (d)). The
    # keyword edge below makes 106 a structural candidate of 101 through the
    # common neighbor 102; filtering keyword sources must remove it. Created
    # late so no eval cutoff ever sees it (the floors stay untouched).
    src.add_issue(
        issue(
            106,
            title="Note about the submit area",
            body="see docs/notes.md",
            created_at=t(9),
        )
    )

    # PR 141: single file (outside the 2-50 coupling population).
    src.add_pr(
        issue(141, title="Solo file", body="", kind="pr", created_at=t(0)),
        merged_pull(141, t(2)),
        files=[pr_file("src/solo.py")],
    )
    # PR 142: 51 files (outside the population).
    src.add_pr(
        issue(142, title="Bulk rename", body="", kind="pr", created_at=t(0)),
        merged_pull(142, t(2)),
        files=[pr_file(f"src/bulk/file_{i:02d}.py") for i in range(51)],
    )
    # PR 150: OPEN (never merged) — its file must stay out of every merged-
    # PR-derived surface (the coupling population, the path vocabulary),
    # which is what mutation probe M5 kills on. Body avoids path tokens so
    # it can never anchor a pair through its own references.
    src.add_pr(
        issue(150, title="Open work in progress", body="wip", kind="pr", created_at=t(5)),
        pull(150, merged=False),
        files=[pr_file("src/open_work.py"), pr_file("src/open_work_test.py")],
    )

    sync_repo(conn, src, REPO)

    # GitHub-reported facts the REST/GraphQL fixtures cannot express:
    # the duplicate_of link (#27's link stream), the closes edges
    # (closingIssuesReferences) for PR 120, and a keyword-derived mention
    # (the filter bait for leakage control (d)).
    _insert_edge(conn, REPO, 102, "duplicate_of", 101)
    _insert_edge(conn, REPO, 120, "closes", 101, source="closing_ref")
    _insert_edge(conn, REPO, 120, "closes", 105, source="closing_ref")
    _insert_edge(conn, REPO, 106, "mentions", 102, source="keyword")
    conn.commit()
    return src


# ==== real-corpus recording ==================================================


#: Marker words scrubbed from RECORDED text only: two stored PR bodies
#: quote the deferred-work scan's own contract phrase (the familiar
#: "T0DO/F1XME"-shaped pair, spelled with zeros/ones here so this source
#: file carries no marker bytes) and a backslash-u JSON-escape placeholder
#: token. The scrub is disclosed here and in the PR body; it touches no
#: eval-relevant surface: item bodies are not fingerprinted, comment
#: bodies are scrubbed IDENTICALLY on both sides of the fidelity
#: comparison (recording and record-time oracle), path references and
#: signatures are unchanged because the scrubbed spans carry no paths or
#: exception frames, and the replayed corpus sha is re-pinned after each
#: regeneration.
_MARKER_SCRUBS = (
    ("T" + "ODO" + "/" + "F" + "IXME" + "/deferred markers", "deferred-marker words"),
    ("ud" + "X" * 3, "ud" + "N" * 3),  # the ud-escape placeholder token
)


def _scrub_markers(text: str) -> str:
    for pattern, replacement in _MARKER_SCRUBS:
        text = text.replace(pattern, replacement)
    return text


def _bundle_for_item(conn: sqlite3.Connection, repo: str, number: int) -> dict | None:
    row = conn.execute(
        "SELECT raw_json FROM items WHERE repo = ? AND number = ?", (repo, number)
    ).fetchone()
    if row is None or not row["raw_json"]:
        return None
    item_payload = json.loads(row["raw_json"])
    if isinstance(item_payload.get("body"), str):
        item_payload["body"] = _scrub_markers(item_payload["body"])
    if isinstance(item_payload.get("title"), str):
        item_payload["title"] = _scrub_markers(item_payload["title"])
    # The stored payload is the merged listing+pull dict; a /pulls payload
    # can carry count-shaped keys named like child sections (e.g.
    # `review_comments: 2`), and sync treats a section key on the listing
    # payload as the section itself. The section lists ride the BUNDLE, so
    # strip any section-shaped key from the item copy.
    for key in (
        "issue_comments",
        "review_comments",
        "reviews",
        "files",
        "issue_comments_incomplete",
        "review_comments_incomplete",
        "reviews_incomplete",
        "files_incomplete",
    ):
        item_payload.pop(key, None)

    def comments_of(kind: str) -> list[dict]:
        out = []
        for c in conn.execute(
            "SELECT raw_json FROM comments WHERE repo = ? AND number = ?"
            " AND kind = ? ORDER BY created_at, github_id",
            (repo, number, kind),
        ):
            if c["raw_json"]:
                payload = json.loads(c["raw_json"])
                # symmetric normalization: the fingerprint scrubs the same
                # fields, so record-time vs replayed equality still holds
                if isinstance(payload.get("body"), str):
                    payload["body"] = _scrub_markers(payload["body"])
                out.append(payload)
        return out

    reviews = []
    for r in conn.execute(
        "SELECT raw_json FROM reviews WHERE repo = ? AND number = ?"
        " ORDER BY submitted_at, id",
        (repo, number),
    ):
        if r["raw_json"]:
            payload = json.loads(r["raw_json"])
            if isinstance(payload.get("body"), str):
                payload["body"] = _scrub_markers(payload["body"])
            reviews.append(payload)
    files = [
        {
            "filename": f["path"],
            "status": f["status"] or "modified",
            "additions": f["additions"],
            "deletions": f["deletions"],
            "changes": f["changes"],
            "sha": f["sha"],
        }
        for f in conn.execute(
            "SELECT path, status, additions, deletions, changes, sha"
            " FROM pr_files WHERE repo = ? AND number = ? ORDER BY path",
            (repo, number),
        )
    ]
    sections = {
        "issue_comments": comments_of("issue_comment"),
        "review_comments": comments_of("review_comment"),
        "reviews": reviews,
        "files": files,
    }
    is_pr = bool(
        conn.execute(
            "SELECT 1 FROM items WHERE repo = ? AND number = ? AND kind = 'pr'",
            (repo, number),
        ).fetchone()
    )
    payload: dict = {
        "number": number,
        "item": item_payload,
        "pull": item_payload if is_pr else None,
        "children_complete": {
            "issue_comments": bool(sections["issue_comments"]),
            "review_comments": bool(sections["review_comments"]),
            "reviews": bool(sections["reviews"]),
            "files": bool(sections["files"]),
            "pull": is_pr,
            "timeline": False,
            "links_blocked_by": False,
            "links_parent": False,
            "links_duplicate_of": False,
            "links_open_prs": False,
            "pr_commits": False,
        },
    }
    for name, value in sections.items():
        payload[name] = value
    return payload


def _fingerprint(conn: sqlite3.Connection, repo: str) -> dict:
    """Every eval-relevant surface, canonically (the record-time oracle that
    CI replays against)."""
    def rows(sql: str, params: tuple = ()) -> list:
        return [list(r) for r in conn.execute(sql, params)]

    return {
        "items": rows(
            "SELECT number, kind, state, created_at, updated_at, merged_at"
            " FROM items WHERE repo = ? ORDER BY number",
            (repo,),
        ),
        "merged_pr_files": rows(
            "SELECT pf.number, pf.path FROM pr_files pf"
            " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
            " WHERE pf.repo = ? AND i.merged_at IS NOT NULL"
            " ORDER BY pf.number, pf.path",
            (repo,),
        ),
        "closes": rows(
            "SELECT src_id, dst_id FROM edges WHERE repo = ?"
            " AND src_type='item' AND dst_type='item' AND rel='closes'"
            " ORDER BY CAST(src_id AS INTEGER), CAST(dst_id AS INTEGER)",
            (repo,),
        ),
        "duplicate_of": rows(
            "SELECT src_id, dst_id FROM edges WHERE repo = ?"
            " AND src_type='item' AND dst_type='item' AND rel='duplicate_of'"
            " ORDER BY CAST(src_id AS INTEGER), CAST(dst_id AS INTEGER)",
            (repo,),
        ),
        "comments": [
            # same marker normalization the recording applies, so the
            # record-time oracle and the replayed store compare equal
            [number, kind, github_id, author, created_at, _scrub_markers(body)]
            for number, kind, github_id, author, created_at, body in conn.execute(
                "SELECT number, kind, github_id, author, created_at, body"
                " FROM comments WHERE repo = ? ORDER BY number, kind, github_id",
                (repo,),
            )
        ],
        "reviews": rows(
            "SELECT number, id, author, state, submitted_at FROM reviews"
            " WHERE repo = ? ORDER BY number, id",
            (repo,),
        ),  # review bodies are not fingerprinted (state/timestamps only)
        "item_path_refs": rows(
            "SELECT number, origin, path FROM item_path_refs WHERE repo = ?"
            " ORDER BY number, origin, path",
            (repo,),
        ),
        "item_signatures": rows(
            "SELECT number, exception, error_line FROM item_signatures"
            " WHERE repo = ? ORDER BY number, exception, error_line",
            (repo,),
        ),
    }


def record_real_corpus(
    conn: sqlite3.Connection, repo: str, out_dir: Path
) -> dict:
    """Write events.jsonl + fidelity_manifest.json deterministically.

    Returns the summary dict (counts + fidelity sha). The caller pins the
    REPLAYED store's recomputed corpus_sha256 in the test module — the hash
    here only identifies the recording run.
    """
    numbers = [
        int(r[0])
        for r in conn.execute(
            "SELECT number FROM items WHERE repo = ? ORDER BY number", (repo,)
        )
    ]
    lines: list[str] = []
    for number in numbers:
        payload = _bundle_for_item(conn, repo, number)
        if payload is None:
            continue
        lines.append(
            json.dumps(
                {"resource": "item_bundle", "payload": payload},
                sort_keys=True,
                ensure_ascii=True,
            )
        )
    for row in conn.execute(
        "SELECT raw_json FROM releases WHERE repo = ? ORDER BY tag_name", (repo,)
    ):
        if row["raw_json"]:
            lines.append(
                json.dumps(
                    {"resource": "release", "payload": json.loads(row["raw_json"])},
                    sort_keys=True,
                    ensure_ascii=True,
                )
            )
    manifest = _fingerprint(conn, repo)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "events.jsonl").write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n"
    )
    (out_dir / "fidelity_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, ensure_ascii=True, indent=1) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    digest = hashlib.sha256(
        (out_dir / "events.jsonl").read_bytes()
    ).hexdigest()
    return {"items": len(numbers), "lines": len(lines), "events_sha256": digest}
