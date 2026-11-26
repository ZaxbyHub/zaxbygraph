"""Acceptance checks for issue-trace 92-93-output-contract-honesty (issues
#92+#93, decision D10 / AC5+AC7+AC8: the documentation states the output
contract honestly), amended after plan-critic rounds 1-4.

DOCS_ONLY content pins plus the critic's EXECUTED verification: C7 first
derives which concrete statements the REAL guards pass at layer 1 and deny
at layer 2 (query.assert_read_sql and db.connect_readonly_query's
authorizer), THEN pins the docs table rows to name each of them.

- D10 (AC5): EVERY README sentence claiming CLI/MCP payload parity —
  bullet, preamble or non-bullet prose (round 3), in any phrasing:
  match/parity/identical/same-as (round 3) plus equivalent/equals/mirror/
  same-payload (round 4) — must mention BOTH the 4000-token default budget
  AND briefs always on.
- D10 (AC7): docs/schema.md's verified table lists WITH-prefixed DML and
  EXPLAIN DML statements as passing layer 1 and POSITIVELY denied/refused
  by layer 2 — a denial word with no negation in its cell (round 3) and no
  trailing-answer form like 'denied by layer 2? no' (round 4); the
  load_extension row is retained (PRESERVING).
- D10 (AC8): ONE README contract table headed by a row containing 'exit'
  and 'script on `ok`', with EXPLICIT per-row semantics: exit 0 success;
  exits 2/3 NOT fixed by an unchanged retry; exit 1 sometimes is; the
  claim exits 4/5/6 state the claim outcomes are the contract — UNNEGATED
  (round 4) and covered unconditionally (round 3); the guidance tells
  callers to branch on `ok`; each binding cell states one digit; the
  tests/test_docs.py ExitCodeTableDocTests assertions are EXECUTED here.

Read-only: never edits the documents it pins. Hermetic for the guard legs.
Stdlib only; unittest. Failures carry OC-AC5 / OC-AC7 / OC-AC8 tags.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import zaxbygraph  # noqa: F401  (the OC-ENV guard below needs the module)

_WORKTREE = Path(__file__).resolve().parents[1]
if not Path(zaxbygraph.__file__).resolve().is_relative_to(_WORKTREE / "src"):
    raise AssertionError(
        f"OC-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src;<worktree>/tests"
    )

from fixtures import REPO  # noqa: E402
from test_output_contract_92_93_e import seed_corpus  # noqa: E402

README = _WORKTREE / "README.md"
SCHEMA_MD = _WORKTREE / "docs" / "schema.md"


def _flat(text: str) -> str:
    """Markdown line wraps must not defeat multi-word pins."""
    return re.sub(r"\s+", " ", text)


def _mcp_server_section(readme: str) -> str:
    match = re.search(r"^## MCP server\s*$", readme, re.M)
    if match is None:
        return ""
    rest = readme[match.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def _section_prose_blocks(section: str) -> list[tuple[str, bool]]:
    """(block, is_bullet) for EVERY prose block of a markdown section —
    bullets AND non-bullet paragraphs, fences stripped, headings skipped
    (round 3: parity prose is scanned wherever it sits)."""
    fences_stripped = re.sub(r"```.*?```", " ", section, flags=re.S)
    blocks: list[tuple[str, bool]] = []
    bullet: list[str] = []
    para: list[str] = []
    for line in fences_stripped.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            if para:
                blocks.append((" ".join(para), False))
                para = []
            if bullet:
                blocks.append((" ".join(bullet), True))
            bullet = [stripped[2:]]
            continue
        if bullet:
            bullet.append(stripped)
            continue
        if not stripped or stripped.startswith("#") or stripped.startswith("|"):
            if para:
                blocks.append((" ".join(para), False))
                para = []
            continue
        para.append(stripped)
    if bullet:
        blocks.append((" ".join(bullet), True))
    if para:
        blocks.append((" ".join(para), False))
    return blocks


#: ROUND 4 (finding 3): a denial cell is POSITIVE only when a denial word
#: has no negation in the cell AND no trailing-answer form ("? no",
#: "— no", ": false", "(n/a)") — those state the OPPOSITE of the denial.
_DENIAL_WORD = re.compile(r"\b(?:denied|refused|refuses|denies|rejected)\b", re.I)
_NEGATION = re.compile(r"\b(?:not|never|cannot)\b|n't", re.I)
_TRAILING_ANSWER = re.compile(
    r"\?\s*(?:no|false|n/?a)\b"
    r"|\b(?:no|false|n/?a)\s*[.)]?\s*[.!]?\s*$"
    r"|[—:]\s*(?:no|false|n/?a)\b",
    re.I,
)


def _positive_denial(row: str) -> bool:
    """True when at least one cell of the table row carries a denial word
    with no negation and no trailing-answer form (rounds 3-4)."""
    for cell in _cells(row):
        if _DENIAL_WORD.search(cell) and not _NEGATION.search(cell) and not _TRAILING_ANSWER.search(cell):
            return True
    return False


#: ROUND 4 (finding 4): the claim-outcome contract statement must be
#: UNNEGATED — not/never/isn't/excluded/outside variants state the
#: opposite of the contract the row is required to claim.
_CLAIM_NEGATION = re.compile(
    r"\b(?:not|never|isn't|aren't|don't|doesn't|excluded|outside)\b", re.I
)


def _claim_wording_problems(claim_cells) -> list:
    """Wording problems for the claim-outcome rows: each must state the
    claim outcomes ARE the contract, UNNEGATED (rounds 3-4)."""
    problems: list = []
    for cells in claim_cells:
        flat = _flat(" ".join(cells)).lower()
        if "claim" not in flat or "outcome" not in flat:
            problems.append(
                f"the claim-outcome row must state the claim outcomes are the contract; got {cells}"
            )
        if _CLAIM_NEGATION.search(flat):
            problems.append(
                f"the claim-outcome row must state the contract UNNEGATED"
                f" (not/never/isn't/excluded/outside are defects); got {cells}"
            )
    return problems


def _markdown_tables(text: str) -> list[list[str]]:
    """Contiguous `|...|` line blocks, one table per block, lines stripped."""
    tables: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            current.append(stripped)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _stated_digits(span: str) -> set[str]:
    """Every exit digit the span states: a backticked digit, a bold digit,
    or prose like 'exit 2'. (The same shapes tests/test_docs.py parses.)"""
    found = set(re.findall(r"`(\d+)`", span))
    found.update(re.findall(r"exit[ -](\d+)", span, re.I))
    found.update(re.findall(r"(?<![\w/])\*\*(\d+)\*\*", span))
    return found


class TestAc5ReadmeMcpBundleDefaults(unittest.TestCase):
    """D10 (AC5): EVERY payload-parity claim — bullet, preamble or any
    non-bullet prose line of the MCP section — carries BOTH bundle
    defaults; an unqualified 'matches the CLI exactly' style claim fails
    (round 3), and so do the 'equivalent'/'equals'/'mirrors'/'same
    payload' phrasings (round 4, finding 2)."""

    _SAME_AS = re.compile(r"\bsame\b[^.;]{0,120}\bas\b", re.I)

    def _is_parity_claim(self, sentence: str) -> bool:
        low = sentence.lower()
        if "cli" not in low:
            return False
        return bool(
            re.search(r"\bmatch(es|ing)?\b", low)
            or "parity" in low
            or "identical" in low
            or "equivalent" in low
            or re.search(r"\bequals?\b", low)
            or re.search(r"\bmirror(s|ed|ing)?\b", low)
            or "same payload" in low
            or self._SAME_AS.search(low)
        )

    def _parity_problems(self, section: str) -> list:
        """The parity scan the real README check runs (round 4: factored so
        the negative self-test exercises the SAME code path)."""
        problems: list[str] = []
        for block, in_bullet in _section_prose_blocks(section):
            block_flat = _flat(block)
            for sentence in re.split(r"(?<=[.;])\s+", block_flat):
                if not self._is_parity_claim(sentence):
                    continue
                has_budget = "4000" in sentence or "4000" in block_flat
                has_briefs = "brief" in sentence.lower() or "brief" in block_flat.lower()
                if not (has_budget and has_briefs):
                    where = "bullet" if in_bullet else "non-bullet prose"
                    problems.append(
                        f"a payload-parity claim in {where} without BOTH"
                        f" defaults (4000 and briefs): {sentence[:220]!r}"
                    )
        return problems

    def test_ac5_readme_states_mcp_bundle_defaults(self) -> None:
        readme = README.read_text(encoding="utf-8")
        section = _mcp_server_section(readme)
        self.assertTrue(
            section,
            "OC-AC5: the README must keep an '## MCP server' section to"
            " state the bundle defaults in",
        )
        flat = _flat(section)
        self.assertIn(
            "4000", flat,
            "OC-AC5: the MCP-server parity section must state the bundle's"
            " 4000-token budget default",
        )
        self.assertIn(
            "briefs", flat.lower(),
            "OC-AC5: the MCP-server parity section must state that briefs"
            " are always on (the 'briefs' default)",
        )
        problems = self._parity_problems(section)
        self.assertFalse(
            problems,
            "OC-AC5: every README sentence claiming CLI/MCP payload parity"
            " — bullet, preamble or non-bullet prose — must mention BOTH"
            " the 4000-token default budget AND briefs always on:\n- "
            + "\n- ".join(problems),
        )

    def test_ac5_recognizer_negative_selftest(self) -> None:
        # ROUND 4 (finding 2): the pinned unqualified 'equivalent'
        # preamble must be flagged by the SAME scan the README runs.
        problems = self._parity_problems(
            "## MCP server\n\nThe MCP and CLI data payloads are equivalent"
            " on the same store.\n"
        )
        self.assertTrue(
            any("equivalent on the same store" in p for p in problems),
            f"OC-AC5: SELF-TEST: the pinned 'equivalent' preamble must be flagged; got {problems!r}",
        )
        for phrase in ("The MCP payloads equal the CLI.", "The tool answers mirror the CLI.", "The bundle gives the same payload as the CLI."):
            self.assertTrue(self._is_parity_claim(phrase), f"OC-AC5: SELF-TEST: must catch {phrase!r}")
        self.assertFalse(self._is_parity_claim("Run the CLI sync before querying."), "OC-AC5: SELF-TEST: non-parity stays unflagged")


