"""Issue #56 frozen acceptance checks C19-C20: what a LEGACY candidate's
own schema says, and the storage-class contract for every TEXT column the
adoption reads. Authored independently of any fix as a frozen spec: every
test is RED at base 80ea01c6ae202ea03373cc26ad9ff8cc561fe39a for the
reason its docstring names, and GREEN after the fix. All fault injection
is in-process; fixtures are local SQLite/temp-dir builds — no network, no
gh, no credentials.

C19 (AC19) pins the round-6 reviewer's finding 1: `_MigratedCopy` migrates
a legacy candidate, which recreates the tables that version predates as
EMPTY, so the post-migration preflight cannot see their absence and the
unconditional delete loop removes the store's rows for them while the
copy brings zero back — the truncated-image outcome AC3 refuses, on the
tool's primary input class. The candidate's own table set is now captured
before the migration, a table outside it is preserved, and the envelope
names it.

C20 (AC20) pins the round-6 reviewer's finding 2: the storage-class
contract covered an enumerated column list, so a BLOB in another TEXT
column either aborted the whole command through the migration's v8->v9
text backfill (legacy) or was copied verbatim into the store's TEXT
column (current version). One schema-derived rule now covers every TEXT
column the adoption reads.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fixtures import REPO, issue, pull, review  # noqa: E402
from test_migrations import V1_SCHEMA_SQL  # noqa: E402

import zaxbygraph.doctor as doctor_mod  # noqa: E402
from zaxbygraph import db as _db  # noqa: E402
from zaxbygraph.db import (  # noqa: E402
    connect,
    init_schema,
)

#: `_TABLE_INTRODUCED_VERSION` and `current_table_columns` are this branch's
#: own; at the frozen base they do not exist and the census pin is skipped.
_TABLE_INTRODUCED_VERSION = getattr(_db, "_TABLE_INTRODUCED_VERSION", None)
current_table_columns = getattr(_db, "current_table_columns", None)
from zaxbygraph.store import ingest_item  # noqa: E402

#: The v10-era repo-keyed tables a pre-v10 candidate's schema never has.
_V10_TABLES = ("item_events", "pr_commits", "issue_field_values")
#: A raw_json payload whose title/body carry BACKTICKED paths and a
#: closing keyword, so the v8→v9 backfill really produces
#: `item_path_refs`/`item_signatures` rows for a genuine legacy candidate
#: (a plain path without backticks produces nothing).
_RAW_JSON = (
    '{"title": "fix `src/app.py`", "body": "closes #1 via `src/app.py` and'
    ' `src/lib/util.py`"}'
)
#: A comment body with the same backfillable text (the v8→v9 backfill
#: reads `comments.body` with a `comment:<github_id>` origin).
_COMMENT_BODY = "see `src/app.py` and `src/lib/util.py` for the fix"


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _prep(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _item(n: int) -> dict:
    payload = issue(n, title=f"t{n}", body=f"body {n}",
                    created_at="2027-06-01T00:00:00Z")
    payload["id"] = 9000 + n
    return payload


def _seed_store(db: Path, numbers: tuple[int, ...], stamp: str,
                with_v10: bool = False) -> None:
    """A healthy candidate/store for `numbers`, complete and freshest.

    One item is a PR (so `items.head_ref` carries a value) and each item
    has one issue comment and one review (so `comments.body` and
    `reviews.body` carry values): the v8->v9 text backfill reads all three
    in Python, which is the abort surface C20 pins.
    """
    conn = connect(db)
    init_schema(conn)
    for n in numbers:
        ingest_item(
            conn, REPO, _item(n), pull_raw=pull(n) if n == numbers[0] else None,
            issue_comments=[{
                "id": 7000 + n,
                "user": {"login": "bob", "html_url": "https://github.com/bob"},
                "body": "see src/app.py for the fix",
                "created_at": "2027-06-02T00:00:00Z",
            }],
            review_comments=[],
            reviews=[review(6000 + n, "COMMENTED",
                            body="review src/app.py please")],
            files=[], include_patches=False)
    if with_v10:
        for n in numbers:
            conn.execute(
                "INSERT INTO item_events(repo, number, type, created_at)"
                " VALUES (?, ?, 'reopened', '2027-06-03T00:00:00Z')",
                (REPO, n))
            conn.execute(
                "INSERT INTO pr_commits(repo, number, sha, message)"
                " VALUES (?, ?, 'deadbeef', 'msg')", (REPO, n))
            conn.execute(
                "INSERT INTO issue_field_values(repo, number, field_name, value)"
                " VALUES (?, ?, 'size', 'M')", (REPO, n))
    conn.execute(
        "UPDATE sync_state SET last_full_sync_at = ?, full_sync_pending = 0,"
        " issues_since = ? WHERE repo = ?", (stamp, stamp, REPO))
    conn.commit()
    conn.close()


def _strip_v10_tables(bad: Path, version: int) -> None:
    """Drop the v10-era tables and stamp the candidate at `version` — a
    genuine older schema, which `init_schema` then migrates by recreating
    those tables EMPTY."""
    conn = sqlite3.connect(str(bad))
    for table in _V10_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


def _genuine_legacy_candidate(bad: Path, version: int) -> None:
    """A GENUINE pre-v7/pre-v9 schema: the v6→v7 and v8→v9 migrations
    BACKFILL `assignees`/`issue_field_values` from `raw_json` and
    `item_path_refs`/`item_signatures` from the bodies, so the tables the
    candidate's version never had are recreated POPULATED, not empty —
    the round-7 reviewer's finding 1, which a table-drop fixture cannot
    see (its bodies carry no backfillable text)."""
    from test_migrations import V1_SCHEMA_SQL

    conn = sqlite3.connect(str(bad))
    conn.executescript(V1_SCHEMA_SQL)
    conn.execute(f"PRAGMA user_version = {version}")
    # v1's sync_state carries no full_sync_pending; the v1->v2 migration
    # adds it, which is exactly the shape a genuine legacy store has.
    conn.execute(
        "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
        " include_patches) VALUES (?, '2027-06-01T00:00:00Z',"
        " '2027-06-01T00:00:00Z', 0)", (REPO,))
    conn.execute(
        "INSERT INTO items(id, repo, number, kind, title, state, updated_at,"
        " raw_json) VALUES (1, ?, 1, 'issue', 'one', 'open',"
        " '2027-06-01T00:00:00Z', ?)", (REPO, _RAW_JSON))
    conn.execute(
        "INSERT INTO items(id, repo, number, kind, title, state, updated_at,"
        " raw_json) VALUES (2, ?, 2, 'issue', 'two', 'open',"
        " '2027-06-01T00:00:00Z', ?)", (REPO, _RAW_JSON))
    conn.execute(
        "INSERT INTO comments(github_id, repo, number, kind, author, body,"
        " raw_json) VALUES (7001, ?, 1, 'issue_comment', 'bob', ?, '{}')",
        (REPO, _COMMENT_BODY))
    conn.commit()
    conn.close()


def _blob_column_candidate(bad: Path, table: str, column: str,
                           legacy: bool) -> None:
    """Set one row's `table.column` to a BLOB, optionally stamping the
    candidate at v1 so the migration chain reads it before any preflight."""
    conn = sqlite3.connect(str(bad))
    if table == "pr_files":
        # The v6->v7 rebuild reads pr_files.path only for PR items, so the
        # fixture needs a PR whose file row carries the BLOB.
        conn.execute(
            "INSERT INTO pr_files(repo, number, path, status, additions,"
            " deletions, changes, sha) VALUES (?, ?, x'00', 'modified', 1,"
            " 1, 2, 'abc')", (REPO, 5))
        conn.execute(
            "UPDATE items SET kind = 'pr' WHERE repo = ? AND number = 5",
            (REPO,))
    else:
        conn.execute(
            f"UPDATE {table} SET {column} = x'00' WHERE repo = ?", (REPO,))
    if legacy:
        conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


def _wrong_typed_raw_json_candidate(bad: Path) -> None:
    """Make one item's `raw_json` carry wrong-typed title/body fields (the
    #53 malformed-payload class) and stamp the candidate at v1 so the v8→v9
    backfill reads them before any preflight.

    The body's repr deliberately contains a backticked path AND a closing
    keyword, so a `str()` coercion (the hazard the db.py comment names)
    would fabricate an item_path_refs row and a closes_keyword edge from a
    malformed payload, while the empty-text treatment yields none — the two
    are observably different, which is what makes the check discriminating
    (swarm-pr-review N-03)."""
    conn = sqlite3.connect(str(bad))
    conn.execute(
        "UPDATE items SET raw_json = ? WHERE repo = ? AND number = 5",
        ('{"title": 5, "body": ["closes #1 via `src/app.py`"]}', REPO))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


def _tables(db: Path) -> set[str]:
    if not db.exists():
        return set()
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()


def _count(db: Path, table: str) -> int:
    if not db.exists():
        return 0
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


class LegacyTablePreservationTests(unittest.TestCase):
    """AC19: a repo-keyed table the adopted candidate's VERSION never had
    keeps the store's rows — deleting would copy zero back (the
    truncated-image outcome AC3 refuses). Two shapes: the schema never
    carried the table (the migration recreated it), and the schema DOES
    carry it while the version stamp predates its slot (an
    interrupted-upgrade remnant, because `init_schema` commits the current
    schema DDL and only then runs the migration chain)."""

    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="zgt56-legacy-adopt-"))
        self.addCleanup(_rmtree, self.scratch)
        # The version→slot map is hand-maintained, so it is pinned against
        # drift here: every table in the current schema is either in the v1
        # DDL or named in `_TABLE_INTRODUCED_VERSION`. A new table whose
        # slot is missing from the map silently stops being preserved.
        # Guarded because both symbols are this branch's own — at the frozen
        # base the shapes carry the RED evidence instead.
        if _TABLE_INTRODUCED_VERSION is None:
            return
        v1 = sqlite3.connect(":memory:")
        try:
            v1.executescript(V1_SCHEMA_SQL)
            v1_tables = {r[0] for r in v1.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%'")}
        finally:
            v1.close()
        # FTS shadow tables and sqlite_* are machinery, not repo-keyed
        # tables the preservation rule reasons about.
        current = {t for t in current_table_columns()
                   if not t.startswith("sqlite_")
                   and "_fts" not in t}
        self.assertEqual(
            current - v1_tables - set(_TABLE_INTRODUCED_VERSION), set(),
            "a current table is in neither the v1 DDL nor the"
            " introduced-version map, so the preservation rule cannot know"
            " its slot")
        self.assertEqual(
            set(_TABLE_INTRODUCED_VERSION) - current, set(),
            "the introduced-version map names a table the current schema"
            " does not have")

    def _pair(self, tag: str, version: int):
        """(good, bad, store, cwd): a store holding v10-table rows and a
        freshest complete legacy candidate whose schema predates them."""
        root = self.scratch / tag
        store = _prep(root / "store" / "history.db")
        _seed_store(store, (1, 2), "2027-05-01T00:00:00Z", with_v10=True)
        bad = _prep(root / "cand" / "history.db")
        _seed_store(bad, (1, 2), "2027-06-01T00:00:00Z")
        _strip_v10_tables(bad, version)
        return store, bad, _prep(root / "cwd")

    def test_legacy_candidate_preserves_tables_its_version_never_had(
            self) -> None:
        """RED at base, GREEN after the fix: at base the delete loop runs
        unconditionally, the store's item_events/pr_commits/
        issue_field_values rows are deleted and zero rows are copied back,
        and the envelope reports a clean adoption. After the fix the
        candidate's own table set is captured before the migration, those
        tables are preserved, and the envelope names them."""
        for version in (9, 5):
            with self.subTest(version=version):
                store, bad, cwd = self._pair(f"c19-v{version}", version)
                before = {t: _count(store, t) for t in _V10_TABLES}
                self.assertTrue(all(before.values()),
                                "the fixture store holds no v10 rows")
                envelope = doctor_mod.doctor(cwd, REPO, store,
                                             extra_scans=[bad],
                                             consolidate_flag=True)
                self.assertTrue(envelope.get("ok"), envelope)
                self.assertTrue(envelope.get("adopted"), envelope)
                after = {t: _count(store, t) for t in _V10_TABLES}
                self.assertEqual(after, before,
                                 f"v{version}: the store's rows for a table"
                                 f" the candidate's version never had were"
                                 f" deleted: {before} -> {after}")
                # The expected set is HARD-CODED per version, not derived
                # from the implementation's own slot map: a wrong slot value
                # in `_TABLE_INTRODUCED_VERSION` must make this assertion
                # fail (cubic review, PR #58). v9's schema predates only the
                # v10 slot, so the three dropped tables are the preserved
                # set; v5's stamp predates the v7/v8/v9 slots too, so the
                # tables those migrations introduce are preserved as well —
                # the version rule, not just the absent-table rule.
                # `revisions` is excluded: it has its own per-item scoping
                # (issue #40) and never enters the preserved list.
                expected = {
                    9: sorted(_V10_TABLES),
                    5: sorted((set(_V10_TABLES) | {"assignees",
                                                   "issue_field_values",
                                                   "item_path_refs",
                                                   "item_signatures"})),
                }[version]
                self.assertEqual(
                    envelope.get("tables_preserved"), expected,
                    f"v{version}: the envelope does not name the preserved"
                    f" tables: {envelope.get('tables_preserved')}")
                # the store's own corpus is still the candidate's: the
                # adoption is not skipped, only the never-had tables are
                entry = {os.path.normcase(str(r.get("path", ""))): r
                         for r in envelope.get("scanned") or []}
                self.assertTrue(
                    entry.get(os.path.normcase(str(bad)), {}).get("adopted"),
                    "the legacy candidate was not adopted")

    def test_interrupted_upgrade_candidate_preserves_newer_tables(
            self) -> None:
        """RED at base's fix, GREEN after the round-9 correction: a candidate
        left by an INTERRUPTED UPGRADE — `init_schema` commits the current
        schema DDL and only then runs the migration chain, so a crash
        between them leaves the newer tables present under the old
        `user_version`, never populated — is tool-produced, not hand-mangled
        (round-9 reviewer finding 1: the round-8 disclosure claimed the
        divergence was limited to a hand-mangled stamp, which this probe
        refutes). The version rule preserves the tables whose slot the
        stamp predates, so the store's rows survive and the envelope names
        them."""
        from zaxbygraph.db import MIGRATIONS, _schema_sql

        root = self.scratch / "c19-interrupted"
        store = _prep(root / "store" / "history.db")
        _seed_store(store, (1, 2), "2027-05-01T00:00:00Z", with_v10=True)
        bad = _prep(root / "cand" / "history.db")
        # A genuine v9 store: the real migration chain, stopped at 9.
        conn = sqlite3.connect(str(bad))
        conn.executescript(V1_SCHEMA_SQL)
        for target, migrate in MIGRATIONS:
            if target > 9:
                break
            conn.execute("BEGIN IMMEDIATE")
            migrate(conn)
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " include_patches) VALUES (?, '2027-06-01T00:00:00Z',"
            " '2027-06-01T00:00:00Z', 0)", (REPO,))
        conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, state,"
            " updated_at, raw_json) VALUES (1, ?, 1, 'issue', 'one', 'open',"
            " '2027-06-01T00:00:00Z', ?)", (REPO, _RAW_JSON))
        conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, state,"
            " updated_at, raw_json) VALUES (2, ?, 2, 'issue', 'two', 'open',"
            " '2027-06-01T00:00:00Z', ?)", (REPO, _RAW_JSON))
        conn.commit()
        conn.close()
        self.assertNotIn("item_events", _tables(bad),
                         "a genuine v9 schema carries no item_events")
        # The interrupted upgrade: the current DDL is committed, creating
        # item_events/pr_commits empty, and the v9->v10 migration never
        # runs, so the stamp stays at 9 with the newer tables present.
        conn = sqlite3.connect(str(bad))
        conn.executescript(_schema_sql())
        conn.execute("PRAGMA user_version = 9")
        conn.commit()
        conn.close()
        self.assertEqual(_tables(bad) & set(_V10_TABLES), set(_V10_TABLES),
                         "the fixture does not carry the v10 tables under a"
                         " v9 stamp")
        # `issue_field_values` (introduced at v7, so at or below the
        # candidate's stamp) is NOT preserved: a genuine v9 candidate that
        # carries the table makes a claim about it, and replace-the-slug
        # honours that claim. Only the tables whose slot the stamp predates
        # are preserved.
        preserved = sorted(set(_V10_TABLES) - {"issue_field_values"})
        before = {t: _count(store, t) for t in preserved}
        cwd = _prep(root / "cwd")
        envelope = doctor_mod.doctor(cwd, REPO, store, extra_scans=[bad],
                                     consolidate_flag=True)
        self.assertTrue(envelope.get("adopted"), envelope)
        after = {t: _count(store, t) for t in preserved}
        self.assertEqual(after, before,
                         f"an interrupted-upgrade candidate took the store's"
                         f" rows for tables its version never populated:"
                         f" {before} -> {after}")
        self.assertEqual(
            envelope.get("tables_preserved"), preserved,
            "the envelope does not name the preserved tables:"
            f" {envelope.get('tables_preserved')}")

    def test_genuine_legacy_candidate_is_adopted_not_refused(self) -> None:
        """RED at base's fix, GREEN after the round-7 correction: a GENUINE
        pre-v7/pre-v9 schema (V1_SCHEMA_SQL, path-bearing bodies) is
        migrated by a chain that BACKFILLS `assignees`,
        `issue_field_values`, `item_path_refs` and `item_signatures` from
        the candidate's own payload, so the tables its version never had
        come back POPULATED. Skipping only the delete made the copy insert
        those backfilled rows on top of the store's preserved ones — the
        keys collide, the plain INSERT raises IntegrityError, and the copy
        handler refused a perfectly genuine legacy candidate with "schema
        defeated the copy". The invariant is therefore neither deleted NOR
        copied — and, because the candidate's own backfilled rows ARE an
        observation, they are ADOPTED rather than silently dropped
        (swarm-pr-review N-02: the reviewer's probe showed a genuine v1
        candidate adopted with assignees/issue_field_values/item_path_refs
        all EMPTY while base returned them populated). The preserved set is
        therefore only the tables the migration does NOT populate from this
        payload; the backfilled ones are adopted and asserted here."""
        for version in (1,):
            with self.subTest(version=version):
                root = self.scratch / f"c19-genuine-v{version}"
                store = _prep(root / "store" / "history.db")
                _seed_store(store, (1, 2), "2027-05-01T00:00:00Z")
                # the store already holds the rows the candidate's
                # migration would backfill for the same items
                conn = sqlite3.connect(str(store))
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO item_path_refs(repo, number,"
                        " origin, path, backticked) VALUES (?, 1, 'body',"
                        " 'src/app.py', 1)", (REPO,))
                    conn.commit()
                finally:
                    conn.close()
                bad = _prep(root / "cand" / "history.db")
                _genuine_legacy_candidate(bad, version)
                cwd = _prep(root / "cwd")
                envelope = doctor_mod.doctor(cwd, REPO, store,
                                             extra_scans=[bad],
                                             consolidate_flag=True)
                entry = {os.path.normcase(str(r.get("path", ""))): r
                         for r in envelope.get("scanned") or []}.get(
                             os.path.normcase(str(bad)))
                self.assertTrue(
                    envelope.get("adopted"),
                    f"v{version}: a genuine legacy candidate was refused:"
                    f" {entry}")
                self.assertTrue(
                    entry is not None and not entry.get("unreadable"),
                    f"v{version}: the candidate is reported unreadable:"
                    f" {entry}")
                # The payload names no assignee and no issue field value and
                # carries no error signature, so those three stay empty and
                # are preserved; the bodies DO carry backticked paths, so the
                # v8->v9 backfill populates item_path_refs and the
                # candidate's own rows are adopted. item_events/pr_commits
                # have no backfill at all.
                self.assertEqual(
                    envelope.get("tables_preserved"),
                    sorted(("assignees", "issue_field_values",
                            "item_signatures", "item_events", "pr_commits")),
                    f"v{version}: the envelope does not name the preserved"
                    f" tables: {envelope.get('tables_preserved')}")
                # The candidate's own derived rows landed — the invariant
                # N-02 says was never asserted. The migrated copy holds 6:
                # 2 items x 2 backticked body paths, plus the comment's 2.
                conn = sqlite3.connect(str(store))
                try:
                    paths = [tuple(r) for r in conn.execute(
                        "SELECT path FROM item_path_refs WHERE repo = ?"
                        " ORDER BY path", (REPO,)).fetchall()]
                finally:
                    conn.close()
                self.assertEqual(
                    sorted({p for (p,) in paths}),
                    ["src/app.py", "src/lib/util.py"],
                    "the candidate's own backfilled item_path_refs rows were"
                    f" dropped: {paths}")
                self.assertEqual(
                    len(paths), 6,
                    "the candidate's own backfilled item_path_refs rows were"
                    f" not adopted: {paths}")

    def test_current_version_candidate_replaces_normally(self) -> None:
        """PRESERVING: a candidate at the current version carries every
        table, so nothing is preserved and the store's rows are replaced as
        before — the preservation rule must not fire spuriously."""
        root = self.scratch / "c19-current"
        store = _prep(root / "store" / "history.db")
        _seed_store(store, (1, 2), "2027-05-01T00:00:00Z", with_v10=True)
        bad = _prep(root / "cand" / "history.db")
        _seed_store(bad, (1, 2), "2027-06-01T00:00:00Z", with_v10=True)
        cwd = _prep(root / "cwd")
        envelope = doctor_mod.doctor(cwd, REPO, store, extra_scans=[bad],
                                     consolidate_flag=True)
        self.assertTrue(envelope.get("adopted"), envelope)
        self.assertEqual(envelope.get("tables_preserved"), [],
                         "a current-version candidate preserved tables:")
        for table in _V10_TABLES:
            self.assertEqual(_count(store, table), 2,
                             f"{table} was not replaced by the candidate's"
                             " rows")


class TextStorageClassContractTests(unittest.TestCase):
    """AC20: a divergent storage class in ANY TEXT column the adoption
    reads refuses the candidate instead of aborting the command (legacy,
    through the migration's v8->v9 text backfill) or being copied verbatim
    into the store's TEXT column (current version)."""

    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="zgt56-text-class-"))
        self.addCleanup(_rmtree, self.scratch)
        # The rule's own scope is derived, so it must stay derived: a table
        # dropped from `_ADOPTED_TABLES` silently re-admits a BLOB, and the
        # suite has to notice (round-7 reviewer finding 2). Guarded because
        # the symbol is this branch's own - at the frozen base the check's
        # BLOB shapes carry the RED evidence instead.
        adopted = getattr(doctor_mod, "_ADOPTED_TABLES", None)
        if adopted is not None:
            self.assertEqual(
                set(adopted),
                {t for t, _pk in doctor_mod._COPY_TABLES} | {"actors",
                                                             "sync_state"},
                "_ADOPTED_TABLES drifted from the copied tables plus actors"
                " and sync_state")

    def _pair(self, tag: str, diverge):
        root = self.scratch / tag
        good = _prep(root / "good" / "history.db")
        _seed_store(good, (1, 2), "2027-01-01T00:00:00Z")
        bad = _prep(root / "bad" / "history.db")
        _seed_store(bad, (5, 6), "2027-03-01T00:00:00Z")
        diverge(bad)
        return good, bad, _prep(root / "store" / "history.db"), _prep(
            root / "cwd")

    def _assert_isolated(self, envelope, bad: Path) -> None:
        entry = {os.path.normcase(str(r.get("path", ""))): r
                 for r in envelope.get("scanned") or []}.get(
                     os.path.normcase(str(bad)))
        self.assertIsNotNone(entry, "the divergent candidate vanished")
        self.assertTrue(entry.get("unreadable"), entry)
        self.assertTrue(entry.get("unreadable_reason"), entry)
        self.assertIsNone(envelope.get("adopted"), envelope)

    def test_blob_text_column_is_refused_not_aborted_or_adopted(self) -> None:
        """RED at base, GREEN after the fix. Legacy shapes: at base the
        migration's v8→v9 backfill reads the column with a str-only regex
        and raises an uncaught TypeError that aborts the whole command
        (probe-verified for comments.body, reviews.body and
        items.head_ref), and the v6→v7 rebuild joins `pr_files.path` into
        `items.paths_text` with `" ".join`, so a BLOB there aborts the same
        way (final critic finding 1: the whole `doctor` run died, healthy
        sibling included, while the current-version twin was refused
        cleanly). Current-version shape: at base the BLOB is copied
        verbatim into the store's TEXT column and read back as `blob`.
        Wrong-typed-payload shape: at base a `{"title": 5}` raw_json aborts
        the migration the same way; after the fix the field is coerced and
        the candidate is adopted with no references extracted."""
        for table, column in (("comments", "body"), ("reviews", "body"),
                              ("items", "head_ref"), ("pr_files", "path")):
            with self.subTest(shape="legacy", table=table, column=column):
                good, bad, store, cwd = self._pair(
                    f"c20-legacy-{table}-{column}",
                    lambda b, t=table, c=column: _blob_column_candidate(
                        b, t, c, legacy=True))
                try:
                    envelope = doctor_mod.doctor(cwd, REPO, store,
                                                 extra_scans=[good, bad],
                                                 consolidate_flag=True)
                except TypeError as exc:
                    self.fail(f"TypeError escaped candidate isolation and"
                              f" aborted the whole command: {exc!r}")
                self._assert_isolated(envelope, bad)

        with self.subTest(shape="current", table="comments", column="body"):
            good, bad, store, cwd = self._pair(
                "c20-current-comments-body",
                lambda b: _blob_column_candidate(b, "comments", "body",
                                                 legacy=False))
            try:
                envelope = doctor_mod.doctor(cwd, REPO, store,
                                             extra_scans=[good, bad],
                                             consolidate_flag=True)
            except Exception as exc:  # noqa: BLE001 - any escape is a failure
                self.fail(f"{type(exc).__name__} escaped candidate isolation"
                          f" and aborted the whole command: {exc!r}")
            self._assert_isolated(envelope, bad)
            # The refusal precedes any store write, so the store is never
            # created — the structural form of "the BLOB was not copied into
            # the store's TEXT column" (swarm-pr-review N-11: the previous
            # `if store.exists():` guard wrapped a query that could never
            # run).
            self.assertFalse(
                store.exists(),
                "the refusal path created the store before refusing, so 'no"
                " divergent storage class reached the store' is no longer"
                " structural")

        # A wrong-typed payload FIELD is the same "one candidate kills the
        # whole run" outcome one coercion away: `{"title": 5}` fed the
        # v8→v9 backfill's str-only regexes and aborted the migration. The
        # field is treated as EMPTY text, never coerced with str() — a
        # list's or dict's repr can contain backticked paths and `closes
        # #N`, and extracting references from it would let a malformed
        # payload write fabricated edges into the corpus (final critic,
        # round-7 question; swarm-pr-review N-03: the shape asserted only
        # that the candidate was adopted, so mutating the coercion to
        # str() left the whole suite green). The payload below carries a
        # repr with BOTH a backticked path and a closing keyword, so the
        # two coercions diverge observably and this shape pins the named
        # invariant.
        with self.subTest(shape="wrong_typed_raw_json"):
            good, bad, store, cwd = self._pair(
                "c20-wrong-typed-raw-json", _wrong_typed_raw_json_candidate)
            try:
                envelope = doctor_mod.doctor(cwd, REPO, store,
                                             extra_scans=[good, bad],
                                             consolidate_flag=True)
            except TypeError as exc:
                self.fail(f"TypeError escaped candidate isolation and"
                          f" aborted the whole command: {exc!r}")
            self.assertTrue(envelope.get("ok"), envelope)
            entry = {os.path.normcase(str(r.get("path", ""))): r
                     for r in envelope.get("scanned") or []}.get(
                         os.path.normcase(str(bad)))
            self.assertIsNotNone(entry, "the malformed-payload candidate"
                                       " vanished from the report")
            self.assertTrue(
                entry.get("adopted"),
                f"a malformed payload should degrade to 'no references"
                f" extracted', not to an unreadable candidate: {entry}")
            # THE INVARIANT: the malformed field yields no references. Under
            # a str() coercion the list's repr — which contains a backticked
            # path and `closes #1` — would fabricate an item_path_refs row
            # and a closes_keyword edge; under empty-text it yields none.
            # Adoption already proved the store was written, so its existence
            # is asserted rather than guarded: a guard here would silently
            # skip the invariant (cubic review, PR #58).
            self.assertTrue(
                store.exists(),
                "the candidate was adopted but no store was written")
            conn = sqlite3.connect(str(store))
            try:
                paths = conn.execute(
                    "SELECT COUNT(*) FROM item_path_refs WHERE repo = ?",
                    (REPO,)).fetchone()[0]
                fabricated = conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE repo = ?"
                    " AND rel = 'closes_keyword'", (REPO,)).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(
                paths, 0,
                "a malformed payload field fabricated item_path_refs"
                f" rows: {paths}")
            self.assertEqual(
                fabricated, 0,
                "a malformed payload field fabricated closes_keyword"
                f" edges: {fabricated}")


if __name__ == "__main__":
    unittest.main()
