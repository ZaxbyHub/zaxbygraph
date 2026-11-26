"""Acceptance checks for issue-trace 92-93-output-contract-honesty (issues
#92+#93, decision D8 / AC10+AC11: every command's declared schema matches
its REAL envelopes), STRENGTHENED after plan-critic rounds 1-3.

The two sweep classes keep their frozen dotted ids here; the shared
infrastructure (validator, schema-defect falsifier, round-3 valid-schema
templates + negative-selftest helpers, production tables) lives in
tests/test_output_contract_92_93_e.py.

Decisions pinned here (D8, strengthened; round 3 in bold):

1. Validator hardening: unrecognized JSON-Schema `type` names are ERRORS
   (never a silent pass), and every command's emitted schema must be
   non-empty — `required` carries ok/db/repo/freshness/data/truncated/
   version, `properties` declares each envelope key with a real type,
   **properties.error is a non-empty object declaration UNCONDITIONALLY
   (required string code/message, optional string hint)**, and
   `properties.data` names the declared data keys with real types (object
   kind) or declares real row items (list kind, **rows typed object**).
2. Validator SELF-TEST legs: a deliberately vacuous schema, a schema
   validating an obviously wrong envelope, and — round 3 — a VALID schema
   with exactly one feature removed (missing error; row type removed;
   truncated emitted data_keys) MUST be rejected.
3. For EVERY command in CLI_OUTPUT_SHAPES (non-empty asserted first): the
   **EMITTED `schema --output` data_keys** (compared against the
   CLI_OUTPUT_SHAPES registry declaration — drift between the two is a
   defect) must be a subset of the keys the real success payload carries,
   with the real-minus-declared set empty or within the explicit allowlist
   of DOCUMENTED conditional keys pinned in _e (sql: warnings / hints /
   total_rows; status: sync_lock / jobs; claim outcomes: their evidence
   keys; others: none).
4. CLI-specific shapes: `open` (bare list payload) and `item` with a
   single number AND with MULTIPLE numbers including a ghost number
   (per-row not_found rows) validate against the SAME emitted schemas.
5. The failure sweep validates, for every command, a real failure
   envelope's ok:false, error.code (string), error.message (string) and
   version — plus data:null EXCEPT the live claim commands whose
   documented data-preserving outcome contract keeps a dict (FB-008).
6. brief's declared data_keys AND the emitted schema's required data keys
   both equal the real always-present set (author, created_at, html_url,
   labels, updated_at included). data_kind_multi is NOT part of the
   contract (the plan dropped that key).

Hermetic: FakeGitHubSource, tempfile stores, patched gh seams; no network,
no gh, no writes outside temp roots. Stdlib only; unittest. Every failure
message carries a distinctive single-line tag (OC-AC10 / OC-AC11).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import zaxbygraph

_WORKTREE = Path(__file__).resolve().parents[1]
if not Path(zaxbygraph.__file__).resolve().is_relative_to(_WORKTREE / "src"):
    raise AssertionError(
        f"OC-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src;<worktree>/tests"
    )

from fixtures import REPO  # noqa: E402
from test_mcp import pin_env_for, run_cli  # noqa: E402
from test_output_contract_92_93_e import (  # noqa: E402
    FAILURE_CALLS,
    ITEM_MULTI_EXTRA,
    ITEM_MULTI_TAIL,
    REAL_MINUS_DECLARED_ALLOWLIST,
    SUCCESS_CALLS,
    DeadGh,
    amendment_selftest_problems,
    data_keys_sweep_problems,
    parse_envelope,
    schema_defects,
    seed_corpus,
    validate,
    wrong_envelope_rejections,
)


class _SweepCase(unittest.TestCase):
    """One seeded store per test; the shared sweep helpers."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db = self.root / "history.db"
        seed_corpus(self.db)
        pin_env_for(self, self.root, db=self.db)
        self.base = ["--repo", REPO, "--db", str(self.db), "--format", "json"]

    def _registry(self) -> dict:
        import zaxbygraph.mcp_server as mcp_server

        registry = getattr(mcp_server, "CLI_OUTPUT_SHAPES", None)
        self.assertIsInstance(
            registry, dict,
            "OC-AC10: mcp_server.CLI_OUTPUT_SHAPES must be the command shape registry",
        )
        self.assertGreater(
            len(registry), 0,
            "OC-AC10: the CLI_OUTPUT_SHAPES registry must be non-empty before"
            " the per-command schema sweep",
        )
        return registry

    def _declared_schema(self, command: str, tag: str = "OC-AC10") -> dict:
        code, out, err = run_cli(["schema", "--output", command, *self.base])
        try:
            env = json.loads(out)
        except ValueError:
            self.fail(f"{tag}: schema --output {command} stdout must parse as the envelope; got {out[:200]!r}")
        self.assertEqual(code, 0, f"{tag}: schema --output {command} must succeed: {err[:160]}")
        data = env.get("data") or {}
        schema = data.get("schema")
        self.assertIsInstance(
            schema, dict,
            f"{tag}: schema --output {command} must carry a data['schema'] object"
            f" describing the FULL envelope; data keys: {sorted(data)}",
        )
        self.assertEqual(
            schema.get("type"), "object",
            f"{tag}: {command}: the schema must describe the envelope object; got type {schema.get('type')!r}",
        )
        defects = schema_defects(
            schema, command, (data.get("data_keys") or []), data.get("data_kind") or "object"
        )
        self.assertFalse(
            defects,
            f"{tag}: {command}: the emitted full-envelope schema is vacuous"
            f" somewhere (a vacuous declaration can never validate a real"
            f" envelope honestly):\n- " + "\n- ".join(defects),
        )
        return schema

    def _emitted_data_keys(self, command: str, tag: str = "OC-AC10") -> list:
        """The data_keys `schema --output command` actually EMITS (round 3,
        finding 3: the sweep must run on the emitted declaration, not only
        the registry)."""
        code, out, err = run_cli(["schema", "--output", command, *self.base])
        self.assertEqual(code, 0, f"{tag}: schema --output {command} must succeed: {err[:160]}")
        try:
            data = json.loads(out).get("data") or {}
        except ValueError:
            self.fail(f"{tag}: schema --output {command} stdout must parse; got {out[:160]!r}")
        return data.get("data_keys") or []

    def _run_command(self, command: str, tail: list[str], repo_override: bool, dead_gh: bool = False):
        argv = [command, *tail, *self.base]
        if repo_override:
            argv += ["--repo", "acme/other-never-synced"]
        from zaxbygraph import claims as claims_mod
        from test_output_contract_92_93_e import live_gh_seam

        seam = DeadGh() if dead_gh else live_gh_seam(command)
        saved_gh = claims_mod.run_gh
        if seam is not None:
            claims_mod.run_gh = seam
        try:
            return run_cli(argv)
        finally:
            claims_mod.run_gh = saved_gh


