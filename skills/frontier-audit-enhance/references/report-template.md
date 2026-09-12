# Report template

Write one Markdown file: `REPORT.md` in the run directory. Precise technical prose. No hedging filler. No praise. Where you do not know, say so and name what would resolve it.

## 1. Executive summary

Ten most important conclusions. Defects and enhancements interleaved by impact. Two sentences each.

## 2. Method and coverage

What you read, what you ran, what you searched, what you could not verify.

Exact counts: issues, PRs, files read, collector packets, candidates emitted, candidates overturned, external sources.

Tools actually used (search product names, `zaxbygraph` or `gh` or not, runtimes). History path: A (zaxbygraph), B (gh pagination), or C (git only).

Role split compliance: collector count, whether any collector packet was discarded for interpretation, reviewer model identities, whether review was time-separated or process-separated.

## 3. History model

Phase 1 artifacts, condensed.

## 4. Codebase model

Phase 2 maps. Diagrams only when a picture clarifies a mechanism (Mermaid allowed).

## 5. Runtime verification

Phase 3 results. Raw command output in appendices, not in this section.

## 6. Defect findings

Ordered by severity, then blast radius. Only reviewed items. Include novelty_reason and grounding on each.

## 7. Enhancement program

Ordered by impact / effort. Call out the raw-impact top five separately.

## 8. Sequenced roadmap

Three to five milestones. Each lists the defects and enhancements it contains, why it sits there, guards it adds, and the user-visible outcome. Milestone one is achievable in under two weeks and removes the most user-visible friction.

## 9. Challenge-pass log

What was removed or downgraded and why.

## 10. Source bibliography

Every external source: URL, title, retrieval date, claims it supports.

## 11. Open questions for the maintainer

Only where a decision cannot be made from evidence.
