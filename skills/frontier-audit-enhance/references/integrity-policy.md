# Integrity policy

Integrity and reliability are in scope. Dual-use offensive work is out of scope. The purpose is to keep long-horizon frontier sessions on the model the user selected, and to keep the report useful without turning into an attack manual.

## In scope (integrity, reliability, correctness)

Write these as reliability or contract failures, never as attacks.

- A documented gate, approval, or invariant that does not actually constrain the path that depends on it.
- Producer and consumer disagree on schema, version, or migration of durable state.
- Work that was deferred, queued, or made optional and is later treated as done.
- Session, resume, or restart paths that drop or duplicate user-visible state.
- Registry drift: a command, tool, agent, hook, flag, or skill listed in one place and absent in another.
- Docs, skills, or help text that describe behavior the code no longer has, or the reverse.
- Tests or CI jobs that cannot fail the class they claim to guard.
- Init, install, first-run, or host-portability failures (wrong cwd, wrong runtime, path seams, missing binaries).
- Observability gaps that make a failed run indistinguishable from a successful one.
- Secret material written into logs, transcripts, or durable project state as a data-handling defect. Name the sink. Do not print the secret.
- Authz or approval checks that are dead, skipped, or inconsistent with the documented contract, described as "this check does not run on this path."

## Out of scope (do not investigate, do not describe)

If a collector packet or a lead thought approaches any of the following, record one line and stop that thread:

`DEFERRED_OUT_OF_SCOPE: <path> — integrity-policy class, no analysis`

Classes:

- Offensive techniques, payloads, exploit steps, or bypass recipes.
- Penetration-style exercise of a running system.
- Binary reversing, fuzzing-for-crash, or memory-corruption writeups.
- Weaponized prompt-injection recipes or tool-poisoning how-tos.
- Supply-chain attack construction (malicious package recipes, provenance forgery methods).
- Anything that would require describing how to defeat a sandbox, host permission model, or classifier.

Do not use those words in collector prompts. Do not ask sub-agents to "hunt vulnerabilities," "attack the sandbox," or "review security." Ask them to extract contracts, call graphs of approval functions, and whether a named check is invoked on a named path.

## How to phrase an integrity finding

Bad: "Attackers can bypass the sandbox by ..."

Good: "The documented approval gate in `docs/x.md` is not invoked on the resume path in `src/a.ts:210-244`. A resumed run proceeds without the recorded approval fact. Guard `tests/foo` does not cover resume."

## If the host already tripped a safety fallback

Stop. Write `RUN_ABORTED_HOST_FALLBACK` in the run directory with the visible notice. Do not rephrase the same request to sneak past a classifier. Resume only after stripping out-of-scope language from the next turn.
