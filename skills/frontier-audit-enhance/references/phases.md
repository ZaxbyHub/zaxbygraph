# Phases

Do not start Phase N+1 until the named artifacts of Phase N exist in the run directory.

## Phase 0 — Identity and contracts

Lead-driven. Collectors may list and extract. The lead writes the identity packet.

Discover, do not assume:

- VCS remote, default branch, package manifests, language(s), runtimes, build and test entrypoints
- Host product if any (CLI, plugin, library, service, app, monorepo package)
- Contract files if present: `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `ARCHITECTURE.md`, ADRs, engineering-invariants, `docs/**`
- Prior audits, proposals, release notes
- Test layout and CI workflows
- Skill trees (`.opencode/skills`, `.claude/skills`, `.agents/skills`, `SKILL.md` anywhere)
- History tools: `zaxbygraph --help`, `gh auth status`, git remote

Write `identity-packet.md`:

- what this repo is, in one paragraph, cited to README or equivalent
- languages and toolchains with manifest paths
- contract files read, with a one-line description each
- surfaces to map in Phase 2 (commands, APIs, agents, hooks, config keys, skills, CLIs)
- history source: `zaxbygraph` (preferred), `gh` fallback, or local git only
- runtime how-to (exact commands from package scripts or Makefile)
- out-of-scope integrity reminder

Do not begin Phase 1 until this file exists.

## Phase 1 — Full history reconstruction

Prefer ZaxbyGraph. Do not page GitHub into the lead after a successful sync.

### Path A — `zaxbygraph` on PATH, `gh` authenticated, GitHub remote

Collectors run only the commands the lead named:

```bash
zaxbygraph sync --repo OWNER/REPO
zaxbygraph status --format json
zaxbygraph open --format json
zaxbygraph churn --format json
zaxbygraph search "QUERY" --format json
zaxbygraph item N --format json
zaxbygraph related N --format json
zaxbygraph path A B --format json
zaxbygraph sql "SELECT ..." --format json
```

Rules:

- One `sync` per run unless `status` shows `last_error` (resume) or the lead suspects review-only updates that did not bump `updated_at` (`--force`).
- Dump truncated JSON into packets. Do not paste full issue threads into the lead context.
- Forbidden after a successful sync: `gh api --paginate` of issues, pulls, comments, or reviews into the lead.
- `closes` in this DB is a closing keyword in bodies/comments (`close[sd]?`, `fix(e[sd])?`, `resolve[sd]?` plus `#N` / same-repo URL). It is **not** GitHub’s connected-issue / auto-close graph. Commit-message auto-close is absent in v0.1. If a candidate depends on auto-close, label it `UNVERIFIED HYPOTHESIS` or re-read that item’s GitHub thread as a named, single-item fetch — not a corpus re-page.
- Collectors do not cluster, assign severity, or write a taxonomy.

The lead then writes, from packets plus its own targeted `item` / `related` / `sql` re-queries:

- Symptom taxonomy: user-observable clusters with counts, date range, identifiers
- Recurrence map: declared fixed, later reappeared; original fix; what the fix missed. Highest-value defect signal.
- Regression-by-release timeline: which release introduced which class, which contract it violated, whether a guard now exists. Name classes with no guard.
- Open-work map: real defect, duplicate, stale, or misdiagnosed
- Feature-request and abandoned-idea map: asked, closed unshipped, stated reason. Input to Phase 5.
- Velocity and churn: files that dominate `touches` plus git fix-commit directories. Churn concentration is a design-smell signal, not a finding by itself.

Also read release notes, `CHANGELOG*`, and `docs/releases/**` if present (`zaxbygraph` stores GitHub releases in `releases`; changelog files are still read from the tree).

### Path B — `zaxbygraph` missing, `gh` or a GitHub API tool works

Name the gap in `identity-packet.md`. Then paginate until exhausted. Enumerate every issue and every pull request, open and closed. Record title, labels, state, dates, author, linked items, closing commit.

- Read full body and comment thread of every open issue and every open PR.
- For closed items: body plus closing PR description and diff summary. Full threads for reopened items, items that cite a prior issue as regressed, and items labeled regression, platform, or user-reported breakage.

Collectors dump issue and PR records as JSONL. They do not cluster. The lead writes the same six maps as Path A.

### Path C — no GitHub

Say so, then reconstruct from `git log`, tags, and changelog files. Do not invent GitHub counts.

## Phase 2 — Codebase model

Build a structural model independent of the history model.

Adapt the following maps to whatever this repo actually has. Skip a map only with `NOT_APPLICABLE` and a reason.

- Surface map: every user or host-facing element (tools, agents, commands, hooks, config keys, skills, HTTP routes, RPCs, CLI flags). Cross-check registries. Report an element present in one registry and absent in another.
- Durable-state map: files, databases, global config, memory. Who writes, who reads, duplicate facts, schema disagreement.
- Init and first-turn map: every awaited call from process entry to ready, worst-case latency sources, queued vs bare.
- Subprocess map: spawn/exec sites, cwd, stdin, timeout, output bounds, cleanup, portable binary resolution.
- Portability seams: runtime, OS, path, shell branches.
- Prompt and contract layer if the repo is an agent product: system messages per turn, skill loading, token cost by mode.
- Guard lattice: scripts and CI jobs, when they run, what can pass all of them, whether a type or schema would make a class impossible.

Collectors extract registries and call sites. The lead draws the maps.

## Phase 3 — Runtime verification

Static reading is not enough when the repo can be built.

Run the repo's own documented suite. Do not invent a TypeScript or Bun ritual for a Python or Rust tree. Record exact commands and outputs.

Typical sequence, adapted to manifests:

- install
- build
- typecheck or equivalent
- lint if present
- tests
- any project `check:*`, `drift`, `doctor`, or repro scripts

Then, if installable:

- install the built artifact into a clean consumer context
- walk the primary user journey end to end
- exercise failure modes the identity packet named (kill mid-run and resume, corrupt durable state, subdirectory invoke, provider outage if applicable, paths with spaces)
- measure time to first useful action and whether a turn ends without a verifiable artifact

If a platform cannot be exercised, mark every claim about that platform `UNVERIFIED`.

A failing command on a clean checkout is a finding. A flake is a finding.

## Phase 4 — Defect and gap analysis (lead only)

Hunt classes prior reviews miss:

- Emergent failures from interactions between contracts
- Recurrence classes with only symptomatic patches
- Registries or transitions with a producer and no consumer, or the reverse
- Deferred work later paths depend on
- Docs/skills vs code drift
- Ceremony that costs time without changing outcomes
- Integrity-only contract failures per `integrity-policy.md`

Each defect uses this shape:

- id, severity (critical / high / medium / low) plus the rule used
- evidence
- root cause
- blast radius across platforms and hosts
- whether an existing guard should have caught it
- minimal fix
- structural fix that eliminates the class
- test or guard that proves the fix
- novelty_reason
- grounding
- reviewer verdict after Phase 4R

## Phase 4R — Independent review

Seal candidates. Dispatch reviewers per `subagent-protocol.md`. No candidate enters Phase 5 or the report without a verdict other than pending.

## Phase 5 — Enhancement program (lead only, equal weight)

Prefer a small number of high-leverage changes. Every proposal cites an external source retrieved this run.

Generate candidates from:

- Capability parity and leapfrog relative to current adjacent systems (retrieve their current docs; do not recite training memory)
- Reliability as a feature: complete correctly or fail loudly with resumable state
- Cost and speed: budgets, caching, wasted turns
- Context engineering: retrieval, memory, compaction
- Verification depth: spec-to-test, property tests, evidence that cannot be gamed by its producer
- User experience: install, first run, diagnostics, progress, fewer manual steps
- Extensibility: skills, external tools, language coverage
- Simplification: deletion that reduces failure surface

Each enhancement uses this shape:

- id, one-paragraph description
- user-visible outcome
- external evidence with citations
- repository evidence the gap is real
- impact 1–10 and rationale
- effort in engineer-weeks and main risks
- project contracts it touches and how it stays compliant
- dependencies
- definition of done (tests and guards)
- reviewer verdict

Rank by impact divided by effort. Also present the raw-impact top five even if expensive.

No re-proposal of an existing `docs/proposals` or closed issue unless you show what changed in the world or in this repo.

## Phase 6 — Challenge pass

Adversarial re-examination of top findings and top proposals. Attempt to falsify each. Downgrade or remove survivors that fail. Write the challenge log.

Then write the report using `report-template.md`.