class TestAc10SuccessSchemaSweep(_SweepCase):
    """D8 (AC10): every registry command's declared schema validates its
    REAL success envelope — including the CLI-specific bare-list `open` and
    the multi-number `item` answer."""

    def test_ac10_every_command_schema_matches_success_envelope(self) -> None:
        registry = self._registry()
        problems: list[str] = []
        missing = sorted(set(registry) - set(SUCCESS_CALLS))
        if missing:
            problems.append(f"the sweep table must cover every registry command; uncovered: {missing}")
        for command, (tail, repo_override, note) in sorted(SUCCESS_CALLS.items()):
            if command not in registry:
                continue
            schema = self._declared_schema(command)
            code, out, err = self._run_command(command, tail, repo_override)
            try:
                env = json.loads(out)
            except ValueError:
                problems.append(f"{command} ({note}): stdout is not JSON: {out[:120]!r} err {err[:120]!r}")
                continue
            if code != 0 or env.get("ok") is not True:
                problems.append(
                    f"{command} ({note}): the success leg must succeed; exit {code},"
                    f" ok {env.get('ok')!r}, error {(env.get('error') or {}).get('code')!r}"
                )
                continue
            errors: list = []
            validate(schema, env, command, errors)
            if errors:
                problems.append(
                    f"{command} ({note}): the REAL success envelope must validate"
                    f" against its declared full-envelope schema: {errors[:6]}"
                )
        # The CLI-only multi-number item answer: one row per number with
        # per-row not_found rows, validated against the SAME emitted
        # `item` schema (data union object | array | null).
        schema = self._declared_schema("item")
        code, out, err = self._run_command("item", ITEM_MULTI_TAIL, False)
        try:
            env = json.loads(out)
        except ValueError:
            problems.append(f"item multi-number: stdout is not JSON: {out[:120]!r}")
            env = {}
        if code != 0 or env.get("ok") is not True:
            problems.append(
                f"item multi-number: the mixed stored+ghost answer must succeed"
                f" (per-row not_found rows); exit {code}, ok {env.get('ok')!r}"
            )
        else:
            rows = env.get("data")
            if not isinstance(rows, list) or len(rows) != len(ITEM_MULTI_TAIL):
                problems.append(
                    f"item multi-number: data must be one row per number; got {type(rows).__name__}"
                )
            else:
                ghost = rows[-1]
                if ghost.get("number") != 99999 or ghost.get("not_found") is not True:
                    problems.append(f"item multi-number: the ghost row must be a per-row not_found row; got {ghost!r}")
                if (ghost.get("error") or {}).get("code") != "not_found":
                    problems.append(f"item multi-number: the ghost row must carry error.code not_found; got {ghost.get('error')!r}")
                declared = set(registry["item"].get("data_keys") or [])
                stray = sorted(set(ghost.keys()) - declared - set(ITEM_MULTI_EXTRA))
                if stray:
                    problems.append(
                        f"item multi-number: ghost-row keys outside the declared"
                        f" row fields and the documented per-row markers"
                        f" {sorted(ITEM_MULTI_EXTRA)}: {stray}"
                    )
            errors = []
            validate(schema, env, "item-multi", errors)
            if errors:
                problems.append(
                    f"item multi-number: the REAL multi-number envelope must validate"
                    f" against the SAME emitted item schema: {errors[:6]}"
                )
        self.assertFalse(
            problems,
            "OC-AC10: every command's declared schema must match its real"
            " success envelope (incl. the CLI-specific open/multi-number item"
            " shapes):\n- " + "\n- ".join(problems),
        )

    def test_brief_data_keys_and_item_multi_kind(self) -> None:
        # Declared data_keys for `brief` AND the emitted schema's required
        # data keys both equal the real always-present set (author,
        # created_at, html_url, labels, updated_at included) — ONE
        # declaration, not two drifting lists.
        code, out, err = run_cli(["brief", "1", *self.base])
        self.assertEqual(code, 0, f"OC-AC10: the real brief must succeed: {err[:160]}")
        real = parse_envelope(self, out, "OC-AC10").get("data") or {}
        code, out, err = run_cli(["schema", "--output", "brief", *self.base])
        data = (json.loads(out).get("data") or {})
        declared = data.get("data_keys") or []
        for key in ("author", "created_at", "html_url", "labels", "updated_at"):
            self.assertIn(
                key, declared,
                f"OC-AC10: the brief data_keys must include the always-present {key!r}; declared: {sorted(declared)}",
            )
        self.assertEqual(
            sorted(declared), sorted(real.keys()),
            "OC-AC10: the brief data_keys must equal the real payload's"
            f" always-present keys; declared {sorted(declared)}, real {sorted(real.keys())}",
        )
        schema = data.get("schema") or {}
        data_required = ((schema.get("properties") or {}).get("data") or {}).get("required") or []
        self.assertEqual(
            sorted(data_required), sorted(real.keys()),
            "OC-AC10: the emitted schema's required data keys for brief must"
            f" equal the same always-present set; got {sorted(data_required)}"
            f" vs real {sorted(real.keys())}",
        )
        # `schema --output item` reports data_kind "object" for the
        # single-number answer (data_kind_multi is NOT part of the
        # contract; the plan dropped that key).
        code, out, err = run_cli(["schema", "--output", "item", *self.base])
        item_data = (json.loads(out).get("data") or {})
        self.assertEqual(
            item_data.get("data_kind"), "object",
            f"OC-AC10: schema --output item must report data_kind 'object' for the"
            f" single-number answer; got {item_data.get('data_kind')!r}",
        )

    def test_declared_data_keys_match_real_payloads(self) -> None:
        # (3)+(6-iv), ROUND 3 (finding 3): for EVERY command the EMITTED
        # `schema --output` data_keys (a) EQUAL the CLI_OUTPUT_SHAPES
        # registry declaration (drift between the two surfaces is a defect)
        # and (b) sweep against the real success payload — phantom keys are
        # drift, and real keys outside the declaration and the command's
        # pinned allowlist of documented conditional keys are drift (every
        # command has an entry; the default is empty — an unpinned command
        # is a sweep bug, never a pass).
        registry = self._registry()
        problems: list[str] = []
        missing_entries = sorted(set(registry) - set(REAL_MINUS_DECLARED_ALLOWLIST))
        if missing_entries:
            problems.append(
                f"every registry command needs a pinned conditional-key"
                f" allowlist entry (default empty); missing: {missing_entries}"
            )
        for command, (tail, repo_override, note) in sorted(SUCCESS_CALLS.items()):
            if command not in registry:
                problems.append(f"{command}: registry entry missing")
                continue
            emitted = self._emitted_data_keys(command)
            registry_keys = set(registry[command].get("data_keys") or [])
            if set(emitted) != registry_keys:
                problems.append(
                    f"{command}: the EMITTED schema --output data_keys"
                    f" {sorted(emitted)} must equal the CLI_OUTPUT_SHAPES"
                    f" registry declaration {sorted(registry_keys)}"
                )
                continue
            code, out, err = self._run_command(command, tail, repo_override)
            env = json.loads(out) if out.strip().startswith("{") else {}
            data = env.get("data")
            if code != 0 or not isinstance(data, (dict, list)):
                problems.append(f"{command} ({note}): the payload probe must succeed; exit {code}")
                continue
            if isinstance(data, list):
                real: set = set()
                for row in data:
                    if isinstance(row, dict):
                        real |= set(row.keys())
            else:
                real = set(data.keys())
            allowlist = set(REAL_MINUS_DECLARED_ALLOWLIST.get(command, frozenset()))
            problems += data_keys_sweep_problems(command, emitted, real, allowlist)
        self.assertFalse(
            problems,
            "OC-AC10: the EMITTED data_keys must equal the registry and"
            " track the real payloads for EVERY command (phantom keys and"
            " undeclared keys are drift):\n- " + "\n- ".join(problems),
        )

    def test_validator_rejects_vacuous_and_wrong_envelopes(self) -> None:
        # (2)+(6) SELF-TEST of the check itself: the falsifier must reject a
        # vacuous schema and every named mutation, and the validator must
        # reject an obviously wrong envelope against a real schema — a
        # mutation-style guard that this sweep cannot pass itself on an
        # empty declaration.
        registry = self._registry()
        self.assertTrue(registry, "OC-AC10: fixture sanity: the registry is non-empty")
        vacuous = {"type": "object", "properties": {}, "required": []}
        defects = schema_defects(vacuous, "selftest", ["number"], "object")
        self.assertTrue(
            defects,
            "OC-AC10: SELF-TEST: the schema-defect falsifier must REJECT a"
            f" vacuous schema (empty properties, empty required); got {defects!r}",
        )
        defects = schema_defects(
            {"type": "object", "properties": {"data": {}}, "required": ["ok"]},
            "selftest", ["number"], "object",
        )
        self.assertTrue(
            defects,
            "OC-AC10: SELF-TEST: the falsifier must reject an empty data"
            f" declaration and a truncated required list; got {defects!r}",
        )
        # (6-i) a list items schema with no type/properties is REJECTED, and
        # list-row properties omitting the declared row fields are rejected.
        defects = schema_defects(
            {"type": "object", "properties": {"data": {"type": "array", "items": {"description": "x"}}}, "required": ["ok"]},
            "selftest", ["path", "prs"], "list",
        )
        self.assertTrue(
            defects,
            "OC-AC10: SELF-TEST: the falsifier must reject a list items schema"
            f" without type/properties ({{'description': 'x'}}); got {defects!r}",
        )
        defects = schema_defects(
            {"type": "object", "properties": {"data": {"type": "array", "items": {"type": "object", "properties": {"unrelated": {"type": "string"}}}}}, "required": ["ok"]},
            "selftest", ["path", "prs"], "list",
        )
        self.assertTrue(
            defects,
            "OC-AC10: SELF-TEST: the falsifier must reject list-row properties"
            f" that omit the declared row fields; got {defects!r}",
        )
        # (6-ii) empty declared data_keys is a defect for EVERY command.
        defects = schema_defects(
            {"type": "object", "properties": {"data": {"type": "object", "properties": {"number": {"type": "integer"}}}}, "required": ["ok"]},
            "selftest", [], "object",
        )
        self.assertTrue(
            defects,
            "OC-AC10: SELF-TEST: the falsifier must reject empty declared"
            f" data_keys; got {defects!r}",
        )
        # (6-iii) an error object without required string code/message, and a
        # schema with NO error declaration, are rejected.
        defects = schema_defects(
            {"type": "object", "properties": {"error": {"type": ["object", "null"], "properties": {"code": {"type": "string"}, "message": {"type": "string"}}}}, "required": ["ok"]},
            "selftest", ["number"], "object",
        )
        self.assertTrue(
            defects,
            "OC-AC10: SELF-TEST: the falsifier must reject an error object"
            f" without required string code/message; got {defects!r}",
        )
        no_error = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        self.assertTrue(
            schema_defects(no_error, "selftest", ["number"], "object"),
            "OC-AC10: SELF-TEST: the falsifier must reject a schema with NO error declaration",
        )
        self.assertFalse(
            wrong_envelope_rejections(),
            "OC-AC10: SELF-TEST: the validator must reject every obviously"
            " wrong envelope against the real registry schema:\n- "
            + "\n- ".join(wrong_envelope_rejections()),
        )

    def test_amendment_negative_selftests(self) -> None:
        # ROUND 3 (finding 3): isolated negative self-tests that start from
        # a VALID schema and remove ONLY the feature under test — missing
        # error; row type removed; truncated emitted data_keys — so each
        # named mutation FAILS the falsifier or the sweep.
        problems = amendment_selftest_problems()
        self.assertFalse(
            problems,
            "OC-AC10: SELF-TEST: removing exactly one feature from a valid"
            " schema (the error object; the list row type; truncating the"
            " declared data_keys) must be caught:\n- " + "\n- ".join(problems),
        )


