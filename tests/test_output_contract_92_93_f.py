"""Shared runtime helpers for the 92-93 output-contract checks (no TestCase
here): the hermetic seed, the envelope parser, the gh seams, the dead-stdout
and dead-flush runners, the README fence extractor, the canonical
vocabulary + hint-shape checker, and the shared StoreCase base (moved out of
test_output_contract_92_93.py at the 500-line cap, round 3). Contract DATA
(pinned tables, snippets, the validator and its schema-defect falsifier)
lives in test_output_contract_92_93_e.py, which re-exports everything
defined here.
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import zaxbygraph

_WORKTREE = Path(__file__).resolve().parents[1]
if not Path(zaxbygraph.__file__).resolve().is_relative_to(_WORKTREE / "src"):
    raise AssertionError(
        f"OC-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src;<worktree>/tests"
    )

from fixtures import REPO, FakeGitHubSource, issue, pr_file, pull  # noqa: E402
from zaxbygraph.db import connect, init_schema  # noqa: E402
from zaxbygraph.sync import sync_repo  # noqa: E402

#: Contract constants the seam helpers need (re-exported through _e).
OTHER_REPO = "acme/other-never-synced"
RELEASE_TOKEN = "RELEASE-TOKEN-1234567890"



def seed_corpus(db_path: Path, slug: str = REPO) -> None:
    """A mixed corpus: open issues, a closed issue closed by a merged PR,
    two PRs sharing a file, and a needle item. Offline."""
    conn = connect(db_path)
    init_schema(conn)
    src = FakeGitHubSource()
    src.add_issue(issue(1, title="first issue", body="needle one", state="open"))
    src.add_issue(issue(2, title="second issue", body="see #1", state="closed"))
    src.add_pr(
        issue(3, title="touch store", body="fixes #2", kind="pr", state="closed"),
        pull(3, changed_files=2, merged=True),
        files=[pr_file("src/store.py"), pr_file("src/db.py")],
    )
    src.add_pr(
        issue(4, title="other pr", body="more files", kind="pr", state="closed"),
        pull(4, changed_files=1, merged=True),
        files=[pr_file("src/store.py")],
    )
    src.add_issue(issue(6, title="real work", body="mentions needle", state="open"))
    sync_repo(conn, src, slug)
    conn.close()



def seed_zero_repo_store(db_path: Path) -> None:
    """A valid schema with NO stored repo (the zero-repo freshness-fallback
    leg: a post-open no-filter failure has no single repo to report)."""
    conn = connect(db_path)
    try:
        init_schema(conn)
    finally:
        conn.close()



def seed_multi_repo_store(db_path: Path) -> None:
    """One store holding TWO distinct repos (the multi-repo freshness
    fallback leg: no single stored repo can be picked without guessing)."""
    seed_corpus(db_path, REPO)
    seed_corpus(db_path, "acme/second-repo")



class StoreCase(unittest.TestCase):
    """Temp store + pinned ZAXBYGRAPH_HOME/ZAXBYGRAPH_DB per test (the
    shared base of the 92-93 CLI check classes; moved here at the 500-line
    cap of test_output_contract_92_93.py, round 3)."""

    def setUp(self) -> None:
        from test_mcp import pin_env_for  # lazy: keep _f import-light

        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db = self.root / "history.db"
        seed_corpus(self.db)
        pin_env_for(self, self.root, db=self.db)
        self.base = ["--repo", REPO, "--db", str(self.db), "--format", "json"]


def resolve_leg_arg(part: str, substitutions: dict[str, str]) -> str:
    """One argv token, with the leg's __PLACEHOLDER__ substitutions."""
    return substitutions.get(part, part)




def parse_envelope(test, out: str, tag: str) -> dict:
    try:
        env = json.loads(out)
    except ValueError:
        test.fail(f"{tag}: stdout must parse as one JSON envelope; got {out[:200]!r}")
    test.assertIsInstance(env, dict, f"{tag}: the envelope must be a JSON object")
    return env



class DeadGh:
    """The dead gh seam the live claim failure legs run against (offline)."""

    def __call__(self, cmd):
        return (1, "", "gh: sweep seam is dead (no network in tests)")



def live_gh_seam(name: str):
    """A seeded successful gh for one live command's success leg; None for
    commands that never call gh."""
    if name not in ("claim", "release", "pr-guard"):
        return None
    from test_claims_ext import FakeGh

    fake = FakeGh()
    if name == "release":
        # releasing needs a claim to end; the sweep's token is fixed, so
        # the seeded claim carries exactly that token.
        fake.add_claim(1, RELEASE_TOKEN, hours_ago=2.0)
    return fake



