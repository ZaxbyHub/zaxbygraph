# Usage-trace addendum to the 2026-11-09 first-class review

Date: 2026-11-10. Base: `origin/main` 6009c30. Input: the maintainer's
"complete usage history" digest (generated 2026-11-09 from ZCode and Claude
Code transcripts: 289 engagements, 4,375 tool calls, 5,069 CLI invocations,
2026-09-12 → 2026-11-09, six consumer repositories plus this one).

The 2026-11-09 review was built from reading and running the tool. This
addendum tests it against how agents actually used the tool. Every claim
about current behaviour below was re-run on `main` today; every claim about
past use cites the digest and says where the digest cannot support it.

## 1. What the digest can and cannot prove

The digest is a narrative built on a mechanical call ledger. Its counts are
strict for the commands its parser knew. **The parser did not know the
derived commands.** Two ledger rows show a `triage` leg dropped from a
compound command (the output reads `triage exit=2` and
`usage: zaxbygraph triage …`, while the recorded command lists only `open`
and `churn`), and the subcommand table has no row for `similar`, `explain`,
`coupling`, `eval`, `triage`, `bundle`, `brief` or `snapshot`, including in
the 21 development sessions that built and tested them.

Consequence: the digest cannot say how often the derived tier was used. It
can say what the recorded calls did. The raw event files can settle it
(§6).

## 2. What agents actually do

| Share of invocations | Command | What it is used for |
|---|---|---|
| 26% (1,309) | `item` | Reading issue contracts and comment threads, usually in a shell loop over 4–24 numbers |
| 25% (1,285) | `sql` | Open-issue census with labels, open-PR list, state checks for a set of numbers, claim sweeps, label/title `LIKE` scans |
| 13% (641) | `search` | Prior-art and duplicate checks before filing |
| 9% (435) | `open` | Inventory |
| 14% (721) | `status` + `sync` | Freshness, incremental update |
| 4% (187) | `related` | Neighbourhood of a candidate |
| 5% (253) | `--version`, `--help` | Probing |

255 of 289 engagements are two workflows driven by one standing prompt:
a layout survey before working a named issue (105) and a repository-wide
"which issues can be collated into one PR" survey (150). The MCP server was
never registered in any session; every call was the CLI.

In the recorded calls, the collation survey is hand-built every time from
the same five steps: open issues with labels (`sql`), open PRs (`sql`), a
**claim sweep** over recent comments (`sql`, 40 calls, matching
`Agent claim` / `Working this issue`), body reads (`item` or
`sql substr()`), and `related` plus state checks on the neighbours.

## 3. Where the calls failed, and what is still true on `main`

598 calls returned a non-OK result. Classes, with today's status:

