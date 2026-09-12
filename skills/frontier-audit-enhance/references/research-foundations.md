# Research foundations (retrieved 2026-09-11)

This program is not a restatement of `codebase-review-swarm`. That skill lets cheap explorers emit candidates. This skill forbids that. The split below is the reason.

## Premature consensus and hypothesis contamination

ArcticSwarm (arXiv 2609.01870, 2026-09) shows that long-horizon multi-agent research fails when workers see peer hypotheses too early. Separating evidence gathering from evidence integration, and isolating some search tasks from peer claims, beat larger swarms on BrowseComp-Plus (88.3% with GPT-5 vs 70.6% MiroFlow rerun). Premature consensus is the named failure mode.

Implication: collectors must not see each other's theories and must not produce theories. The lead sees raw packets only.

## Asymmetric verification beats self-critique

OpenCodeReview (arXiv 2608.09290v2, 2026-08) isolates three failures of agent review: misaligned retrieval, coherence/efficiency on multi-file changes, and hallucinated comments. Their Independent Reflection filter uses an asymmetric information boundary — the reflector sees the artifact under review, not the explorer's tool-augmented narrative. That removes hallucinated comments without the self-reinforcing bias of intrinsic self-critique. Same paper: rule-guided dispatch plus grounded file review beat Claude Code and Codex backends on AACR-Bench at 5–15× lower token cost.

Implication: reviewers and critics re-read the cited files. They do not inherit the lead's exploration story as evidence.

## Reviewer capability has a floor, and same-model self-review is noisy

Tanveer, arXiv 2609.04270 (2026-09): in execute-review-revise pipelines, a cross-family mid-tier reviewer improved final accuracy 12 points with zero damaged answers. Same-model self-review had the highest detection recall (0.85) but rejected correct work 35% of the time vs 2% for the cross-family reviewer. A reviewer below a capability floor changed zero of 100 answers and doubled cost.

Implication: prefer a different model family for review and critic when the host allows it. Never use a collector-tier model as reviewer. If only one model exists, isolate the review pass in a fresh context with sealed packets and no prior chain of thought.

## Independent reviewers, then arbitration

LinkedIn's 2026 multi-agent review platform (InfoQ, 2026-08) treats review as production infrastructure. Multiple independent reviewers using distinct models; convergence is strong evidence; unique findings are verified separately, not dropped. Comment acceptance was 63.9% overall, 80% for logic errors, 100% for concurrency bugs, on 5,230 sampled comments.

Adversarial review (proposer vs challenger vs arbiter) is the same shape. Quest (KjellKod, 2026) adds artifact-only handoff — agents do not share chat history.

Implication: reviewers do not chat with the lead. They consume files.

## Grounding is a separate job from generation

HalluJudge (arXiv 2601.19072, FSE 2026): reference-free context-alignment scoring of review comments reaches F1 0.85 at about $0.009 per judgment. Tree-of-thought grounding beat direct assessment. 67% of judgments aligned with developer preference in production.

Implication: every kept claim carries a grounding grade (HIGH / MED / LOW) against the cited lines. LOW is discarded.

## Repository context before file judgment

Magistrate (OpenReview V9pJJy2uRc / SSRN 5973895): hierarchical delegator plus parallel detectors over full repo context, not isolated diffs. 2.2–5.5× F1 over single-shot; 0.6% hallucination on the best run; 997 valid issues humans missed on the evaluated slice.

RepoReviewer (arXiv 2603.16107): decompose into acquisition, context synthesis, file analysis, prioritization, summary. Collapsing those into one pass reduces relevance and weakens prioritization.

Implication: Phase 0 identity and context packets exist before any candidate is written.

## What this program refuses from those papers

- Magistrate-style IssueDetectors that both explore and judge in one agent. Exploration is a collector. Judgment is the lead.
- Hydra-Reviewer-style comment generation as the goal. The goal is defects plus high-leverage enhancements with citations, not comment BLEU.
- Code Broker-style parallel assessors that each invent findings. Parallelism is for collection and for independent verification, not for candidate invention.
- Any design where a cheap model proposes and a frontier model merely ranks. Ranking a poisoned candidate list hides the findings this program exists to produce.

## Enhancement research posture

Training cutoffs are stale for host APIs, agent products, and durable-execution practice. Before proposing an enhancement, retrieve current primary sources (official docs, changelogs, RFCs, papers, competitor feature pages). Record URL, title, retrieval date, and the exact claim used.

Durable-state comparisons in 2026 treat LangGraph checkpoints as durable data and Temporal / Restate / Inngest / DBOS as durable execution. Cite the current source, not this paragraph, when proposing work in that area.
