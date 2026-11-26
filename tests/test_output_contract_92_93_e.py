"""Shared infrastructure for the 92-93 output-contract checks (no frozen
argv TestCase here; the sweep classes keep their frozen dotted ids inside
test_output_contract_92_93_c.py).

Contents: the union-aware validator (unrecognized type names are ERRORS),
the schema-defect falsifier (a vacuous emitted schema is a defect, never a
pass; round 3: the error object required unconditionally, list rows typed
object; round 4: items typed object whenever the data schema permits an
array, array properties must type their items), the negative-selftest
helpers, and the pinned per-command production tables. The hermetic seed,
the envelope parser, the gh seams, the dead-stream runners and the
valid-schema fixtures live in test_output_contract_92_93_f.py and are
re-exported below. Hermetic, offline; failures carry OC-ACn tags.
"""

from __future__ import annotations

import io
import json
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

from fixtures import REPO  # noqa: E402  (the sweep tables key on the slug)
from test_output_contract_92_93_f import (  # noqa: E402,F401  (re-exported helpers)
    CANONICAL_VOCABULARIES, DEAD_PIPE_FAILURES, OTHER_REPO, RELEASE_TOKEN,
    VALID_ITEM_UNION_SCHEMA, VALID_LIST_SCHEMA, VALID_OBJECT_SCHEMA,
    DeadFlushStream, DeadGh, StoreCase, dead_flush_and_success_render_problems,
    hint_problems, live_gh_seam, parse_envelope, readme_json_fences,
    resolve_leg_arg, run_with_dead_flush, run_with_dead_stdout, seed_corpus,
    seed_multi_repo_store, seed_zero_repo_store,
)

# The union-aware validator, HARDENED (critic round 1): an unrecognized
# JSON-Schema `type` name is an ERROR, never a silent pass.

_KNOWN_TYPES = frozenset(
    {"object", "array", "string", "integer", "boolean", "number", "null"}
)

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def _type_ok(declared: str, value) -> bool:
    check = _TYPE_CHECKS.get(declared)
    if check is None:
        return False  # an unrecognized type name matches NOTHING
    return check(value)