| Class (digest §) | Size | Status on `main` 6009c30 | Tracked |
|---|---|---|---|
| Agent JSON post-processing crashes on output shape (5A.2) | ≈265 calls, ~90 engagements | Identity line fixed (#20). Shapes still differ: `open` and `churn` return a bare list, `search` an object with `items`, `sql` `{columns, rows}`. No machine-readable description of any CLI shape. **Verified.** | New AC on #72 |
| Schema guessing, loud (5A.3) | ≈42 calls | `no_such_column` carries a column hint. `no_such_table` carries none. **Verified.** | New AC on #72 |
| Vocabulary guessing, silent (5A.3, 5A.5, 5A.11) | ~20 engagements | `kind='pull_request'`, `state='OPEN'`, `comments.kind='issue'` and a mixed-case `repo` literal each return `ok: true` with zero rows and no hint. **Verified.** | New AC on #72 |
| CLI not on PATH; version illusion; registry probes (5A.1, 5A.12) | ~60 engagements | Unchanged | #73 |
| Flag placement and unknown flags (5A.12) | ~10 engagements | `zaxbygraph --db X status` and `item --number 1 --format json` print argparse usage on stderr, exit 2, no envelope. **Verified.** | New AC on #72 |
| Split stores (5A.4) | ~12 engagements | Addressed by `where`, `doctor --consolidate` | Closed work |
| Interrupted syncs, introspection abort (5A.5) | 3+ engagements | Open | #65 |
| Oversized outputs (5A.13) | ~10 engagements | Open | #70, #71 |
| Ghost `related` rows crashing formatters ("#None [None/None]") | 2 engagements | Open | #72 AC2 |
| Windows cp1252 decode (5A.7) | ~7 engagements | **Fixed**: `gh` output is decoded as UTF-8 at `github.py:77`, `graphql.py:735`, `sync.py:790`; stdio is reconfigured at `cli.py:68-77`. The digest's "no landing fix observed" is wrong for `main`. | — |
| `export-corpus --help` headed `snapshot pull` (5A.12) | 1 | Does not reproduce on `main`. | — |
| Exit code disagrees with envelope (5A.9) | ~9 engagements | Does not reproduce for `status`/`item` on `main`; the recorded cases are compound shell lines whose last leg was another tool. Not filed. | — |
| Git-Bash `/tmp` redirects (5A.8) | ≈39 calls | Not a tool defect | — |

## 4. Three findings the review did not have

### 4.1 A wrong literal nearly caused a duplicate claim, and the wrong lesson was stored

Digest §5A.5 reports a "comments table that lagged behind items", which
produced a false "NO CLAIMS" and nearly a duplicate claim of
opencode-swarm #3539 + #3491 (engagement `sess_1fda0be0`, 2026-11-03). The
ledger shows otherwise. All three failing sweeps (rows 7, 11, 14) filter
`comments.kind='issue'`. The stored values are `issue_comment` and
`review_comment`, so the filter matches nothing. `item 3539` read the same
table without that filter and showed the claim. There was no lag.

The session then stored three memory records (5592dc0c…, 95f39224…,
33f55f83…) saying the comments table can lag after an incremental sync and
must never be used for claim sweeps. That lesson is false and should be
superseded. The tool-side fix is to stop a zero-row answer from an
out-of-vocabulary literal looking like a finding.

### 4.2 Work selection does not know what another session has claimed

Sessions on these repositories announce work with an issue comment
(`**Agent claim:** Working this issue now …`). The digest records 40
hand-written claim sweeps, nine bundles dropped or narrowed because another
session owned the work, and two duplicates caught late. `triage` and
`bundle` read none of this. Probe on a copy of this repository's store: one
comment `**Agent claim:** Working this issue now` added to open issue #8;
`triage` returns #8 `ranked: true, gates: []` and `bundle` returns
`[8, 21]`. **Verified.**

The 2026-11-09 epic held this back as a proposal with the trigger "agents
are observed colliding". The trigger has fired. A gate computed at query
time from stored comments needs no persistence and stays inside the
derived-tier rule.

### 4.3 The skill agents load is not the skill in the repository

The skill is not part of the installed package (`pyproject.toml` ships only
`schema.sql`), so every installed copy is a manual copy. The digest shows
three copies on the maintainer's machine: `~/.claude/skills` is the v0.1
text from 2026-09-12; `~/.zcode/skills` was measured on 2026-11-08 at
revision 3d3aab0 (2026-10-28, 291 lines); `~/.agents/skills` is stamped
b47913e (2026-10-23). The triage → bundle → brief recipe first appears in
df98847 (2026-10-30). None of the three stamped revisions contains the
words `triage` or `bundle`. **Verified against git history.**

The skill was loaded 196 times. For at least 2026-10-30 → 2026-11-08 a
ZCode session that loaded it could not have learned that the derived
commands exist. This is the most likely reason the recorded collation
surveys are hand-built from `sql`, and it is independent of whether the
commands are good.

## 5. What this changes in the 2026-11-09 plan

- **Order of value.** `item` and `sql` are half of all use and the source
  of most failures. The honest-surface work (#72) and the install/skill work
  (#73) reach every session on the day they merge. Typed MCP schemas
  (#72 AC8) and MCP defaults (#70 AC6) reach none until the server is
  registered somewhere; #73's `init` printing the registration line is the
  step that makes them matter.
- **Workstream B is still the product**, and the standing prompt is the
  proof: 150 engagements asked exactly the question `bundle` exists to
  answer. Its acceptance now needs a claim gate (new issue) and the skill
  reaching agents (#73), or the evaluation will improve a command nobody is
  told about.
- **Not changed:** Workstream A. The digest's sync failures (502, rate
  limit, introspection abort, partial stores) match #65–#67 as filed.

## 6. Unknowns

- Whether consumer sessions invoked `triage`, `bundle`, `brief` or
  `similar` at all. The digest's parser dropped them (§1). One search of
  the pre-filter event files answers it:
  `grep -cE 'zaxbygraph(\.exe)?"? +(triage|bundle|brief|similar|explain|coupling|eval)\b' zcode_events.jsonl claude_events.jsonl`
  in `E:\ZCode\zmem-dev\.zcode\scratch-zg\`.
- Whether the `~/.zcode` and `~/.agents` skill copies are identical today.
  The digest states both that they are byte-identical and current, and
  gives two different stamps.
- Carried from 2026-11-09 and still open: the `own/repo` stub store, and
  the 928-versus-878 test count.

## 7. Tracking

| Finding | Where |
|---|---|
| Claim gate for `triage` and `bundle` (§4.2) | New issue, Workstream B PR 4 |
| Out-of-vocabulary literals, `no_such_table` hint, argparse errors as envelopes, several numbers in one `item` call, CLI output shapes described by the tool (§3, §4.1) | #72, new claims and ACs |
| Skill shipped with the package and checked against installed copies (§4.3) | #73, new claim and ACs |
| Coverage rows, fired triggers, priority note | #64 |
