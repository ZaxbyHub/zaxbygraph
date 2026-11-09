# What stops zaxbygraph from being the best tool of its kind

**Date:** 2026-11-09. **Code reviewed:** `main` at `6009c30` (schema v11, 878 tests green).

## 1. How this review was done, and its limits

Every earlier finding was treated as unproven. Nothing below rests on the October plan.

- **Hands-on run.** I installed `main` and synced this repository's own 63 issues and PRs. Then I ran the workflow the tool exists for: `triage`, `bundle`, `brief`, `similar`, `search` and `eval`.
- **Three independent reviewers.** Each took one area: ingest, the derived tier, and the agent surfaces. Each had to prove its findings with a script or a command. I re-ran a sample of the findings myself; those are marked **(re-run)**.
- **Backtest.** One reviewer rebuilt the backlog as it stood just before each of eight real multi-issue fix PRs, then compared what `bundle` proposed with what was fixed together.

Three limits apply:

- **No agent traces.** `E:\zcode` could not be read. The computer does not offer that folder to cloud sessions. Every usability finding comes from our own runs, not from your agents' sessions.
- **One small corpus.** GitHub access in this session covers this repository only, and GraphQL is blocked. The store is 63 items, synced over REST. Quality numbers are small-sample and say so.
- **Scale figures are synthetic.** The 5k and 20k item stores were cloned from the real one.

## 2. Verdict

The storage side is strong. The parts that are supposed to make it the best tool for agents are not yet trustworthy.

1. **The headline feature does not work out of the box.** On its own repository, `triage` ranks the two finished tracking epics as the top work, and `bundle` proposes them as a bundle. In the backtest, default bundles were 18% precise.
2. **Sync can silently lose items and can be wedged by one comment.** These break the tool's core promise that the store is trustworthy evidence.
3. **The agent surface has sharp edges.** One `sql` query can hang the MCP server for good. In a GraphQL-blocked sandbox, the first MCP read of a store older than 15 minutes marks a healthy store as broken. The most common read costs 10–30k tokens, mostly bot text.
4. **Nothing measures quality.** `eval bundle` does not evaluate `bundle`, and on a REST store all three evaluations return nothing.

None of this is tracked. The only open issues are two epics whose work is finished.

## 3. Tracking state

| Item | State | Correct? |
|---|---|---|
| #22–#34 (the 13-PR program) | All closed and merged | Delivered as specified. The defects below are in what was specified or how it was built. |
| #36–#62 (follow-ups) | All closed; #63 merged during this review | Yes |
| #21 `[Epic]` SOTA program | Open, 13 of 13 sub-issues complete | **Stale.** Its closure test is that `bundle --count 3` returns disjoint bundles the maintainer accepts. This review shows that test fails. Keep it open and point it at §4, or close it and open a new epic. |
| #8 `[Epic]` first review | Open, all seven children closed | **Stale.** Close it. |

**Nothing in §4 to §8 has an issue.**

## 4. Blockers: the core use does not work yet

### 4.1 Bundles are close to chance

Each backtest snapshot is the backlog just before a real multi-issue fix PR. Eight snapshots, 201 open-issue pairs.

| `bundle` variant | Pair precision | Pair recall | Next fix set recovered exactly |
|---|---|---|---|
| default | 4/22 (18%) | 4/17 (24%) | 1 of 8 |
| epics excluded by hand | 10/19 (53%) | 10/17 (59%) | 4 of 8 |

The pair score separates true co-fix pairs from the rest with AUC 0.58, where 0.5 is chance. The causes:

- **The anchor rule filters nothing.** A pair needs one "anchor" signal above zero. Co-change is above zero for 100% of pairs, because any two files that once changed together count. Co-change has no hub discount, so `docs/schema.md` with `cli.py` anchors everything.
- **The affinity floor filters nothing.** 99% of pairs clear it.
- **The strongest signals have the lowest weights.** Title scored AUC 0.70 and lexical 0.68, with weights 2 and 1. Direct files scored 0.51 and co-change 0.53, with weights 4 and 3.
- **Triage score overrides affinity when choosing partners.** Before PR 45, the true partner had the highest affinity and still lost.
- **Bundles always grow to the maximum size.** True two-issue sets come back as three.
- **Hub files block later bundles.** Once one bundle uses `cli.py`, almost nothing else is file-disjoint. Default output was exactly one bundle in all eight snapshots, and unbundled issues get no reason.
- **A bundle can ship with a pair below the floor**, shown as `affinity: 0.0`.

