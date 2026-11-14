# eval_fixture_backtest - the C17 recorded backtest corpus (issues #68/#69)

A NEIGHBORHOOD subcorpus of the real `ZaxbyHub/zaxbygraph` repository,
recorded from the user-level store
(`%LOCALAPPDATA%/zaxbygraph/github.com/zaxbyhub/zaxbygraph/history.db`,
opened read-only) into a replayable `events.jsonl`
(`tests/replay.py::JsonlReplaySource` re-ingests it through the real sync
path). `tests/test_selection_quality.py::RecordedCorpusBacktestTests` (C17,
the #69 acceptance gate) runs its backtest on THIS corpus - unlike
`tests/eval_fixture/`, which is anchored by the issue #29 traces and holds
an older, whole-repo recording.

## Neighborhood rule

1. **Fix PRs** - every MERGED PR (`kind='pr'`, `merged_at` set) whose
   `closes` UNION `closes_keyword` edges close >= 2 distinct issues.
   Recorded: PRs 20, 41, 45, 49, 50, 51, 54, 63, 76, 78 (10 PRs).
2. **Closed issues** - every issue closed by one of those PRs through
   either stream: 22 issues
   (17, 19, 26, 27, 30, 32, 33, 34, 36, 40, 44, 46, 48, 52, 53, 61, 62,
   65, 66, 67, 71, 72).
3. **Open-at-the-time pool** - every issue that was OPEN at some fix PR's
   creation (`kind='issue'`, `created_at <= that PR's created_at`, and
   either still open now or closed at/after that PR's creation): 35
   issues (the 22 closed ones plus still-open 8, 21, 28, 29, 31, 64, 68,
   69, 70, 73, 74, 75, 77). These are the candidates the per-fix-PR
   snapshots may legitimately rank.
4. **Children and actors** - the recorded items' comments, review
   comments, reviews and pr_files ride the `item_bundle` records
   (`tests/fixtures_eval._bundle_for_item`); actors repopulate from the
   recorded payloads at replay. Fix PRs additionally carry their stored
   `pr_commits` section (94 rows total) so the commit-message
   `closes_keyword` stream re-derives through the real ingest path: PR
   49's #32 link exists ONLY as a timeline `closes` edge plus the
   commit-message keyword edge of commit `0440c7c7...`, and the commit
   half is what makes PR 49 replayable as a multi-issue closer.
5. **Revisions** (amendment wave 3) - the neighborhood items'
   `revisions` rows (the store's own text pre-images:
   `item_title`/`item_body`/`comment`/`review` displacement history) are
   recorded VERBATIM as one extra `{"resource": "revisions"}` record at
   the end of `events.jsonl` (50 rows: 37 comment, 7 item_body, 2 review,
   4 item_title; content scrubbed with the same marker normalization as
   every other recorded text; the SQLite-assigned `id` excluded). Sync
   alone cannot replay them - the ingest path writes a pre-image only
   when it OBSERVES a change between fetches, and a recording cannot
   re-observe the original edit sequence - so
   `tests/fixtures_backtest.py::BacktestReplaySource` collects the record
   and `replay_backtest_store()` re-inserts the rows after the sync pass.
   The base `tests/replay.py::JsonlReplaySource` ignores unknown
   resources, so the older whole-file replay semantics are unchanged.

Total: 45 item bundles + 1 revisions record (3.15 MB).
`recording_manifest.json` records the neighborhood sets, the revisions
counts, and the `events.jsonl` sha256 at record time.

## Disclosed recording limitations

The same families `tests/eval_fixture/` discloses, plus the neighborhood
cuts:

- Timeline-sourced `closes` edges (1 row on the source store: PR 49 ->
  #32) are not re-derivable from item bundles; PR 49's multi-issue truth
  survives through the recorded commit stream instead (its provenance is
  `keyword`, not `closes`).
- Link-stream edges (`duplicate_of`, `blocked_by`, `child_of`,
  `linked_pr`), `item_events`, and any PR outside the fix-PR set are not
  recorded; open PRs of the source repo therefore cannot gate replayed
  pool issues, and the co-change population is exactly the ten fix PRs.
- Per-section `children_complete` is re-emitted conservatively (present
  sections True, absent False), as in `tests/eval_fixture/`.
- **Revisions coverage is partial by construction**: pre-images exist
  only for text edits the source store's sync OBSERVED (an edit inside a
  sync gap leaves no row). On this recording, 3 of the 22 truth
  endpoints (61, 62, 72) have an `item_title`/`item_body` pre-image
  chain reaching before their fix PR's `created_at`, and only fix PR
  63's case has BOTH endpoints covered. The per-case disclose+exclude
  rule that follows - a case whose truth endpoints lack covering
  pre-images discloses `text_as_of_unknown` - is pinned in the CHECK
  (`tests/test_selection_quality.py` C17), not in this fixture; the
  fixture merely carries the honest 50 rows.

## Regenerating

    python tests/eval_fixture_backtest/record_backtest_corpus.py [STORE]

Re-recording changes the corpus: re-derive and re-pin the C17 GT numbers
(fix PRs / truth pairs / eligible targets / per-PR ground-truth
provenance) in `tests/test_selection_quality.py` from the REPLAYED store,
not from the source store - the replay's edge set is the truth the test
asserts against. Re-derive the revisions coverage numbers (endpoints
with a pre-image chain reaching before their fix PR's `created_at`, and
which fix-PR cases are fully covered) the same way: they drive the
per-case `text_as_of_unknown` disclosure pins.