def readme_json_fences(section: str) -> list:
    """Every fenced code block in a markdown section that parses as a JSON
    object (the README MCP-registration fidelity pins parse these)."""
    import re as _re

    out = []
    fence = "```" + r"\w*" + "\n"
    for body in _re.findall(fence + r"(.*?)" + "```", section, _re.S):
        try:
            parsed = json.loads(body)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out

# The pinned per-command production tables (documented per leg). The sweep
# classes in test_output_contract_92_93_c.py run these verbatim.

#: The canonical closed vocabularies (valid_values, independent of rows).
CANONICAL_VOCABULARIES = {
    "items.kind": ["issue", "pr"],
    "items.state": ["open", "closed"],
    "comments.kind": ["issue_comment", "review_comment"],
}


def hint_problems(hints, tag: str, column, literal) -> list:
    """Shape checks for the annotate-only hints block (OC-AC14 callers
    append the returned problems to their own list)."""
    out: list = []
    if not isinstance(hints, list) or not hints:
        return [f"{tag}: the out-of-vocabulary zero-row read must carry hints; got {hints!r}"]
    for hint in hints:
        if not isinstance(hint, dict):
            out.append(f"{tag}: each hint is an object; got {hint!r}")
            continue
        for key in ("column", "literal", "stored_values", "valid_values"):
            if key not in hint:
                out.append(f"{tag}: each hint must carry {key}; got {sorted(hint)}")
        if column is not None and hint.get("column") != column:
            out.append(f"{tag}: hint.column {hint.get('column')!r}, want {column!r}")
        if literal is not None and hint.get("literal") != literal:
            out.append(f"{tag}: hint.literal {hint.get('literal')!r}, want {literal!r}")
        valid = hint.get("valid_values")
        if column in CANONICAL_VOCABULARIES:
            if not isinstance(valid, list) or sorted(valid) != sorted(CANONICAL_VOCABULARIES[column]):
                out.append(
                    f"{tag}: valid_values must be the canonical set"
                    f" {sorted(CANONICAL_VOCABULARIES[column])} independent of stored rows; got {valid!r}"
                )
        elif not isinstance(valid, list):
            out.append(f"{tag}: valid_values must be a list; got {valid!r}")
        if not isinstance(hint.get("stored_values"), list):
            out.append(f"{tag}: stored_values must be a list; got {hint.get('stored_values')!r}")
    return out




def run_with_dead_stdout(argv: list[str]) -> tuple[int | None, str, str]:
    """cli.main(argv) in-process with cli._write_stdout raising
    BrokenPipeError (restored in finally, so no patch leaks across legs).
    Returns (exit-or-None-if-escaped, stderr, escaped-repr)."""
    from zaxbygraph import cli as cli_mod

    saved_write = cli_mod._write_stdout

    def _dead(text: str) -> None:
        raise BrokenPipeError("reader went away")

    code: int | None = None
    escaped: str | None = None
    err = io.StringIO()
    out = io.StringIO()
    try:
        cli_mod._write_stdout = _dead
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_mod.main(list(argv))
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
    except Exception as exc:  # BrokenPipeError must never escape main
        escaped = f"{type(exc).__name__}: {exc}"
    finally:
        cli_mod._write_stdout = saved_write
    return code, err.getvalue(), escaped or ""