### 4.2 Finished tracking epics rank as the top work

**(re-run)** On the live store, default `triage` returns only #8 and #21, and `bundle` returns the two of them.

- Their score comes from inbound mentions, which count mentions from their own closed children.
- Their "files" are the hotspot tables in the epic body.
- #21 is stored with 13 of 13 sub-issues complete, and the tool does not use that.
- No config can exclude them. The only escape is `--exclude N` by hand.

### 4.3 Triage has almost no signal by default

- The repository's own priority labels (`high`, `medium`, `bug`, `critical-path`) contribute nothing without a config file.
- `hotspot` is 1.0 for every row. Participants, leverage and risk are constant.
- Picking the lowest-numbered open issue predicted the next fix in 7 of 8 snapshots. Triage's top pick did so in 6 of 8.

### 4.4 Nothing measures any of this

- `eval bundle` scores `similar`'s top five candidates. It never runs `bundle`, so it cannot see any defect in §4.1.
- Ground truth uses only GitHub-reported `closes` and `duplicate_of` edges. On a REST store all three modes return `no_ground_truth` with `ok: true`. **(re-run)**
- With truth edges synthesized, `eval localize` scores 0.923, and "the five most-touched files" scores 0.962. Every top-five score ties at 1.0 and ties break alphabetically.

**What to do.** Make the backtest the evaluation: replay the real `bundle` on as-of snapshots and score its bundles. Then fix §4.1 to §4.3 against that number. Weight changes without it are guesses, and the October plan's weights were exactly that.

## 5. Evidence integrity: sync can lose or corrupt data

Each of these was reproduced with a script against the real sync code.

| # | Defect | Trigger | Consequence |
|---|---|---|---|
| 1 | Page-number pagination over a moving sort order (`github.py:166-179`) **(re-run: code confirmed)** | An item on an already-fetched page is updated mid-sync | One item per shift is never listed, and the watermark then passes it. `status` reports a complete sync. Near-certain on a long first sync. |
| 2 | Unbounded `int()` on `#<digits>` (`extract.py:126`) **(re-run)** | Any text with `#` followed by more than 4,300 digits | The item's transaction fails on every run. Sync is wedged for good. Any commenter on a public repo can do it. |
| 3 | The deletion check refuses when a whole chunk is missing (`graphql.py:1315`) | `sync --force` after one real deletion | `--force` fails every time. The next plain sync clears the error and stamps a full sync that never ran its deletion pass. |
| 4 | Rate-limit retry restarts from the original watermark (`sync.py:991`) | Any sync needing more than one rate window | Five hours holding the lock, re-fetching the same items, then failure. |
| 5 | `sync --numbers` treats a repository-level 404 as per-item deletion | Wrong `gh` account, lapsed SSO, or a rename | Live items are marked deleted and their real state is overwritten. The run reports success. |
| 6 | Torn rows **(re-run: seen live)** | An item changes between the listing and its detail fetch | PR #63 was stored `state=open` with `merged_at` set. `open` lists it, and gating treats it as open. Heals on the next sync. |
| 7 | A plain `sync` after `--include-patches` erases the patches | — | Mixed store, flag flipped off. |
| 8 | Closing keywords read from the wrong text | Comments, reviews, issue bodies, code spans | 8 of 41 `closes_keyword` edges (20%) come from text GitHub never honours. The comment-derived ones are all wrong, and one hides a real issue from triage. |
| 9 | Per-issue scan of every PR branch name (`store.py:1016`) | Large repositories | Issue ingest goes from 5 ms at 1k PRs to 30 ms at 10k. The quadratic cost is back. |

