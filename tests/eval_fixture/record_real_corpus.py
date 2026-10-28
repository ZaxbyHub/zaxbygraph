"""Regenerate tests/eval_fixture/events.jsonl + fidelity_manifest.json.

The committed sidecar is a deterministic recording of a real
ZaxbyHub/zaxbygraph store, synthesized into replayable item_bundle records
(tests/fixtures_eval.record_real_corpus). Run from the repository root,
pointing at a store to record:

    python tests/eval_fixture/record_real_corpus.py [PATH_TO_STORE]

Without an argument the user-level store for the origin repo is used. The
recording is only as faithful as the committed fidelity manifest asserts:
after regenerating, run the test module and confirm the fingerprint checks
pass, then re-pin REPLAYED_CORPUS_SHA256 in tests/test_similar.py from the
replayed store. Changing the recording is an intentional fixture change —
the empirical eval goals in the test module docstring must be re-measured
and re-justified against it.

Known unrecoverable families (disclosed): the original per-section
`children_complete` map is not stored and is re-emitted conservatively
(present sections True, absent False), and GraphQL-only timeline/link pages
beyond what the tables retained cannot be reconstructed.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from fixtures_eval import record_real_corpus  # noqa: E402
from zaxbygraph.db import connect, open_existing  # noqa: E402


def main() -> int:
    store_arg = sys.argv[1] if len(sys.argv) > 1 else None
    repo = "zaxbyhub/zaxbygraph"
    if store_arg:
        conn = connect(Path(store_arg))
    else:
        from zaxbygraph.paths import store_db_path, store_root

        candidate = store_root() / "github.com" / repo / "history.db"
        if not candidate.exists():
            print(f"no user-level store found at {candidate}; pass a store path", file=sys.stderr)
            return 2
        conn = open_existing(candidate)
    try:
        summary = record_real_corpus(conn, repo, Path(__file__).resolve().parent)
    finally:
        conn.close()
    print(
        f"recorded {summary['items']} items ({summary['lines']} lines),"
        f" events sha256 {summary['events_sha256']}"
    )
    print("re-pin REPLAYED_CORPUS_SHA256 in tests/test_similar.py after verifying")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
