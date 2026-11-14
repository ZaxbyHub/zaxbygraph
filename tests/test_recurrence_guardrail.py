"""Recurrence guardrail for trace 68-69 (new module; outside the checkpoint
manifest so the frozen test files keep their frozen bytes)."""
from __future__ import annotations

import unittest
import ast
from pathlib import Path

class FilterAnnotationGuardrailTests(unittest.TestCase):
    """Phase 4.2 recurrence guardrail (08a-recurrence-sweep.md): an AST scan
    that FAILS when a new unfiltered SQL aggregate (COUNT/GROUP BY/SUM) lands
    in derive.py's feature/gate builders without either the defect class's
    semantic filters or an explicit allowlist entry carrying a reason.

    The defect class (04-root-cause.md): "unfiltered aggregate" — a feature
    or gate computed from a table aggregate that lacks a filter the semantics
    require (bot actors, closed/child items, hub files, merged state, draft
    state, evidence-origin). The fix added filters at every named site; this
    guardrail keeps the next sibling aggregate from regressing silently.

    Added by the recurrence-sweep phase AFTER the frozen checks; no frozen
    method is touched. Hermetic: parses src/zaxbygraph/derive.py, runs no
    SQL, needs no store.
    """

    DERIVE_PATH = (
        Path(__file__).resolve().parents[1] / "src" / "zaxbygraph" / "derive.py"
    )

    #: The selection tier's feature/gate builders (plus their direct SQL
    #: helpers). A new aggregate in ANY of these functions is in scope.
    GUARDED_BUILDERS = frozenset({
        "_triage_features",   # stage-1 features + every gate input
        "_triage_compute",    # stage-2 (hotspot pool, merge stamps)
        "_item_state_map",    # gate state lookup (kind/state/merged/draft)
        "_open_pr_files",     # shared-file collision surface
        "_coupling_index",    # co-change evidence population
        "build_context",      # hub-share denominator + vocabulary
        "_bundle",            # greedy selection + pool classification
        "_bundle_payload",    # per-bundle echo
        "brief",              # work packet (bot-excluded comment ranking)
    })

    #: Substring markers naming the defect class's semantic filters. An
    #: aggregate whose SQL carries none of these must be allowlisted with a
    #: recorded reason or the guardrail fails. (A bare ``WHERE repo = ?`` is
    #: NOT a semantic filter — every guarded aggregate has one.)
    FILTER_MARKERS = (
        "is_bot", "state", "child_of", "hub", "merged", "draft",
        "kind", "evidence",
    )

    #: Aggregates whose semantics genuinely require none of the class
    #: filters, with the recorded reason. Keyed by normalized SQL text
    #: (whitespace-collapsed, constant parts in source order).
    ALLOWLIST = {
        "SELECT dst_id, COUNT(DISTINCT src_id) AS n FROM edges WHERE repo = ?"
        " AND rel IN ('duplicate_of', 'duplicate_keyword')"
        " AND src_type = 'item' AND dst_type = 'item' GROUP BY dst_id":
            "inbound_duplicates: each duplicate REFERENCE is itself the"
            " demand evidence — closing the duplicate is that lifecycle's"
            " normal terminal state and does not retract it;"
            " duplicate_of/duplicate_keyword edges are timeline/keyword-"
            "derived, never bot-authored; as-of governed by the temp"
            " edges view",
        "SELECT number, COUNT(*) AS n FROM item_events WHERE repo = ?"
        " AND type = 'reopened' GROUP BY number":
            "reopens: counting reopened events is actor- and state-agnostic"
            " by semantics — a reopen by any actor reopens the issue;"
            " temporally governed by prepare_snapshot's item_events shadow"
            " (created_at <= T)",
    }

    #: Non-vacuity floor: the guarded builders are known to hold at least
    #: this many aggregates today (mentions, duplicates, reopens, hub
    #: population, brief bot count). A parser regression that finds zero
    #: cannot pass vacuously.
    MIN_KNOWN_AGGREGATES = 5

    @staticmethod
    def _constant_text(node) -> str:
        import ast

        parts = [
            sub.value for sub in ast.walk(node)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
        ]
        return " ".join(parts)

    def _guarded_aggregates(self):
        """[(function, line, normalized_sql)] for every conn.execute
        aggregate inside the guarded builders, plus a fail-closed list for
        executes whose SQL cannot be statically resolved."""
        import ast
        import re

        tree = ast.parse(self.DERIVE_PATH.read_text(encoding="utf-8"))
        agg_re = re.compile(r"COUNT\s*\(|GROUP\s+BY|SUM\s*\(", re.IGNORECASE)
        module_vars: dict[str, str] = {}

        def _feed(store, name, text):
            if text:
                store.setdefault(name, "")
                store[name] += " " + text

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(
                node.targets[0], ast.Name
            ):
                _feed(module_vars, node.targets[0].id,
                      self._constant_text(node.value))
            if isinstance(node, ast.AugAssign) and isinstance(
                node.target, ast.Name
            ):
                _feed(module_vars, node.target.id,
                      self._constant_text(node.value))

        found, unresolved = [], []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            if fn.name not in self.GUARDED_BUILDERS:
                continue
            local_vars: dict[str, str] = {}
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and isinstance(
                    node.targets[0], ast.Name
                ):
                    _feed(local_vars, node.targets[0].id,
                          self._constant_text(node.value))
                if isinstance(node, ast.AugAssign) and isinstance(
                    node.target, ast.Name
                ):
                    _feed(local_vars, node.target.id,
                          self._constant_text(node.value))
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                callee = (
                    func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else ""
                )
                if callee != "execute" or not node.args:
                    continue
                sql = self._constant_text(node.args[0])
                if not sql and isinstance(node.args[0], ast.Name):
                    name = node.args[0].id
                    sql = local_vars.get(name) or module_vars.get(name, "")
                if not sql:
                    # Fail closed: a dynamically built statement in a
                    # guarded builder cannot be proven aggregate-free.
                    unresolved.append((fn.name, node.lineno))
                    continue
                if agg_re.search(sql):
                    found.append((fn.name, node.lineno, " ".join(sql.split())))
        return found, unresolved

    def test_every_guarded_aggregate_carries_a_filter_or_allowlist_reason(
        self,
    ):
        found, unresolved = self._guarded_aggregates()
        self.assertFalse(
            unresolved,
            f"unresolvable conn.execute SQL inside guarded builders (make"
            f" the statement statically readable or allowlist it with a"
            f" reason): {unresolved}",
        )
        self.assertGreaterEqual(
            len(found),
            self.MIN_KNOWN_AGGREGATES,
            f"expected at least {self.MIN_KNOWN_AGGREGATES} guarded"
            f" aggregates (a parser regression must not pass vacuously);"
            f" found {found}",
        )
        unfiltered = []
        markers_seen = set()
        for func, line, sql in found:
            sql_markers = {
                m for m in self.FILTER_MARKERS if m in sql.lower()
            }
            markers_seen |= sql_markers
            if sql_markers or sql in self.ALLOWLIST:
                continue
            unfiltered.append(
                f"derive.py:{line} in {func}(): {sql!r} — add the semantic"
                f" filter the class requires (is_bot/state/child_of/hub/"
                f"merged/draft/kind/evidence) or record an ALLOWLIST reason"
            )
        self.assertEqual(
            unfiltered,
            [],
            "unfiltered aggregate(s) in derive.py's feature/gate builders"
            " (recurrence of the #69 defect class): " + "; ".join(unfiltered),
        )
        # The fix's three filter families must stay represented among the
        # guarded aggregates: bot actors, open/closed state, merged state.
        for required in ("is_bot", "state", "merged"):
            self.assertIn(
                required,
                markers_seen,
                f"no guarded aggregate carries the {required!r} filter"
                f" anymore — the {required} filter family must stay"
                f" applied in derive.py's builders",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
