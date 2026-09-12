---
name: frontier-audit-enhance
description: >
  Repo-agnostic SOTA audit and enhancement program. Activate on frontier audit,
  SOTA audit, enhancement program, history reconstruction, or find what prior
  reviews missed. Collectors read and file only. The lead model alone generates
  candidates. Independent reviewers and critics verify. Integrity in scope.
  Dual-use work out of scope. Any language, any host, any model. Phase 1 prefers
  zaxbygraph over paging GitHub.
metadata:
  type: workflow
  version: "1.2.0"
  created: "2026-09-11"
---

# Frontier audit and enhancement

Execute this program against the current working tree. Discover what the repository is. Do not assume language, host product, or a prior target.

Two equal mandates:

1. Evidence-grade audit of defects, structural gaps, and user-facing friction that prior reviews missed.
2. Enhancement program grounded in sources retrieved during this run.

Primary goal: findings a competent mid-tier reviewer would miss. Interaction failures, recurrence without a structural fix, unenforced contracts, producer/consumer splits, deferred work later paths depend on, and capabilities this architecture can deliver that peers do not.

Do not implement fixes. Do not modify product source. Write only the report and run artifacts.

## Load these references before Phase 0

- `references/research-foundations.md` — why the role split exists
- `references/integrity-policy.md` — in-scope integrity vs out-of-scope dual-use work
- `references/subagent-protocol.md` — collectors vs lead vs reviewers vs critics
- `references/phases.md` — Phase 0 through Phase 6
- `references/report-template.md` — required report
- `assets/PASTE_PROMPT.md` — standalone prompt when the host cannot load this skill
- sibling skill `../zaxbygraph/SKILL.md` — Phase 1 history source

## Role split (hard)

| Role | Who | Allowed | Forbidden |
|---|---|---|---|
| Collector | Cheap or fast sub-agent | Read, list, grep, extract verbatim quotes, run prescribed commands (including `zaxbygraph` queries), dump raw packets | Candidates, severity, root cause, this is a bug, enhancements, ranking |
| Lead | The model running this skill | All candidate generation, history synthesis, enhancement proposals, ranking | Accepting collector judgments. There must be none. |
| Reviewer | Independent context, preferably a different model family | Re-read cited files and uphold, refine, downgrade, or overturn a candidate | Inventing a new candidate unless the lead opened a miss-hunt with a sealed packet |
| Critic | Independent context | Falsify top findings and proposals. Check grounding. | Expanding scope or adding praise |

If the host cannot spawn sub-agents, the lead still separates the jobs in time. Collection packets are written to disk. Candidate generation starts only after those files exist. Review and critic passes start only after candidates exist. Do not mix jobs in one continuous reasoning pass.

## Evidence and novelty gates

Discard a claim that fails any of:

1. No path and line range, no command plus output, no issue or PR number, and not labeled UNVERIFIED HYPOTHESIS.
2. It only restates a written project contract unless you show the violation or non-enforcement site.
3. It restates a shipped proposal or prior audit unless you show what changed.
4. A mid-tier reviewer doing a standard pass would catch it (style, nits, unused imports, missing docs, naming).
5. For enhancements — no 2025-2026 primary source retrieved this run, or no repository evidence the gap is real, or an existing proposal already covers it at equal or better impact/effort.

## Host neutrality

Use the search, fetch, GitHub, and shell tools this host actually exposes. Name them in Method and coverage. Prefer current primary sources over training memory. If a named research MCP exists, use it. If it does not, use host web search and page fetch.

## Phase 1 history source

Prefer ZaxbyGraph. After a successful `zaxbygraph sync`, collectors query SQLite (`status`, `search`, `item`, `related`, `churn`, `open`, `path`, `sql`) and paste truncated JSON. They do not page `gh api --paginate` of issues/PRs/comments into the lead. Details in `references/phases.md` and `docs/frontier-audit-hook.md` at the zaxbygraph repo root.

`closes` in the graph is keyword-in-body, not GitHub auto-close.

## Start

1. Create a run directory. Prefer `.swarm/frontier-audit/<utc-stamp>/` when `.swarm/` is ignored, else `audit-runs/<utc-stamp>/`.
2. Run Phase 0 until `identity-packet.md` exists (include whether `zaxbygraph` and `gh` work).
3. Dispatch collectors. Wait for raw packets.
4. Lead generates candidates from packets plus its own re-reads of cited files.
5. Independent review, then critic, then report.
