# Remote and multi-user use of zaxbygraph (and how it pairs with zmem)

**Status:** this is a proposal, and nothing in it is built yet. It is a
companion to `sota-and-bundling.md`, and it depends on that plan's §12.3
(`export-corpus` and `corpus_sha256`).

**Basis:** every zaxbygraph code claim was checked at `b47913e`. The zmem
claims come from `zaxbysauce/zmem` `README.md`, `docs/CLOUD.md` and
`docs/FLEET.md`, read from `raw.githubusercontent.com` on 2026-10-25.

## 1. The asymmetry that drives the design

| | zaxbygraph | zmem |
|---|---|---|
| **Source of truth** | GitHub. The store is a rebuildable cache (README: "Do not commit `history.db` — it is a rebuildable cache"). | The store itself. The memories exist nowhere else. |
| **Writers** | `sync` only, and only from GitHub data | Every agent session, through hooks and tools |
| **Conflict model** | None needed. Two syncs of the same repo converge on the same GitHub state. | It needs one, and zmem already has it: a single-writer hub, a durable op queue, untrusted-peer ingest (FLEET T-02..T-11). |
| **Trust risk of a shared copy** | Fabricated *evidence*. A tampered snapshot could claim GitHub said something it did not. | A persistent *instruction-injection* channel (CLOUD.md, "Trust model"). |

zaxbygraph has no write-merge problem. Its remote and multi-user problem is
how to deliver a **verified warm start** cheaply. Once delivered, the
existing watermark makes every later `sync` fetch only the delta. That is
the one property git can supply well, and it is all this design asks of
git.

## 2. Defect to fix first: loopback proxy remotes

Remote sessions (Claude Code Remote, cloud sessions) reach their repo
through a local git proxy, with remotes of the form
`http://local_proxy@127.0.0.1:<port>/git/<org>/<repo>`. zmem's CLOUD.md
documents this shape and handles it with `ZMEM_PROXY_FORGE_HOST`.

zaxbygraph does not. **VERIFIED:**
`repo._parse_remote_info('http://local_proxy@127.0.0.1:41233/git/ZaxbyHub/zaxbygraph')`
returns `None`, so `remote_info` raises "could not parse origin remote"
(`repo.py:152-154`). In such a session, every command run without `--repo`
fails before it reaches the database.

**Fix (PR R0).** Mirror zmem's rule exactly:

- A loopback host (`127.0.0.1`, `localhost`, `::1`, `[::1]`) with a path
  starting `git/` rewrites to `<forge>/<org>/<repo>`.
- The forge defaults to `github.com` and can be overridden with
  `ZAXBYGRAPH_PROXY_FORGE_HOST`.
- An empty value opts out, and an unparseable value disables the rewrite.

zmem's namespace (`project:github.com/<org>/<repo>`) and zaxbygraph's store
key (`<host>/<owner>/<repo>`) then derive the same identity in the same
session. The proxy port, which changes every session, must never reach
either key; otherwise one repo's store fragments across sessions.

## 3. Git-delivered snapshots (PR R1, R2)

### 3.1 Publish: `zaxbygraph snapshot publish`

1. **Take a consistent copy.** Run under the existing whole-run sync lock
   (`sync.acquire_sync_lock`). Copy the store with
   `sqlite3.Connection.backup()`, which is stdlib and consistent under WAL,
   into a temp file. Then run `VACUUM` on the copy only.
2. **Refuse an incomplete or broken store.** Refuse if `status` reports
   `complete: false` or a non-null `last_error`. A partial corpus must never
   become someone else's evidence.
3. **Compress and write the manifest.** Gzip the copy and write
   `manifest.json` with these fields:
   - `schema: zaxbygraph.snapshot.v1`
   - `producer_version`
   - `user_version`
   - `repo`, `host`
   - `watermark`
   - `last_full_sync_at`
   - `corpus_sha256` (from `export-corpus`, §12.3)
   - `db_bytes`, `db_sha256`
   - `gz_bytes`, `gz_sha256`
   - `parts[]`, used when the gzip exceeds 95 MB and is split to stay under
     GitHub's 100 MB per-file limit
   - `publisher`: the gh login and machine hostname, so the provenance is
     visible