class TestAc7SchemaMdGuardLayers(unittest.TestCase):
    """D10 (AC7): the verified guard table names every layer-1-pass /
    layer-2-DENIAL statement class -- verified against the REAL guards
    first, then pinned in the docs."""

    #: The statement matrix EXECUTED against the real guards before the
    #: docs rows are trusted: {INSERT, UPDATE, DELETE} x {WITH, EXPLAIN}
    #: plus EXPLAIN QUERY PLAN <DML> — each passes layer 1 and is DENIED
    #: at layer 2; each needles pair locates the docs row for the class.
    _DML_STATEMENTS = (
        ("WITH-prefixed INSERT",
         "WITH x AS (SELECT 1) INSERT INTO items(repo, number, kind, title,"
         " state, raw_json) VALUES ('o/r', 1, 'issue', 't', 'open', '{}')",
         ("with", "insert")),
        ("WITH-prefixed UPDATE",
         "WITH x AS (SELECT 1) UPDATE items SET title = 'y'",
         ("with", "update")),
        ("WITH-prefixed DELETE",
         "WITH x AS (SELECT 1) DELETE FROM items",
         ("with", "delete")),
        ("EXPLAIN INSERT",
         "EXPLAIN INSERT INTO items(repo) VALUES ('x')",
         ("explain", "insert")),
        ("EXPLAIN UPDATE",
         "EXPLAIN UPDATE items SET title = 'y'",
         ("explain", "update")),
        ("EXPLAIN DELETE",
         "EXPLAIN DELETE FROM items",
         ("explain", "delete")),
        ("EXPLAIN QUERY PLAN DELETE",
         "EXPLAIN QUERY PLAN DELETE FROM items",
         ("query plan", "delete")),
    )

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        seed_corpus(Path(self._td.name) / "history.db")

    def _verify_real_guards(self) -> None:
        from zaxbygraph.db import connect_readonly_query
        from zaxbygraph import query

        for label, statement, _needles in self._DML_STATEMENTS:
            try:
                query.assert_read_sql(statement)
            except ValueError as exc:
                self.fail(f"OC-AC7: guard verification: {label} must PASS layer 1; rejected with: {exc}")
            conn = connect_readonly_query(Path(self._td.name) / "history.db")
            try:
                conn.execute(statement)
            except Exception as exc:
                self.assertIn(
                    "not authorized", str(exc).lower(),
                    f"OC-AC7: guard verification: {label} must be DENIED at"
                    f" layer 2 by the AUTHORIZER ('not authorized'), not"
                    f" merely by read-only mode; got: {exc}",
                )
            else:
                self.fail(f"OC-AC7: guard verification: {label} was EXECUTED on the read-only connection")
            finally:
                conn.close()

    def test_ac7_schema_md_lists_with_and_explain_dml(self) -> None:
        # The real guards, executed: each statement class below must pass
        # layer 1 and be denied by the layer-2 authorizer before the docs
        # rows are trusted.
        self._verify_real_guards()
        text = SCHEMA_MD.read_text(encoding="utf-8")
        rows = [
            _flat(line.strip())
            for line in text.splitlines()
            if line.strip().startswith("|")
        ]
        problems: list[str] = []
        for label, _statement, (word1, word2) in self._DML_STATEMENTS:
            matching = [
                r for r in rows
                if word1 in r.lower()
                and word2 in r.lower()
                and "passes layer 1" in r.lower()
                and _positive_denial(r)
            ]
            if not matching:
                problems.append(
                    f"docs/schema.md's verified table must list the {label}"
                    " statement as passing layer 1 and POSITIVELY"
                    " DENIED/REFUSED by layer 2 (negated denials such as"
                    " 'not denied by layer 2' and trailing-answer forms"
                    " such as 'denied by layer 2? no' do not count)"
                )
            elif label.startswith("EXPLAIN"):
                # The EXPLAIN rows must name the EXPLAIN QUERY PLAN form
                # explicitly (critic round 2, finding 8).
                if not any("query plan" in r.lower() for r in matching):
                    problems.append(
                        f"the {label} docs row must name the EXPLAIN QUERY PLAN"
                        " form explicitly"
                    )
        if not any("load_extension" in r.lower() and "passes layer 1" in r.lower() for r in rows):
            problems.append("the load_extension row stays in the verified table (PRESERVING)")
        readme_flat = _flat(README.read_text(encoding="utf-8"))
        if "the one case" in readme_flat:
            problems.append(
                "the README 'the one case' wording must be gone once more"
                " than one layer-1-pass statement class is documented"
            )
        self.assertFalse(
            problems,
            "OC-AC7: docs/schema.md must name every layer-1-pass / layer-2"
            "-denial statement class the real guards verify, with the"
            " POSITIVE denial wording:\n- " + "\n- ".join(problems),
        )

    def test_ac7_positive_denial_negative_selftest(self) -> None:
        # ROUND 4 (finding 3): trailing-answer and negated denial forms
        # must NOT count; the positive form must keep counting.
        for bad in ("denied by layer 2? no (exit 1)", "denied by layer 2 — no", "denied by layer 2: false", "denied by layer 2 (n/a)", "not denied by layer 2 (exit 1)"):
            self.assertFalse(_positive_denial(f"| x | **passes layer 1**, {bad} |"), f"OC-AC7: SELF-TEST: must reject {bad!r}")
        self.assertTrue(
            _positive_denial("| x | **passes layer 1**, denied by layer 2 (exit 1) |"),
            "OC-AC7: SELF-TEST: the positive denial wording must count",
        )


