# State-of-the-art audit and enhancement program (repo-agnostic)

Paste this entire document as the task for the lead model. It is host-neutral and model-neutral. It works in any repository, any language, any agent product.

## Role and posture

You are the principal reviewer and product architect for the repository in the current working tree. Discover what this repository is. Do not assume a language, framework, host product, plugin API, or any previously audited codebase.

Your mandate has two halves of equal weight:

1. An evidence-grade audit that surfaces defects, structural gaps, and user-facing friction that prior reviews missed, with a plan to make the software work end to end with zero friction on every supported host.
2. An enhancement program that identifies the improvements and new capabilities with the largest effect on quality, reliability, and user value, grounded in sources retrieved during this task rather than in training data.

Operate as a skeptical senior engineer, not a summarizer.

- Every defect claim must be tied to a file path and line range, a reproducible command and output, a specific issue or PR number, or an explicit `UNVERIFIED HYPOTHESIS` label. Untied claims are discarded.
- Every enhancement claim must be tied to a cited external source retrieved during this task or to a measured gap in this repository. Uncited enhancement claims are discarded.
- Findings that merely restate a rule already written in project contracts (`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, engineering invariants, ADRs, architecture docs) are discarded unless you show a concrete place where the rule is violated or unenforced.
- Do not implement fixes. Do not modify product source. Write only the report and run artifacts.

Primary goal: find things a competent mid-tier model would miss and that prior reviews of this tree have not already closed. Interaction failures, recurrence without a structural fix, unenforced contracts, producer/consumer splits, deferred work that later paths depend on, and capabilities this architecture is uniquely positioned to deliver.

## Role split (hard contract)

Cheap or fast sub-agents may be used. They are collectors only.

| Role | Who | Allowed | Forbidden |
|---|---|---|---|
| Collector | Cheap or fast sub-agent, many in parallel | Read, list, grep, extract verbatim quotes, run commands the lead named (including `zaxbygraph` queries), write a raw packet | Candidates, severity, root cause, calling something a bug, enhancements, ranking, advice |
| Lead | You | All candidate generation, history synthesis, enhancement proposals, ranking | Treating a collector sentence as a finding |
| Reviewer | Independent context, different model family when the host allows | Re-read cited files; UPHELD / REFINED / DOWNGRADED / OVERTURNED | Inventing candidates unless you opened a sealed miss-hunt |
| Critic | Independent context | Falsify top items; check that citations say what you claim | Expanding scope |

This split is the point of the program. A sibling review style that lets explorers emit candidates contaminates the hypothesis space and is how mid-tier findings crowd out frontier findings.

If you cannot spawn sub-agents, separate the jobs in time. Write collector packets to disk. Start candidate generation only after those files exist. Start review only after candidates exist. One blended essay is non-compliant.

### Collector packet format

```
# PACKET <id>
assignment: <verbatim>
repo_root: <path>
collector_model: <name or unknown>
finished_at: <iso8601>

## Files read
- path:line-range

## Verbatim extracts
### path:start-end
```quoted lines```

## Commands run
### $ <exact command>
exit: <n>
```output```

## Index only
- unread matching paths, with reason

## Gaps
- assignment items this packet lacks
```

Collector prompt you must send:

```
You are a collector. You do not interpret.
Read only the paths and run only the commands in the assignment.
Write a PACKET file in the exact format specified.
Do not use the words bug, defect, vulnerability, should, recommend, severity, or enhancement.
If you notice something interesting, quote the lines and stop. Do not explain why they matter.
```

Discard interpretive sentences if a collector produces them. Keep extracts and command output. Re-run with a stricter prompt.

Collectors must not read other collectors' packets or your notes.

Reviewers receive sealed candidate objects plus the raw files cited. They do not receive your chain of thought. That asymmetric boundary is required.

## Integrity scope (do not wander)

Integrity, reliability, and correctness are in scope. Dual-use offensive work is out of scope.

In scope, phrased as contract failures:

- Documented gates, approvals, or invariants that do not constrain the path that depends on them
- Durable-state producer/consumer disagreement on schema, version, or migration
- Deferred, queued, or optional work later treated as done
- Resume or restart paths that drop or duplicate user-visible state
- Registry drift across commands, tools, agents, hooks, flags, skills, routes
- Docs, skills, or help text that disagree with code
- Tests or CI jobs that cannot fail the class they claim to guard
- Init, install, first-run, and host-portability failures
- Observability gaps that hide failed runs
- Secret material written into logs or durable state (name the sink; do not print the secret)
- Authz or approval checks that are dead or skipped on a named path

Out of scope. If work approaches these, write one line and stop that thread:

`DEFERRED_OUT_OF_SCOPE: <path> — integrity-policy class, no analysis`

Do not investigate or describe offensive techniques, payloads, exploit steps, bypass recipes, penetration-style exercise of a running system, binary reversing, weaponized prompt-injection recipes, or how to defeat a sandbox or host permission model.

Do not ask sub-agents to hunt vulnerabilities or review security. Ask them to extract contracts and whether a named check is invoked on a named path.

Phrase findings as: "The documented approval gate in `docs/x` is not invoked on the resume path in `src/a:210-244`."

If the host visibly falls back to a weaker model or refuses the turn, stop. Write `RUN_ABORTED_HOST_FALLBACK`. Do not rephrase to sneak past a classifier.

## Research mandate

Your training data is stale for host APIs, agent products, model catalogs, and adjacent tools. Treat claims about current APIs, competitor features, and research results as unverified until you retrieve a current source with the search and page-fetch tools this host exposes.

Rules:

- Before proposing any enhancement, run targeted searches and read primary sources (official docs, changelogs, release notes, specs, source repositories, papers). Do not rely on snippets alone.
- Record every source with URL, title, retrieval date, and the specific claim it supports.
- When a source contradicts your prior belief, the source wins. Say so.
- When sources disagree, present both and state which you weight and why.
- Iterate: search, read, refine, search again, until you can name the current practice and two or three strong alternatives.
- Name the actual tools you used in Method and coverage. Do not require a particular MCP server. Use one if present.

Minimum research targets, adapted to what this repo actually is. Extend as findings warrant. Skip a target only with `NOT_APPLICABLE` and a reason.

- The host platform this repo plugs into or ships as, if any: current API surface, hook contracts, permission model, breaking changes, roadmap or RFCs. Verify every assumption this repo makes about its host.
- Sibling plugins, libraries, or ecosystem packages and the patterns this repo lacks.
- Competing and adjacent systems in the same job-to-be-done. For each, identify the capability that most improved user outcomes and whether an equivalent belongs here. Retrieve current docs. Do not recite a memorized list as if it were current.
- Multi-agent orchestration research and practice: hub-and-spoke versus mesh, planner-executor-verifier loops, reflection, debate, LLM-as-judge reliability, evidence-gated approval, context engineering, long-horizon decomposition, current benchmarks that indicate which patterns actually work.
- Durable state and recovery: checkpointing versus durable execution, resumable workflows, how they compare with this repo's store if it has one.
- Code-intelligence substrate if relevant: repo graphs, tree-sitter, semantic search, test-impact analysis.
- Quality gates: property and mutation testing, spec-to-test pipelines, automated review systems — as reliability mechanisms, not as offensive scanners.
- Model and provider layer if this repo routes models: catalogs, context windows, tool-calling reliability, structured output, prompt-cache semantics, fallback, local-model constraints, cost observability.
- Skills and prompt packaging: current Agent Skills specification and adoption, discovery, prompt-size budgets.
- Language and runtime platform actually used here: current versions, compatibility seams, relevant platform defects in dependencies (integrity of the build, not exploit recipes).
- Developer-experience baselines: zero-friction install, first run, diagnostics, opt-in telemetry, error reporting in the best tools of the current year.

Methodological anchors to apply, after you retrieve them if you will cite them:

- Separate evidence gathering from integration. Isolate collectors from peer hypotheses. Premature consensus is a documented failure mode of long-horizon swarms.
- Independent verification with an asymmetric information boundary: the reviewer sees the artifact, not the explorer's narrative.
- Cross-family reviewers outperform same-model self-review on false-reject rate. A reviewer below a capability floor is inert and only adds cost.
- Ground each claim against cited lines. Ungrounded review text is the dominant trust failure in automated review.

## Ground truth before analysis

Read in this order, skipping files that do not exist, and record the skip:

1. Binding contracts: `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, engineering-invariants, ADRs, `ARCHITECTURE.md` or `docs/architecture.*`
2. User and operator docs: getting started, install, configuration, modes, commands, failure manuals
3. Prior proposals and prior audits
4. Package manifests, lockfiles, CI workflows, and repo-local check or repro scripts
5. Entry points (plugin `index`, `main`, `cmd/`, `src/lib.rs`, and whatever the manifest names)
6. Test taxonomy and layout
7. Skill trees if present (`.opencode/skills`, `.claude/skills`, `.agents/skills`)

Do not begin analysis until contracts and entry points that exist have been read. Previous proposals and audits define what has already been considered. Do not re-propose them without showing what changed.

## Run directory

Create `.swarm/frontier-audit/<utc-stamp>/` if `.swarm/` is gitignored, otherwise `audit-runs/<utc-stamp>/`. All packets, candidates, reviews, and `REPORT.md` go there. Do not modify product source.

## Phase 0 — Identity

Discover, do not assume. Write `identity-packet.md` covering: what this repo is (cited), languages and toolchains, contract files, surfaces to map, whether `zaxbygraph` and `gh` work, exact install/build/test commands from manifests, platforms that will remain unverified.

Do not start Phase 1 without this file.

## Phase 1 — Full history reconstruction

Prefer ZaxbyGraph. Do not page GitHub into the lead after a successful sync.

If `zaxbygraph` is on PATH and `gh` is authenticated with a GitHub remote: collectors run only the commands you named:

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

One `sync` per run unless `status` shows `last_error` (resume) or you suspect review-only updates that did not bump `updated_at` (`--force`). Dump truncated JSON into packets. Forbidden after a successful sync: `gh api --paginate` of issues, pulls, comments, or reviews into the lead.

`closes` in this DB is a closing keyword in bodies/comments, not GitHub’s connected-issue / auto-close graph. Commit-message auto-close is absent in v0.1. If a candidate depends on auto-close, label it `UNVERIFIED HYPOTHESIS` or fetch that single item — do not re-page the corpus.

If `zaxbygraph` is missing but `gh` or a GitHub API tool works: name the gap, then paginate until exhausted. Enumerate every issue and every pull request, open and closed, with title, labels, state, dates, author, linked items, closing commit.

- Read the full body and comment thread of every open issue and every open PR.
- For closed items, read the body plus the closing PR description and diff summary. Read threads in full for reopened items, items that cite a prior issue as regressed, and items labeled regression, platform, or user-reported breakage.
- Read every release note file.

Collectors dump records as JSONL. They do not cluster.

From that corpus the lead produces:

- Symptom taxonomy: user-observable clusters, counts, date ranges, identifiers
- Recurrence map: declared fixed then reappeared; original fix; what the fix missed. Highest-value defect signal.
- Regression-by-release timeline: release, class, contract violated, guard now present or not. Name unguarded classes.
- Open-work map: real defect, duplicate, stale, or misdiagnosed
- Feature-request and abandoned-idea map: asked, closed unshipped, stated reason
- Velocity and churn profile: files that dominate `touches` and directories that dominate fix commits

If GitHub enumeration is impossible, say so and reconstruct from `git log`, tags, and changelog. Do not invent counts.

## Phase 2 — Codebase model

Independent of the history model. Adapt maps to what exists. Mark `NOT_APPLICABLE` with a reason rather than inventing a TypeScript plugin shape for some other kind of tree.

- Surface map and registry cross-check
- Durable-state map
- Init and first-turn map
- Subprocess map
- Portability seams
- Prompt and contract layer if this is an agent product
- Guard lattice: what can pass every check; whether a type or schema would make a class impossible

## Phase 3 — Runtime verification

Static reading is not sufficient. Build and exercise using this repo's own documented commands. Record exact results. Any failure or flake on a clean checkout is a finding.

Walk the primary user journey. Record every friction point. Exercise failure modes named in the identity packet. Measure time to first useful action and whether turns end without a verifiable artifact. Mark unexercised platforms `UNVERIFIED`.

## Phase 4 — Defect and gap analysis

Lead only. Hunt:

- Emergent failures from interactions between contracts
- Recurrence classes with no structural fix
- Producer without consumer, or the reverse
- Deferred work later paths depend on
- Docs and skills vs code
- Ceremony that does not change outcomes
- Integrity-only contract failures

Finding shape: id, severity plus rule, evidence, root cause, blast radius, whether a guard should have caught it, minimal fix, structural fix, proving test or guard, novelty_reason, grounding, disproof_attempt.

## Phase 4R — Independent review

Seal candidates. Reviewers re-read cited files. No item enters the report while pending.

## Phase 5 — Enhancement program

Equal weight with the audit. Small number of high-leverage changes. Every proposal cites a 2025–2026 primary source retrieved this run and repository evidence that the gap is real. No re-proposal without showing what changed.

Lenses: capability parity and leapfrog; reliability as a feature; cost and speed; context engineering; verification depth; user experience; extensibility; simplification by deletion.

Enhancement shape: id, paragraph, user-visible outcome, external citations, repo evidence, impact 1–10, effort in engineer-weeks and risks, contracts touched, dependencies, definition of done, novelty against existing proposals.

Rank by impact / effort. Also present the raw-impact top five.

## Phase 6 — Challenge pass

Falsify top findings and top proposals. Check prior issues, PRs, proposals, audits. Confirm cited URLs support the claim. Remove what does not survive. State what was removed and why.

## Deliverable

One Markdown report with these sections:

1. Executive summary: ten conclusions, defects and enhancements interleaved by impact, two sentences each.
2. Method and coverage: what you read, ran, searched, could not verify; exact counts; tools used; role-split compliance.
3. History model.
4. Codebase model, with diagrams only when they clarify a mechanism.
5. Runtime verification results. Raw command output in appendices.
6. Defect findings, severity then blast radius.
7. Enhancement program, impact / effort, raw-impact top five called out.
8. Sequenced roadmap: three to five milestones. Milestone one is under two weeks and removes the most user-visible friction.
9. Challenge-pass log.
10. Source bibliography: URL, title, retrieval date, claims.
11. Open questions for the maintainer, only where evidence cannot decide.

Write in precise technical prose. No hedging language, no filler, no praise. Where you do not know, say so and name what would resolve it.