def validate(schema, value, path: str, errors: list) -> None:
    """Minimal stdlib JSON-schema checker: unions, properties, required,
    items — and unknown type names are errors."""
    if not isinstance(schema, dict):
        errors.append(f"{path}: schema node is not an object: {schema!r}")
        return
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        unknown = [name for name in names if name not in _KNOWN_TYPES]
        if unknown:
            errors.append(f"{path}: unrecognized schema type name(s) {unknown}")
            return
        if not any(_type_ok(name, value) for name in names):
            errors.append(f"{path}: expected {declared}, got {type(value).__name__}")
            return
    if isinstance(value, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, sub in properties.items():
                if key in value:
                    validate(sub, value[key], f"{path}.{key}", errors)
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, element in enumerate(value):
                validate(items, element, f"{path}[{index}]", errors)


# The schema-defect falsifier (critic round 1): an emitted full-envelope
# schema that is vacuous anywhere is a DEFECT — an empty declaration can
# never validate a real envelope green.

ENVELOPE_REQUIRED = ("ok", "db", "repo", "freshness", "data", "truncated", "version")


def _real_declaration(prop) -> bool:
    """A property declaration that actually constrains something."""
    if not isinstance(prop, dict) or not prop:
        return False
    if "type" in prop:
        names = prop["type"] if isinstance(prop["type"], list) else [prop["type"]]
        return bool(names) and all(name in _KNOWN_TYPES for name in names)
    return bool(prop.get("properties")) or bool(prop.get("items"))


def _string_property(props: dict, key: str) -> bool:
    """True when props[key] declares a string type (present)."""
    prop = props.get(key)
    if not isinstance(prop, dict) or "type" not in prop:
        return False
    names = prop["type"] if isinstance(prop["type"], list) else [prop["type"]]
    return "string" in names


def _array_items_untyped(prop) -> bool:
    """True when an array-permitting declaration does not type its items."""
    if not isinstance(prop, dict) or "array" not in (
        prop["type"] if isinstance(prop.get("type"), list) else [prop.get("type")]
    ):
        return False
    items = prop.get("items")
    return not isinstance(items, dict) or not _real_declaration(items)


def schema_defects(schema, command: str, declared_data_keys, data_kind: str) -> list:
    """Every reason `schema` is too vacuous to validate `command`'s real
    envelopes: empty required/properties, missing envelope keys, a missing
    or malformed error object (round 3: required UNCONDITIONALLY, nested
    required string code/message, optional string hint), empty data_keys,
    or a data declaration not naming the declared keys/rows with real
    types. ROUND 4: whenever the data schema permits an array or carries
    items — regardless of data_kind — data.items must type its rows
    object, and declared array properties must type their items."""
    defects: list = []
    if not isinstance(schema, dict):
        return [f"{command}: the emitted schema is not an object"]
    if not isinstance(declared_data_keys, (list, tuple)) or not declared_data_keys:
        defects.append(f"{command}: data_keys is empty — the payload's always-present keys are not declared")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        defects.append(f"{command}: schema.properties is empty or missing")
        properties = {}
    required = schema.get("required")
    if not isinstance(required, list) or not required:
        defects.append(f"{command}: schema.required is empty or missing")
        required = []
    missing = [key for key in ENVELOPE_REQUIRED if key not in required]
    if missing:
        defects.append(f"{command}: required lacks the envelope keys {missing}")
    for key in ENVELOPE_REQUIRED:
        if not _real_declaration(properties.get(key)):
            defects.append(
                f"{command}: properties.{key} is not a real declaration: {properties.get(key)!r}"
            )
    # The error object (round 3): REQUIRED unconditionally — failure
    # envelopes validate against the SAME schema: an object whose required
    # carries code/message, typed strings (hint optional string).
    error = properties.get("error")
    if not isinstance(error, dict) or not error:
        defects.append(
            f"{command}: properties.error must declare a non-empty error object"
            f" with required string code/message; got {error!r}"
        )
    else:
        enames = error.get("type")
        enames = enames if isinstance(enames, list) else ([enames] if enames else [])
        if "object" not in enames:
            defects.append(f"{command}: properties.error must declare an object; got {error.get('type')!r}")
        ereq = error.get("required") or []
        for key in ("code", "message"):
            if key not in ereq:
                defects.append(f"{command}: properties.error.required must carry {key!r}; got {ereq!r}")
        eprops = error.get("properties")
        if not isinstance(eprops, dict) or not eprops:
            defects.append(f"{command}: properties.error.properties does not declare code/message")
        else:
            for key in ("code", "message"):
                if not _string_property(eprops, key):
                    defects.append(f"{command}: properties.error.{key} must be declared a string; got {eprops.get(key)!r}")
            if "hint" in eprops and not _string_property(eprops, "hint"):
                defects.append(f"{command}: properties.error.hint (optional) must be a string; got {eprops.get('hint')!r}")
    data = properties.get("data")
    if not isinstance(data, dict) or not data:
        defects.append(f"{command}: properties.data is not a declaration")
        return defects
    names = data.get("type")
    names = names if isinstance(names, list) else ([names] if names else [])
    # ROUND 4: the items rule is data_kind-independent (the item union).
    if "array" in names or "items" in data:
        items = data.get("items")
        if not isinstance(items, dict) or not items:
            defects.append(
                f"{command}: the data schema permits an array but data.items"
                " does not declare the row objects"
            )
        else:
            inames = items.get("type")
            inames = inames if isinstance(inames, list) else ([inames] if inames else [])
            if "object" not in inames:
                defects.append(
                    f"{command}: data.items must declare its rows as type object"
                    f" (round 4: whenever the data schema permits an array or"
                    f" carries items, regardless of data_kind); got"
                    f" {items.get('type')!r}"
                )
    if data_kind == "list":
        if "array" not in names:
            defects.append(
                f"{command}: a list-kind payload must declare data as an array; declared type {data.get('type')!r}"
            )
        row_props = (data.get("items") or {}) if isinstance(data.get("items"), dict) else {}
        row_props = row_props.get("properties")
        if not isinstance(row_props, dict) or not row_props:
            defects.append(
                f"{command}: data.items.properties is empty — the declared row"
                " fields are not described with real types"
            )
        else:
            untyped_arrays = [
                key for key in declared_data_keys
                if _array_items_untyped(row_props.get(key))
            ]
            if untyped_arrays:
                defects.append(
                    f"{command}: data.items.properties permits arrays without"
                    f" typing their items (round 4 sweep): {untyped_arrays}"
                )
            undescribed = [
                key for key in declared_data_keys
                if not _real_declaration(row_props.get(key))
            ]
            if undescribed:
                defects.append(
                    f"{command}: data.items.properties does not describe the"
                    f" declared row fields {undescribed} with real types"
                )
    else:
        if "object" not in names:
            defects.append(
                f"{command}: an object-kind payload must declare data as an object; declared type {data.get('type')!r}"
            )
        data_props = data.get("properties")
        if not isinstance(data_props, dict) or not data_props:
            defects.append(
                f"{command}: data.properties is empty — the declared data keys are not described with real types"
            )
        else:
            untyped_arrays = [
                key for key in declared_data_keys
                if _array_items_untyped(data_props.get(key))
            ]
            if untyped_arrays:
                defects.append(
                    f"{command}: data.properties permits arrays without typing"
                    f" their items (round 4 sweep): {untyped_arrays}"
                )
            undescribed = [
                key for key in declared_data_keys
                if not _real_declaration(data_props.get(key))
            ]
            if undescribed:
                defects.append(
                    f"{command}: data.properties does not describe the declared"
                    f" data keys {undescribed} with real types"
                )
    return defects



#: The round-3/4 valid-schema fixtures live in _f.py (displaced at the
#: 500-line cap) and are re-exported above.


def data_keys_sweep_problems(command: str, declared, real_keys, allowlist) -> list:
    """The real-vs-declared data_keys comparison core (round 3): declared
    keys the real payload never carries are phantom drift; real keys
    outside the declaration AND the documented conditional allowlist are
    undeclared drift. Runs on the EMITTED `schema --output` data_keys."""
    declared = set(declared or ())
    real = set(real_keys or ())
    out: list = []
    phantom = sorted(declared - real)
    if phantom:
        out.append(f"{command}: declared data_keys the real payload never carries: {phantom}")
    stray = sorted((real - declared) - set(allowlist or ()))
    if stray:
        out.append(
            f"{command}: real payload keys outside the declared data_keys"
            f" and outside the documented conditional allowlist"
            f" {sorted(allowlist or ())}: {stray}"
        )
    return out


def amendment_selftest_problems() -> list:
    """Round-3/4 negative self-tests: each starts from a VALID schema and
    removes ONLY the feature under test (missing error; row type removed,
    list or item-union; truncated data_keys; untyped array property), and
    the typed item union must REJECT a scalar data row ([42])."""
    problems: list = []
    if schema_defects(VALID_OBJECT_SCHEMA, "selftest", ["number", "title"], "object"):
        problems.append("fixture sanity: VALID_OBJECT_SCHEMA must be defect-free")
    if schema_defects(VALID_LIST_SCHEMA, "selftest", ["path", "prs"], "list"):
        problems.append("fixture sanity: VALID_LIST_SCHEMA must be defect-free")
    if schema_defects(VALID_ITEM_UNION_SCHEMA, "selftest", ["number"], "object"):
        problems.append("fixture sanity: VALID_ITEM_UNION_SCHEMA must be defect-free")
    obj_props = VALID_OBJECT_SCHEMA["properties"]
    list_data = VALID_LIST_SCHEMA["properties"]["data"]
    union_data = VALID_ITEM_UNION_SCHEMA["properties"]["data"]
    no_error = {**VALID_OBJECT_SCHEMA, "properties": {k: v for k, v in obj_props.items() if k != "error"}}
    row_typeless = {**VALID_LIST_SCHEMA, "properties": {**VALID_LIST_SCHEMA["properties"], "data": {**list_data, "items": {k: v for k, v in list_data["items"].items() if k != "type"}}}}
    union_typeless = {**VALID_ITEM_UNION_SCHEMA, "properties": {**VALID_ITEM_UNION_SCHEMA["properties"], "data": {**union_data, "items": {k: v for k, v in union_data["items"].items() if k != "type"}}}}
    array_untyped = {**VALID_LIST_SCHEMA, "properties": {**VALID_LIST_SCHEMA["properties"], "data": {**list_data, "items": {**list_data["items"], "properties": {"path": {"type": "string"}, "prs": {"type": "array"}}}}}}
    for note, mutated, keys, kind, needle in (
        ("missing error", no_error, ["number", "title"], "object", "error"),
        ("row type removed", row_typeless, ["path", "prs"], "list", "type object"),
        ("item-union row type removed", union_typeless, ["number"], "object", "type object"),
        ("untyped array property", array_untyped, ["path", "prs"], "list", "without typing their items"),
    ):
        defects = schema_defects(mutated, "selftest", keys, kind)
        if not defects or not any(needle in d for d in defects):
            problems.append(f"a schema with the {note} mutation must be rejected naming {needle!r}; got {defects!r}")
    # The typed union must REJECT a scalar data row — the load-bearing
    # half of the round-4 items.type requirement.
    scalar_row_env = {
        "ok": True, "db": "x", "repo": "o/r", "freshness": {},
        "data": [42], "truncated": False, "version": "0.3",
    }
    errors: list = []
    validate(VALID_ITEM_UNION_SCHEMA, scalar_row_env, "selftest", errors)
    if not any("data[0]" in e for e in errors):
        problems.append(f"the TYPED item union must REJECT a scalar data row ([42]); got {errors!r}")
    if data_keys_sweep_problems("selftest", ["number", "title"], {"number", "title"}, frozenset()):
        problems.append("fixture sanity: the full declaration must sweep clean")
    if not data_keys_sweep_problems("selftest", ["number"], {"number", "title"}, frozenset()):
        problems.append("a TRUNCATED declared data_keys list must be sweep drift")
    if not data_keys_sweep_problems("selftest", ["number", "ghost"], {"number"}, frozenset()):
        problems.append("a phantom declared key must be sweep drift")
    return problems


def wrong_envelope_rejections() -> list:
    """The validator self-test against the REAL registry schema
    (graph_status): every obviously wrong envelope must be REJECTED."""
    import zaxbygraph.mcp_server as mcp_server

    real_schema = mcp_server._output_schema_for("graph_status")
    ok_envelope = {"ok": True, "db": "x", "repo": "o/r", "freshness": {},
                   "data": {"repos": []}, "truncated": False, "version": "0.3"}
    problems: list = []
    for label, wrong in (
        ("missing required keys", {"ok": True, "db": "x"}),
        ("wrong ok type", {**ok_envelope, "ok": "yes"}),
        ("wrong data type", {**ok_envelope, "data": 42}),
        ("wrong truncated type", {**ok_envelope, "truncated": "no"}),
        ("error not an object", {**ok_envelope, "error": 42}),
    ):
        errors: list = []
        validate(real_schema, wrong, "selftest", errors)
        if not errors:
            problems.append(f"the validator must reject the obviously wrong envelope ({label}); it passed!")
    return problems


SUCCESS_CALLS: dict[str, tuple[list[str], bool, str]] = {
    "status": ([], False, "plain read"),
    "search": (["needle"], False, "fts hit"),
    "item": (["1"], False, "single-number item object"),
    "related": (["3"], False, "1-hop neighborhood"),
    "path": (["3", "2"], False, "connected pair"),
    "overlap": (["3", "4"], False, "shared-file PRs"),
    "file-history": (["src/store.py"], False, "stored path"),
    "what-closed": (["2"], False, "closed issue"),
    "sql": (["SELECT number, kind FROM items ORDER BY number"], False, "in-vocabulary read"),
    "similar": (["6"], False, "derived analogs"),
    "triage": ([], False, "derived ranking"),
    "bundle": ([], False, "derived bundles"),
    "brief": (["1"], False, "work packet"),
    "churn": ([], False, "file ranking"),
    "open": ([], False, "bare row list"),
    "claims": ([], False, "derived claim listing"),
    "claim": (["1", "--settle-seconds", "0.05"], False, "live claim through the FakeGh seam"),
    "release": (["1", "--token", RELEASE_TOKEN], False, "live release through the FakeGh seam"),
    "pr-guard": (["1"], False, "live guard through the FakeGh seam"),
}


#: The CLI-only extra shapes (critic round 1 finding 2): the bare-list
#: `open` payload and the multi-number `item` answer (one row per number,
#: unstored numbers as per-row not_found rows) validate against the SAME
#: emitted schemas their registry entries declare.
ITEM_MULTI_TAIL = ["1", "99999"]

#: The pinned FAILURE legs: per command, a list of (args before the common
#: flags, repo override after them, note, data expectation). The data
#: expectation distinguishes LIVE claim outcomes (github_unreachable through
#: the dead gh seam: the outcome IS the answer, so the envelope keeps its
#: data dict -- the documented FB-008 contract) from pre-flight refusals
#: (missing token, unknown repo, guard refusal: data:null).
FAILURE_CALLS: dict[str, list[tuple[list[str], bool, str, str]]] = {
    "item": [(["99999"], False, "ghost number -> not_found", "null")],
    "related": [(["99999"], False, "ghost number -> not_found", "null")],
    "path": [(["99999", "1"], False, "ghost number -> not_found", "null")],
    "overlap": [(["99999", "3"], False, "ghost number -> not_found", "null")],
    "what-closed": [(["99999"], False, "ghost number -> not_found", "null")],
    "similar": [(["99999"], False, "ghost number -> not_found", "null")],
    "brief": [(["99999"], False, "ghost number -> not_found", "null")],
    "file-history": [(["src/nothing-here.py"], False, "unknown path -> unknown_path", "null")],
    "sql": [(["DELETE FROM items"], False, "guard refusal -> bad_sql (pre-flight)", "null")],
    "status": [([], True, "unknown --repo on the seeded --db (pre-flight refusal)", "null")],
    "search": [([], True, "unknown --repo on the seeded --db (pre-flight refusal)", "null")],
    "open": [([], True, "unknown --repo on the seeded --db (pre-flight refusal)", "null")],
    "triage": [([], True, "unknown --repo on the seeded --db (pre-flight refusal)", "null")],
    "bundle": [([], True, "unknown --repo on the seeded --db (pre-flight refusal)", "null")],
    "churn": [([], True, "unknown --repo on the seeded --db (pre-flight refusal)", "null")],
    "claims": [
        ([], True, "unknown --repo (pre-flight refusal, data null)", "null"),
        (["--live"], False, "dead gh seam -> github_unreachable LIVE outcome, data preserved", "dict"),
    ],
    "release": [
        (["1"], False, "release without --token -> bad_request (pre-flight, data null)", "null"),
        (["1", "--token", RELEASE_TOKEN], False, "dead gh seam -> github_unreachable LIVE outcome, data preserved", "dict"),
    ],
    "claim": [(["1", "--settle-seconds", "0.05"], False, "dead gh seam -> github_unreachable LIVE outcome, data preserved", "dict")],
    "pr-guard": [(["1"], False, "dead gh seam -> github_unreachable LIVE outcome, data preserved", "dict")],
}

#: The live-claim failure legs run against the dead gh seam.
LIVE_GH_FAILURE_LEGS = frozenset(
    {cmd for cmd, legs in FAILURE_CALLS.items() if any(kind == "dict" for _t, _o, _n, kind in legs)}
)

#: Real-minus-declared coverage for EVERY command in CLI_OUTPUT_SHAPES
#: (critic round 2, finding 6-iv): every key the real success payload
#: carries must be declared in data_keys, or belong to this command's
#: explicit allowlist of DOCUMENTED conditional keys (default: empty).
#: Justifications, each an optional property of that command's data
#: schema: sql keeps `warnings` (#71 pass), `hints` (the zero-row
#: annotation) and `total_rows` (over-budget count); status keeps
#: `sync_lock`/`jobs` (present only while a sync runs); the live claim
#: outcomes keep their evidence keys (login_probes/posts/token, released,
#: named/open_prs_checked, claims/stale_claims/truncated) — the claims
#: listings stay ABSENT rather than fabricated. Every other command pins
#: an EMPTY allowlist: its plain success payload must be declared exactly.
REAL_MINUS_DECLARED_ALLOWLIST: dict[str, frozenset] = {
    "brief": frozenset(),  # the brief leg pins declared == the real always-present set
    "bundle": frozenset(),
    "churn": frozenset(),
    "claims": frozenset({"claims", "stale_claims", "truncated"}),
    "claim": frozenset({"login_probes", "posts", "token"}),
    "file-history": frozenset(),
    "item": frozenset(),  # single-number form exact; multi rows allow ITEM_MULTI_EXTRA
    "open": frozenset(),
    "overlap": frozenset(),
    "path": frozenset(),
    "pr-guard": frozenset({"named", "open_prs_checked"}),
    "related": frozenset(),
    "release": frozenset({"released"}),
    "search": frozenset(),
    "similar": frozenset(),
    "sql": frozenset({"hints", "total_rows", "warnings"}),
    "status": frozenset({"sync_lock", "jobs"}),
    "triage": frozenset(),
    "what-closed": frozenset(),
}

#: The multi-number item rows carry the documented per-row not_found
#: markers beside the declared row fields.
ITEM_MULTI_EXTRA = frozenset({"not_found", "error"})

#: The EXACT registration snippets and config paths (pinned contract data;
#: TestAc6 compares production against these, never the reverse).
CLAUDE_SNIPPET = "claude mcp add zaxbygraph -- zaxbygraph mcp"
ZCODE_CONFIG = {
    "mcp": {
        "servers": {
            "zaxbygraph": {"command": "zaxbygraph", "args": ["mcp"], "type": "stdio"}
        }
    }
}
OPENCODE_CONFIG = {
    "mcp": {
        "servers": {"zaxbygraph": {"type": "local", "command": ["zaxbygraph", "mcp"]}}
    }
}
ZCODE_CONFIG_PATH = "~/.zcode/cli/config.json"
OPENCODE_CONFIG_PATH = "opencode.json"

