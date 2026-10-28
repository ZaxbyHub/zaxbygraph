"""Regenerate tests/corpus_fixture/events.jsonl and print the golden hash.

The committed sidecar is a deterministic recording of one sync of the
two-item store built below (same shapes as tests/test_export_corpus.py's
``_rich_store`` — keep them in sync). Run from the repository root:

    PYTHONPATH="src;tests" python tests/corpus_fixture/generate_fixture.py

It overwrites ``events.jsonl`` in place and prints the corpus_sha256 to pin
as ``GOLDEN_FIXTURE_SHA256`` in tests/test_export_corpus.py. Changing the
store shapes below is an intentional format change: the golden constant is
the compatibility contract, so regenerate it only with a documented reason.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from fixtures import REPO, FakeGitHubSource, issue, pr_file, pull, review
from zaxbygraph import export as export_mod
from zaxbygraph.db import connect, init_schema, open_existing
from zaxbygraph.sync import sync_repo

FIXTURE_DIR = Path(__file__).resolve().parent


def rich_source() -> FakeGitHubSource:
    """One issue (non-ASCII body, comment, label), one merged PR whose file
    totals agree with its summary (3/1), one review on that PR, one release."""
    src = FakeGitHubSource()
    src.add_issue(
        issue(
            1,
            title="Probe ünïcode item",
            body="see src/app.py — déjà vu",
            labels=[{"name": "bug", "color": "ff0000"}],
        )
    )
    src.comment_on(1, "a comment with an em-dash — and ünïcode")
    src.add_pr(
        issue(2, title="Probe PR", body="", kind="pr"),
        pull(2, merged=True, additions=3, deletions=1),
        files=[pr_file("src/app.py")],
    )
    review_row = review(303, "APPROVED", "ship it — ok")
    review_row["html_url"] = f"https://github.com/{REPO}/pull/2#pullrequestreview-303"
    src.reviews[2] = [review_row]
    src.releases.append(
        {
            "id": 77,
            "tag_name": "v0.1.0",
            "name": "First release",
            "body": "notes — with em-dash",
            "draft": False,
            "prerelease": False,
            "author": {"login": "alice"},
            "created_at": "2026-01-01T00:00:00Z",
            "published_at": "2026-01-02T00:00:00Z",
            "html_url": f"https://github.com/{REPO}/releases/tag/v0.1.0",
        }
    )
    return src


def main() -> None:
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="corpus-fixture-gen-"))
    db = connect(workdir / "gen.db")
    init_schema(db)
    result = sync_repo(db, rich_source(), REPO, jsonl_path=workdir / "sidecar")
    print("sync ingested:", result.get("ingested"))
    raw = (workdir / "sidecar" / "events.jsonl").read_bytes()
    assert raw, "sidecar is empty"
    (FIXTURE_DIR / "events.jsonl").write_bytes(raw)
    print("fixture bytes:", len(raw))

    conn = open_existing(workdir / "gen.db")
    try:
        import io

        buf = io.BytesIO()
        export_mod.write_corpus(
            conn, REPO, buf, None, frozenset(), False, str(workdir / "gen.db")
        )
    finally:
        conn.close()
    manifest = json.loads(
        buf.getvalue()[: buf.getvalue().index(b"\n")].decode("ascii")
    )
    print("GOLDEN corpus_sha256:", manifest["corpus_sha256"])
    print("counts:", manifest["counts"])


if __name__ == "__main__":
    main()