class TestAc11FailureSchemaSweep(_SweepCase):
    """D8 (AC11): every registry command's declared schema validates its
    REAL failure envelope — ok:false, error.code/message shapes, version,
    and data:null except the data-preserving live claim outcomes."""

    def test_ac11_every_command_schema_matches_failure_envelope(self) -> None:
        registry = self._registry()
        problems: list[str] = []
        missing = sorted(set(registry) - set(FAILURE_CALLS))
        if missing:
            problems.append(f"the sweep table must cover every registry command; uncovered: {missing}")
        for command, legs in sorted(FAILURE_CALLS.items()):
            if command not in registry:
                continue
            schema = self._declared_schema(command, tag="OC-AC11")
            for tail, repo_override, note, data_kind in legs:
                code, out, err = self._run_command(command, tail, repo_override, dead_gh=True)
                try:
                    env = json.loads(out)
                except ValueError:
                    problems.append(f"{command} ({note}): stdout is not JSON: {out[:120]!r}")
                    continue
                if code == 0 or env.get("ok") is not False:
                    problems.append(
                        f"{command} ({note}): the failure leg must fail; exit {code}, ok {env.get('ok')!r}"
                    )
                    continue
                error = env.get("error") or {}
                if not isinstance(error.get("code"), str) or not error.get("code"):
                    problems.append(f"{command} ({note}): error.code must be a non-empty string; got {error.get('code')!r}")
                if not isinstance(error.get("message"), str) or not error.get("message"):
                    problems.append(f"{command} ({note}): error.message must be a non-empty string; got {error.get('message')!r}")
                if env.get("version") != "0.3":
                    problems.append(f"{command} ({note}): the failure envelope must carry version '0.3'; got {env.get('version')!r}")
                # The data expectation distinguishes LIVE claim outcomes (the
                # outcome IS the answer: the documented FB-008 contract keeps
                # a dict beside the error) from pre-flight refusals (data null).
                if data_kind == "dict":
                    if not isinstance(env.get("data"), dict):
                        problems.append(
                            f"{command} ({note}): a live claim outcome keeps its data dict"
                            f" beside the error; got {env.get('data')!r}"
                        )
                elif env.get("data") is not None:
                    problems.append(f"{command} ({note}): a pre-flight refusal carries data:null; got {type(env.get('data')).__name__}")
                errors: list = []
                validate(schema, env, command, errors)
                if errors:
                    problems.append(
                        f"{command} ({note}): the REAL failure envelope must validate"
                        f" against the SAME declared full-envelope schema: {errors[:6]}"
                    )
        self.assertFalse(
            problems,
            "OC-AC11: every command's declared schema must match its real"
            " failure envelopes (ok:false, coded error, version; data null"
            " for pre-flight refusals, data dict for live claim outcomes):\n- "
            + "\n- ".join(problems),
        )


if __name__ == "__main__":
    unittest.main()
