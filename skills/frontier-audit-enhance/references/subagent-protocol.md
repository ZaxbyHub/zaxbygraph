# Sub-agent protocol

Collectors gather. The lead interprets. Reviewers and critics verify. Mixing those jobs is a defect in the run, not a shortcut.

## Collector (cheap, parallel, many)

Mission: produce raw packets the lead can trust without inheriting a theory.

Allowed tools: read file, list directory, search text, git, `zaxbygraph` query commands the lead named, GitHub list/fetch only when Path B (no zaxbygraph), run a command the lead named, write the packet file.

Packet format (one file per assignment):

```
# PACKET <id>
assignment: <verbatim assignment>
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
```stdout/stderr, truncated with a note if needed```

## Index only
- paths matching the assignment that were not fully read, with reason (too large, binary, missing)

## Gaps
- what the assignment asked for that this packet does not contain
```

Forbidden in a collector packet (scan before accepting):

- words: bug, defect, vulnerability, should, recommend, severity, root cause, enhancement, missing test, race, leak
- imperative advice
- ranked lists of issues
- "this looks wrong"

If a collector violates the ban, discard the interpretive sentences. Keep only extracts and command output. Re-run that assignment with a stricter prompt rather than laundering the judgment.

Collector prompt skeleton the lead must send:

```
You are a collector. You do not interpret.
Read only the paths and run only the commands in the assignment.
Write a PACKET file in the exact format specified.
Do not use the words bug, defect, vulnerability, should, recommend, severity, or enhancement.
If you notice something interesting, quote the lines and stop. Do not explain why they matter.
```

Isolation: collectors do not read other collectors' packets. They do not read the lead's notes. ArcticSwarm isolation applies here.

## Lead (this model)

Mission: all candidate generation.

Inputs: identity packet, contract packet, history packets, surface packets, runtime command logs, and the lead's own re-read of every file it will cite.

The lead may re-read. The lead may run extra commands. The lead may not treat a collector sentence as a finding.

Candidate records go to `candidates.jsonl`, one object per line. Required keys:

- id
- kind: defect | enhancement | hypothesis
- claim
- evidence: path plus lines, and/or command plus output, and/or issue or PR
- quote: verbatim from the tree
- what_the_quote_shows
- novelty_reason: why a mid-tier pass misses this
- grounding: HIGH | MED | LOW
- disproof_attempt: what would make this wrong
- integrity_class or enhancement_lens

LOW grounding does not enter review.

## Reviewer (independent context)

Mission: verify a sealed batch of candidates.

Inputs: the candidate objects, and the raw files those objects cite. Not the lead's chain of thought. Not collector chatter. OpenCodeReview asymmetric reflection applies here.

Outputs, per candidate: UPHELD | REFINED | DOWNGRADED | OVERTURNED, with a one-paragraph reason grounded in a re-read.

Reviewers do not add candidates unless the lead opened a miss-hunt. A miss-hunt packet contains only files and questions, no existing claims. Any miss-hunt candidate still returns to the lead for novelty gating before it can enter the report.

Prefer a different model family than the lead when the host allows it (Tanveer 2026). Never assign review to a collector-tier model.

## Critic (independent context)

Mission: try to kill the top findings and the top proposals.

For each item: find the path or source that would make it wrong; check prior issues, PRs, proposals, and audits; check that cited external URLs actually support the claim. Remove or downgrade anything that fails.

Log removals in `challenge-pass.md`. The report must list what died and why.

## When there are no sub-agents

The lead writes packets to disk as if it were a collector, then starts a new reasoning block that is allowed to interpret. Then a third block that is only allowed to falsify. Time-separated roles are mandatory. One blended essay is non-compliant.