4. **Push.** Push the files as a **single-commit orphan** to the snapshot
   ref and force-update that ref. History never grows, so the store repo
   stays one snapshot in size.

**Where the snapshot lives.** Pick one per repo:

| Mode | Ref | Visibility | Credential | Notes |
|---|---|---|---|---|
| **A. Inside the target repo** (preferred for teams) | `refs/zaxbygraph/snapshot`, a non-`heads` ref, so a default `git clone` (refspec `refs/heads/*`) never downloads it | Exactly the target repo's. Private issue text cannot leak to anyone who could not already read it. | `gh auth setup-git`, so `gh` is still the only credential, consistent with AGENTS | INFERENCE: GitHub accepts pushes to custom `refs/<name>/*` namespaces. Verify with one push and `git ls-remote` before filing. If it is refused, use a `zaxbygraph-snapshot` branch, accepting that default clones fetch it. |
| **B. Dedicated private store repo** (personal travel) | `refs/heads/<host>/<owner>/<repo>` in e.g. `you/zaxbygraph-stores` | That repo's. It must be **private** and must not be readable by anyone without read access to every source repo. | Same | One repo serves many source repos. `doctor` warns if the store repo is public. |

**Single writer.** For teams, exactly one publisher should run `sync` and
`snapshot publish`. That can be a machine, or a scheduled GitHub Action in
the target repo that uses `GITHUB_TOKEN` with `contents: write`, which is
not a PAT. Everyone else only pulls.

### 3.2 Pull: `zaxbygraph snapshot pull [--from REMOTE] [--verify-sample N]`

1. **Fetch.** `git fetch` the ref into a cache directory beside the store.
   The repo's working tree is never touched.
2. **Verify the artifacts:**
   - check `gz_sha256` on every part, then decompress, reading at most
     `db_bytes`;
   - check `db_sha256`;
   - run `PRAGMA quick_check`;
   - reject the snapshot if its `user_version` is greater than this build's
     `CURRENT_USER_VERSION`, with the existing "newer than this build"
     message (`db.py:607-611`).
3. **Install.** Install with `os.replace` under the sync lock, and only
   when **no local store exists** or the snapshot's watermark is newer.
   Never overwrite a newer local store unless `--force` is passed. Then run
   `init_schema` on it so an older snapshot migrates forward.
4. **Record provenance.** Set `sync_state` provenance to
   `snapshot:<db_sha256>@<publisher>`. `status` reports it, so every reader
   knows the evidence arrived through a third party.
5. **Optional spot check (`--verify-sample N`).** Re-fetch N random items
   through `gh` (bulk GraphQL, one or two calls) and compare each item's
   §12.3 `content_hash` against the snapshot. Any mismatch on an item whose
   `updated_at` has not moved fails the pull. This is the defence against a
   tampered snapshot, and it costs a fixed, small number of API calls.
6. **Top up.** Run `zaxbygraph sync`. The watermark makes it fetch only
   what changed since the publisher's sync.

The result is a warm start for a laptop on hotel Wi-Fi, a fresh cloud
container, or a new teammate: one git fetch plus a delta sync, instead of a
cold sync of the whole history.

**Offline use, such as on a plane.** After a pull, every read works with no
network. Freshness reports the age honestly.

The MCP server's stale-read refresh would try to sync and fail. In that case
it must **fail open and keep answering** from the local store, with
`freshness.refreshing: false` and a `refresh_error`. INFERENCE: today the
refresh runs in a background job (`mcp_server._start_job`), so reads should
already be unaffected. Confirm with a test that runs with the network
blocked.

### 3.3 Why not the alternatives