class TestAc8ReadmeContractTable(unittest.TestCase):
    """D10 (AC8): one README contract table with EXPLICIT per-row exit
    semantics, without breaking the existing exit-code doc pins."""

    def _contract_table(self, readme: str) -> list[list[str]]:
        found = []
        for table in _markdown_tables(readme):
            header = _flat(" ".join(_cells(table[0]))).lower()
            if "exit" in header and "script on" in header and "`ok`" in header:
                found.append(table)
        return found

    def test_ac8_readme_contract_table(self) -> None:
        readme = README.read_text(encoding="utf-8")
        tables = self._contract_table(readme)
        self.assertEqual(
            len(tables), 1,
            "OC-AC8: the README must carry exactly ONE contract table headed"
            f" by 'exit' and 'script on `ok`'; found {len(tables)}",
        )
        table = tables[0]
        rows = [
            _cells(row)
            for row in table[1:]
            if not set("".join(_cells(row))) <= set("-: |")  # skip |---|
        ]

        def row_for(digit: str):
            matches = [cells for cells in rows if cells and cells[0].strip("`*").strip() == digit]
            return matches[0] if matches else None

        # EXPLICIT semantics per row.
        rowmap = {d: row_for(d) for d in ("0", "1", "2", "3")}
        for digit, row in rowmap.items():
            self.assertIsNotNone(row, f"OC-AC8: the contract table must have a rule row for exit {digit}")
        problems: list[str] = []
        if rowmap["0"] and "succeed" not in _flat(" ".join(rowmap["0"])).lower():
            problems.append("the exit-0 rule row must state that the command succeeds")
        if rowmap["1"] and "sometimes" not in _flat(" ".join(rowmap["1"])).lower():
            problems.append("the exit-1 rule row must state that an unchanged retry sometimes fixes it")
        for digit in ("2", "3"):
            flat = _flat(" ".join(rowmap[digit] or [])).lower()
            if "retry" not in flat or not ("not" in flat or "never" in flat):
                problems.append(f"the exit-{digit} rule row must state it is NOT fixed by an unchanged retry")
        # The claim outcomes 4/5/6 are the contract (outcomes).
        claim_cells = [
            cells for cells in rows
            if cells
            and (
                cells[0].strip("`*").strip() in ("4", "5", "6")
                or set(re.findall(r"\d", cells[0])) == {"4", "5", "6"}
            )
        ]
        claim_digits: set = set()
        for cells in claim_cells:
            claim_digits |= set(re.findall(r"\d", cells[0]))
        problems += _claim_wording_problems(claim_cells)
        # The claim-row digit UNION must cover 4, 5 AND 6 explicitly —
        # ROUND 3: UNCONDITIONALLY, so a table with NO claim row at all
        # FAILS here instead of skating past the wording checks.
        if claim_digits != {"4", "5", "6"}:
            problems.append(
                f"the contract table must INCLUDE the claim-outcome exits"
                f" explicitly (one 4/5/6 row or 4/5/6 rows); covered only"
                f" {sorted(claim_digits)} — a missing claim row must fail"
            )
        # The header/guidance tells callers to branch on `ok`.
        if "branch on" not in _flat(" ".join(cell for row in table for cell in _cells(row))).lower():
            problems.append("the contract table's header/guidance must tell callers to branch on `ok`")
        # Each binding cell states exactly ONE exit digit (keeps
        # test_docs.py's binding parser unambiguous).
        for cells in rows:
            row_digit_cell = cells[0].strip("`*").strip()
            row_digits = set(re.findall(r"\d", row_digit_cell))
            if not row_digits or not row_digits <= set("0123456"):
                continue
            for other in cells[1:]:
                stray = _stated_digits(other) - row_digits
                if stray:
                    problems.append(
                        f"row {row_digit_cell}: a binding cell states the digit(s)"
                        f" {sorted(stray)} beside exit {sorted(row_digits)}: {other[:160]!r}"
                    )
        self.assertFalse(
            problems,
            "OC-AC8: the README contract table must state the explicit"
            " per-row exit semantics:\n- " + "\n- ".join(problems),
        )
        # WITHOUT breaking tests/test_docs.py ExitCodeTableDocTests.
        from test_docs import ExitCodeTableDocTests

        ExitCodeTableDocTests("test_readme_carries_the_exit_code_split")._assert_table(readme)

    def test_ac8_claim_wording_negative_selftest(self) -> None:
        # ROUND 4 (finding 4): negated claim-outcome wordings must be
        # REJECTED; the positive row must stay clean.
        positive = [["4/5/6", "no", "the claim outcomes are the contract: `4` taken, `5` lost_race, `6` github_unreachable"]]
        self.assertFalse(_claim_wording_problems(positive), f"OC-AC8: SELF-TEST: the positive row must pass; got {_claim_wording_problems(positive)!r}")
        for wording in ("the claim outcomes are not the contract: `4` taken", "claim outcomes are never the contract", "the claim outcomes are excluded from the contract", "outcomes 4/5/6 sit outside the claim contract", "outcome 4 isn't the contract"):
            problems = _claim_wording_problems([["4/5/6", "no", wording]])
            self.assertTrue(any("UNNEGATED" in p for p in problems), f"OC-AC8: SELF-TEST: must reject {wording!r}")


if __name__ == "__main__":
    unittest.main()