Related noise in the same extractors: 21 of 362 `mentions` edges point at items that do not exist. Seven of them are comment ids read as issue numbers.

## 6. Agent surface

### 6.1 A GraphQL-blocked environment breaks first use, then breaks a healthy store

This matters most for cloud sessions, which is where this review ran.

- **(re-run)** Default `sync` fails with a raw 600-character `gh` error. There is no fallback to REST and no hint to try `--source rest`.
- A store built with `--source rest` fails on the next bare `sync`, because the stored source is ignored.
- **(re-run: code confirmed)** The MCP background refresh always uses GraphQL. In a sandbox without it, the first read of a store older than 15 minutes marks a complete store as `complete: false` with a `last_error`. The skill tells agents that state means "do not draw conclusions". Each further read spawns more `gh` processes, with no backoff.
- The MCP `sync` tool has no `source` argument, so an MCP-only agent cannot build a store there at all.

### 6.2 One query can hang the server

**(re-run)** `sql` has no time, step or size cap. A recursive query runs forever, and the MCP server is single-threaded, so the session is dead. `SELECT * FROM comments` at the default limit returns 1.96 MB, about 480k tokens. The docs call a broad query "safe to run".

### 6.3 Reads cost too much and say too little

| Read | Default size |
|---|---|
| `item 51` | 27k tokens, 81% of the comment text from review bots |
| MCP `get_item 49` | 17k tokens of text, 35k on the wire (no comment cap) |
| `similar` (20 candidates) | 8.5k tokens |
| `triage` (13 rows) | 7.6k tokens |
| `bundle` (4 bundles) | 14.6k tokens |
| MCP `related` at depth 5 | 75k tokens, no cap |

- **No bot awareness anywhere.** 599 of 1,260 comments and reviews here are bot-written. They inflate participant counts, drive search ranking and fill `item`.
- **Payloads still lack what an agent needs to decide.** Triage rows have no labels, dates, author or URL. Bundle members carry only number, title and score, so the agent calls `brief` per member.
- **`brief` drops the most useful paths.** Files an issue names that do not exist yet are reduced to a count.
- **`--fields` and `--format jsonl` are silent no-ops** on `search`, `item` and `related`.
- **Every MCP `outputSchema` is the same empty object schema.** Typed output exists in name only, and the wire carries each result twice.

### 6.4 Wrong answers that look like success

- After a failed first sync, `status` and `search` exit 0 with `ok: true` and zero items. Before it they exit 3.
- `related 99999` returns a ghost node. A typo'd path to `file-history` returns an empty list.
- `search` reports `truncated: false` when it cut 36 of 56 hits, and `total_matches` changes with `--limit`.
- `bundle --pool` with closed items returns empty bundles and no reason.
- `snapshot pull` from an unreachable remote reports "no snapshot published".
- Triage hides gated issues with no count. That is why #61 and #62 vanished and only the epics remained.

### 6.5 Search quality