| Alternative | Why it was not chosen |
|---|---|
| Commit `history.db` to a normal branch | It bloats history forever, and the README forbids it. |
| Git LFS | It is an extra install and quota, and `gh` alone cannot drive it. |
| Release assets (`gh release upload`) | Workable, but a non-draft release notifies watchers, while draft assets are readable only by people with push access. That is a poor fit for read-only teammates. |
| Actions artifacts | 90-day retention, and they need a run ID to locate. They are fine as a CI-internal hand-off, poor as the canonical location. |
| Network-mounted store files | SQLite plus WAL over a network filesystem risks corruption, the same reason zmem refuses network paths (README, "Hermes Agent — remote"). |
| A remote zaxbygraph MCP server over HTTP | It needs an always-on host, authentication and TLS, and it gives nothing offline. Defer it. zmem's fleet hub already covers the always-on case for memory. Revisit only if snapshot pulls prove too stale for a team's cadence. |

## 4. How zaxbygraph and zmem fit together

**Keep the two channels separate.** A zmem sync repo is write access to an
agent's memory (CLOUD.md, "Trust model"). A zaxbygraph snapshot ref is read
access to GitHub-derived evidence. Never put them in one repo or behind one
push credential. A compromised snapshot publisher must not gain the ability
to write `user:global` memory rows, which are injected into every session.

**Let each tool keep the remote model that fits its data:**

| Situation | zmem (existing) | zaxbygraph (this plan) |
|---|---|---|
| Travelling laptop on your tailnet | Fleet hub plus per-machine broker (FLEET T-08..T-10) | Snapshot pull once, then delta sync |
| Laptop offline | T-11 broker snapshot (72 h expiry, labelled `[OFFLINE SNAPSHOT …]`) | Local store, fully readable, with freshness age reported |
| Cloud/CCR session (no tailnet) | Tier 1 pack, or Tier 3 private sync repo | Snapshot pull from mode A or B, then delta sync. Needs R0 for the proxy remote. |
| Teammates | Hub with per-machine principals (T-02) | One publisher (machine or Action); everyone pulls |

**Integration points that are worth building:**

1. **One identity key.** R0 makes zmem's
   `project:github.com/<org>/<repo>` and zaxbygraph's
   `github.com/<org>/<repo>` derive from the same normalized remote,
   including through the proxy. Neither tool should import the other. A
   shared, documented normalization rule is enough, plus one test vector
   file asserted in both repos.
2. **Evidence-pinned lessons.** When a zmem lesson is captured from
   zaxbygraph output (for example "#412 and #418 share `sync.py`; fixed
   together in PR #430"), the capture can carry
   `evidence: zaxbygraph:<repo>@corpus_sha256=<…>` with the item numbers.
   zmem's provenance lookup (README, "Provenance source lookup") can then
   lead back to a reproducible corpus. This needs no code in zaxbygraph
   beyond §12.3; it is a capture convention in zmem.
3. **Bundle claims stay on GitHub, not in either store.** Multi-user bundle
   coordination uses GitHub assignment, which the `triage` assignee gate
   already reads, refreshed by `sync --numbers`. Neither zmem rows nor a
   zaxbygraph snapshot should become a claim ledger. Each would be stale on
   other machines exactly when it matters.

## 5. PRs

| PR | Scope | Depends on |
|---|---|---|
| R0 | Loopback-proxy remote rewrite, plus `ZAXBYGRAPH_PROXY_FORGE_HOST` (four states, matching zmem), plus a shared normalization test-vector file | — |
| R1 | `snapshot publish` (backup → vacuum → gzip → split → manifest → orphan force-push to mode A/B ref) | §12.3 `export-corpus` (for `corpus_sha256`) |
| R2 | `snapshot pull` (fetch → verify → quick_check → version gate → atomic install → provenance → optional `--verify-sample`) and `status` provenance field | R1 |
| R3 | Docs: travel and team recipes, a scheduled-Action example for the single publisher, and the separation rule from §4 | R2 |

**Testing.** Tests need no network. Point a bare local repo at a temp
directory (`git init --bare`) and use it as the remote for publish/pull
round trips on the Windows and Linux legs.

**Acceptance criteria to carry into the issues:**

- A tampered `gz` or `db` hash is refused.
- A newer-schema snapshot is refused.
- An older local watermark is replaced, and a newer one is kept.
- An incomplete store refuses to publish.
- The round-trip `corpus_sha256` is equal on both sides.