class DeadFlushStream(io.StringIO):
    """A stdout stream the render succeeds into but whose flush() raises
    (the shutdown-flush dead-pipe shape; critic round 2, finding 1)."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def flush(self) -> None:
        raise self._exc


def run_with_dead_flush(argv: list[str], flush_exc: Exception):
    """cli.main(argv) with sys.stdout replaced by a DeadFlushStream (never a
    production edit). Returns (exit-or-None-if-escaped, stderr, escaped)."""
    from zaxbygraph import cli as cli_mod

    code: int | None = None
    escaped: str | None = None
    err = io.StringIO()
    try:
        with redirect_stdout(DeadFlushStream(flush_exc)), redirect_stderr(err):
            code = cli_mod.main(list(argv))
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
    except Exception as exc:  # the flush failure must never escape main
        escaped = f"{type(exc).__name__}: {exc}"
    return code, err.getvalue(), escaped or ""


def _declares_string(prop) -> bool:
    """True when a property declaration types its value as a string."""
    if not isinstance(prop, dict):
        return False
    names = prop.get("type")
    names = names if isinstance(names, list) else ([names] if names else [])
    return "string" in names


def _type_names(prop) -> list:
    names = prop.get("type") if isinstance(prop, dict) else None
    return names if isinstance(names, list) else ([names] if names else [])


def _hints_surface_problems(label: str, hints, tag: str) -> list:
    """Declaration problems for ONE surface's hints property (rounds 3-4):
    an optional array whose items are TYPED OBJECTS requiring column,
    literal, stored_values, valid_values — column/literal typed string,
    the other two typed arrays of string items; hints.items itself must
    declare type object (round 4, finding 5)."""
    out: list = []
    if not isinstance(hints, dict) or not hints:
        return [f"{tag}: {label} must DECLARE an optional hints property; got {hints!r}"]
    if "array" not in _type_names(hints):
        out.append(f"{tag}: {label}: hints must be declared an array; got {hints.get('type')!r}")
    items = hints.get("items")
    if not isinstance(items, dict) or not items:
        out.append(f"{tag}: {label}: hints.items must declare the hint objects; got {items!r}")
        return out
    if "object" not in _type_names(items):
        out.append(
            f"{tag}: {label}: hints.items must be TYPED an OBJECT (round 4);"
            f" got {items.get('type')!r}"
        )
    required = items.get("required") or []
    props = items.get("properties") or {}
    for key in ("column", "literal", "stored_values", "valid_values"):
        if key not in required:
            out.append(f"{tag}: {label}: hints.items.required must carry {key}; got {required!r}")
        prop = props.get(key)
        if not isinstance(prop, dict) or not prop:
            out.append(f"{tag}: {label}: hints.items.properties.{key} must be declared; got {prop!r}")
            continue
        if key in ("column", "literal"):
            if not _declares_string(prop):
                out.append(f"{tag}: {label}: hints.items.properties.{key} must be TYPED a string (round 3); got {prop!r}")
        else:
            if "array" not in _type_names(prop):
                out.append(f"{tag}: {label}: hints.items.properties.{key} must be TYPED an array (round 3); got {prop!r}")
            elif not _declares_string(prop.get("items")):
                out.append(
                    f"{tag}: {label}: hints.items.properties.{key}.items must"
                    f" type its entries as strings (round 3); got {prop.get('items')!r}"
                )
    return out


def hints_declaration_problems(test, tag: str) -> list:
    # run_cli arrives via the caller's module; import lazily to keep _f free
    # of the heavier test_mcp import at collection time.
    from test_mcp import run_cli

    """Both the emitted CLI `schema --output sql` schema and the MCP sql
    tool outputSchema must declare an OPTIONAL hints array property whose
    items are typed objects requiring column, literal, stored_values,
    valid_values (each typed; round 4: hints.items typed object). Round 4
    negative self-tests ride along: a TYPELESS hints.items is a declaration
    defect, and the typed items schema must REJECT a scalar hint row."""
    problems: list = []
    import zaxbygraph.mcp_server as mcp_server

    schemas = {}
    code, out, err = run_cli(["schema", "--output", "sql", *test.base])
    try:
        schemas["CLI schema --output sql"] = ((json.loads(out).get("data") or {}).get("schema") or {})
    except ValueError:
        problems.append(f"{tag}: schema --output sql stdout does not parse: {out[:120]!r}")
    schemas["MCP sql outputSchema"] = mcp_server._output_schema_for("sql")
    for label, schema in schemas.items():
        hints = (((schema.get("properties") or {}).get("data") or {}).get("properties") or {}).get("hints")
        problems += _hints_surface_problems(label, hints, tag)
    # ROUND 4 (finding 5) negative self-tests.
    typed_items = {
        "type": "object",
        "required": ["column", "literal", "stored_values", "valid_values"],
        "properties": {
            "column": {"type": "string"},
            "literal": {"type": "string"},
            "stored_values": {"type": "array", "items": {"type": "string"}},
            "valid_values": {"type": "array", "items": {"type": "string"}},
        },
    }
    typeless = {"type": "array", "items": {k: v for k, v in typed_items.items() if k != "type"}}
    if not any("TYPED an OBJECT" in p for p in _hints_surface_problems("selftest", typeless, tag)):
        problems.append(f"{tag}: SELF-TEST: a TYPELESS hints.items must be flagged (round 4)")
    from test_output_contract_92_93_e import validate  # lazy: _e imports _f

    errors: list = []
    validate(
        {"type": "object", "properties": {"hints": {"type": "array", "items": typed_items}}},
        {"hints": [42]},
        "selftest",
        errors,
    )
    if not errors:
        problems.append(f"{tag}: SELF-TEST: the typed items schema must REJECT a scalar hint row (hints: [42])")
    return problems


def dead_flush_and_success_render_problems(base: list[str]) -> list[str]:
    """The dead-FLUSH legs plus the successful dead-RENDER leg (round 3,
    finding 4b): a failing command whose render succeeds but whose
    sys.stdout.flush() raises BrokenPipeError / OSError(EINVAL) returns the
    command's OWN code; a successful command returns 0 under a dead flush;
    and a SUCCESSFUL command whose stdout WRITE dies (the _write_stdout
    seam) still returns exit 0 — a mutant answering a synthetic nonzero
    code (e.g. 7) on the successful dead render must fail here."""
    import errno

    problems: list[str] = []
    for label, argv, want_code, flush_exc in (
        ("flush on failing command (BrokenPipeError)", ["item", "99999", *base], 1, BrokenPipeError("dead pipe")),
        ("flush on failing command (OSError EINVAL)", ["item", "99999", *base], 1, OSError(errno.EINVAL, "dead pipe")),
        ("flush on successful command", ["where", *base], 0, BrokenPipeError("dead pipe")),
    ):
        code, err, escaped = run_with_dead_flush(argv, flush_exc)
        if escaped:
            problems.append(f"{label}: the flush failure escaped main ({escaped})")
        elif code != want_code:
            problems.append(f"{label}: a failing stdout FLUSH must return the command's own code {want_code}, got {code}")
        if "Traceback" in err:
            problems.append(f"{label}: the flush failure must stay quiet (no traceback)")
    code, err, escaped = run_with_dead_stdout(["where", *base])
    if escaped:
        problems.append(f"successful dead render: BrokenPipeError escaped main ({escaped})")
    elif code != 0:
        problems.append(
            f"successful dead render: a successful command whose stdout write"
            f" dies must still exit 0, got {code}"
        )
    if "Traceback" in err:
        problems.append("successful dead render: the dead render must stay quiet (no traceback)")
    return problems


#: The C1 dead-pipe failure-emitter matrix (moved out of
#: test_output_contract_92_93.py at its 500-line cap, round 3): (label,
#: command, args before the common flags, args after them, expected own
#: exit code, expected error code, gh seam needed). Post-common args
#: override --repo/--db (argparse last-wins) for the no-corpus leg.
DEAD_PIPE_FAILURES = [
    ("item ghost (not_found)", "item", ["99999"], [], 1, "not_found", False),
    ("usage error (item with no number)", "item", [], [], 2, "bad_request", False),
    ("sql guard refusal (bad_sql)", "sql", ["DELETE FROM items"], [], 2, "bad_sql", False),
    ("sql no_such_table", "sql", ["SELECT * FROM nope_table"], [], 1, "no_such_table", False),
    ("snapshot pull, unreachable source", "snapshot", ["pull", "__SNAP__"], [], 1, "source_unreachable", False),
    ("no-corpus read", "item", ["1"], ["--repo", "acme/nosuch", "--db", "__MISSING__"], 3, "no_corpus", False),
    ("dead-gh claim failure", "claim", ["1", "--settle-seconds", "0.05"], [], 6, "github_unreachable", True),
]


#: Round-3/4 negative-selftest fixtures: schemas that are VALID under the
#: amended falsifier (defect-free), so removing exactly one feature at a
#: time proves the falsifier catches each named mutation. They live here
#: (round 4: displaced from _e.py at its 500-line cap) and are re-exported
#: through _e.
_VALID_ERROR = {
    "type": ["object", "null"],
    "required": ["code", "message"],
    "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
}
_VALID_BASE_PROPS = {
    "ok": {"type": "boolean"},
    "db": {"type": ["string", "null"]},
    "repo": {"type": ["string", "null"]},
    "freshness": {"type": ["object", "null"]},
    "truncated": {"type": "boolean"},
    "version": {"type": "string"},
    "error": _VALID_ERROR,
}
VALID_OBJECT_SCHEMA = {
    "type": "object",
    "required": ["ok", "db", "repo", "freshness", "data", "truncated", "version"],
    "properties": {
        **_VALID_BASE_PROPS,
        "data": {
            "type": ["object", "null"],
            "properties": {"number": {"type": "integer"}, "title": {"type": "string"}},
        },
    },
}
VALID_LIST_SCHEMA = {
    "type": "object",
    "required": ["ok", "db", "repo", "freshness", "data", "truncated", "version"],
    "properties": {
        **_VALID_BASE_PROPS,
        "data": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "prs": {"type": "array", "items": {"type": "integer"}},
                },
            },
        },
    },
}
#: ROUND 4 (finding 1): the item-like data union — object | array | null —
#: with TYPED row objects. Removing only the items type must be a defect,
#: and the typed union must REJECT a scalar data row ([42]).
VALID_ITEM_UNION_SCHEMA = {
    "type": "object",
    "required": ["ok", "db", "repo", "freshness", "data", "truncated", "version"],
    "properties": {
        **_VALID_BASE_PROPS,
        "data": {
            "type": ["object", "array", "null"],
            "properties": {"number": {"type": "integer"}},
            "items": {
                "type": "object",
                "properties": {"number": {"type": "integer"}},
            },
        },
    },
}


class NoopRunner:
    """The injected sync seam every offline MCP session uses."""

    def __call__(self, db_path, repo) -> None:
        return None