**(re-run)** For "ingest gets slower as the database grows", the items about exactly that (#22, #35) were not in the top four, and 61 of 63 items "matched". The top hit for "quadratic sync" matched only through one bot comment. Paraphrases miss, and the OR fallback matches the whole corpus.

### 6.6 Derived commands do not scale

On synthetic stores, `triage` took 58 s at 5k items and 240 s at 20k. `brief` recomputes the whole triage for one issue. 59 of 79 profiled seconds went to one path-resolution loop (`derive.py:308`).

## 7. Product gaps: what the best version would have

These are new capabilities, in the order I would build them once §4 to §6 are fixed.

1. **A real evaluation with a public number.** Backtest `triage` and `bundle` on as-of snapshots of several repositories, in CI, with a baseline beside every metric. No comparable tool publishes this. It is also the only way the weights stop being guesses.
2. **Work-shape awareness.** Recognise tracking epics, sub-issue parents, task lists and "Follow-up from PR #N" chains. An epic is a container for bundles, not a bundle member. A finished epic should be reported as closeable.
3. **Bot-aware everything.** An `is_bot` flag on actors, default exclusion from demand signals and lexical similarity, and a `--no-bots` read option.
4. **One-call work selection.** A single bounded response: the top bundles, each member with labels, age, a body excerpt and the files it names, with a hard token budget. Today that takes one `triage`, one `bundle` and one `brief` per member.
5. **Claims and outcome memory.** `bundle` should know which issues another agent already took (assignment, an open PR, a branch name). After the fact, it should record whether a proposed bundle was fixed together. That closes the loop into the evaluation in item 1.
6. **Time travel.** `triage --as-of T` and `bundle --as-of T`. The reviewer had to hand-build this for the backtest. It also answers "what did we know when we chose this?".
7. **Semantic retrieval without a new dependency: an experiment, not a commitment.** GitHub's `search/issues` endpoint now accepts `search_type=semantic` and `hybrid` through `gh`. I confirmed the endpoint responds. On three paraphrase queries against this repository it returned nothing, and its rate limit is tight. Try it as one more recorded signal on a larger repository, and keep it only if the evaluation in item 1 improves.
8. **Graceful degradation as a rule.** Automatic REST fallback, the stored fetch source honoured everywhere, and failed background refreshes that never mark a good store bad.
9. **Releases.** The version has been `0.4.0` across 30 commits and six schema versions. There are no tags, no changelog and no published package, and the MCP server reports the same frozen version. An agent cannot tell which tool set it has.
10. **Agent traces as a test input.** Replay real agent sessions to find where agents fall back to `gh` or raw SQL. This review could not do it. It is the most direct evidence of what agents need.

## 8. What held up

- **Read-only guarantee.** 24 read commands left the store byte-identical. Every write, attach, pragma and extension attempt through `sql` was refused.
- **Determinism.** Derived output is identical across hash seeds, and pair scores are symmetric on all 780 pairs.
- **Snapshots.** Publish and pull round-trip with equal corpus hashes. Tampered parts and mismatched hashes are refused, and nothing is installed.
- **Corpus export.** Byte-identical across three runs.
- **Watermark handling.** Verbatim, inclusive, and bumped only inside the item's transaction. An interrupted run resumed from the last committed item.
- **MCP protocol handling.** All advertised versions negotiate correctly, including the stateless 2026-07-28 form. Hostile arguments produced well-formed errors, never a protocol fault.
- **Edge ownership.** Inbound edges and GitHub-reported link streams survive re-ingest. This one is by code reading, not by a run.

## 9. Corrections to the October plan

The earlier plan was wrong in ways this review can now show.

- **Co-change as an anchor signal.** I specified it. It anchors every pair.
- **Hub penalty on files only.** I left co-change and file-disjointness without one.
- **Evaluation ground truth from `closes` only.** That makes evaluation impossible on REST stores, and I designed `eval bundle` around `similar` instead of `bundle`.
- **`closes_keyword` from comments.** I kept it and let it gate triage. GitHub honours closing keywords only in PR descriptions and commit messages.
- **No bot handling.** It was never raised, in any round.
- **"Semantic search needs a model dependency."** GitHub's own semantic search has been available through `gh` since April 2026. Whether it helps is unproven (§7 item 7).
- **Epics.** The plan had no concept of a tracking issue, although the plan itself filed one.

## 10. Suggested order of work

1. **Stop the data loss** (§5 rows 1–5). These undermine everything built on the store.
2. **Build the backtest evaluation** (§4.4), then fix bundling and triage against it (§4.1–4.3).
3. **Make the agent surface safe** (§6.1, §6.2), then cheap and honest (§6.3, §6.4).
4. **Product gaps** (§7), starting with epics, bots and one-call selection.

## Sources

- GitHub changelog, improved issue search GA (2026-04-02): https://github.blog/changelog/2026-04-02-improved-search-for-github-issues-is-now-generally-available/
- GitHub changelog, duplicate detection preview (2026-06-18): https://github.blog/changelog/2026-06-18-duplicate-detection-and-issue-fields-mcp-support-for-github-issues/
