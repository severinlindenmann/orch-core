# Release key ceremony: runbook for the lead

Script: `scripts/release/ceremony.py`, always run as `python3 -I`. Plan and decision: P2-B (manager approved option (a)).
The root key lives only in Proton Pass (vault `Agents`, item "orch release root key"). The script never prints, logs or
writes a key; secrets only travel on stdin to `pass-cli` and `gh`.

## Before `init`

1. Opus review of the script and its tests has passed; the manager has created the GitHub environment `release` with
   a deployment branch policy of exactly `main` (the script checks this and refuses otherwise).
2. `scripts/pass-login.sh` has logged in; `gh` is authenticated as the owner account.
3. Clean worktree on a feature branch (never `main`) with `release/keys/` absent.

## init (once)

    python3 -I scripts/release/ceremony.py init

It stores the root key (`root-1`) and the CI key (`rk-1`, core, channel `main`, 180 days) in Proton, sets the
`ORCH_RELEASE_KEY` secret of the `release` environment, signs the first revocation list (min_serial 1, nothing
revoked), writes `release/keys/{root-1.pub.json,rk-1.cert.json,revocations.json,root.env}`, embeds the root public key
in `keys.py` and `install.sh`, and bumps the `install.sh` floor.

**Paste to the manager (stdout only):** the key id, the fingerprint, the changed-file list, and the last line
(`ORCH_RELEASE_KEY in environment release: present`). Nothing else exists to paste. Then open the PR with the public
outputs (Opus check of the diff; before merge confirm `python3 scripts/release/floor.py --check install.sh
release/keys/revocations.json` exits 0).

## resign-revocations (every 14 days, issue #1778)

    python3 -I scripts/release/ceremony.py resign-revocations

Reads the root from Proton, checks it is the published `root-1`, re-signs the list with a new `issued_at` (same
`min_serial` and `revoked`), writes `release/keys/revocations.json`, bumps the `install.sh` floor.
**Paste:** the printed `issued_at` and file list. Commit and PR the two files. The list is valid 30 days, clients warn
10 days before it expires, and after expiry updates fail closed (running installs keep running).

## If something fails halfway

The script prints a fixed message (no child output) and removes its temp dir; the repo tree is only written at the
very end, so a failure earlier leaves it untouched. What already happened outside the repo:

| Failed at | Already done | Recovery |
|---|---|---|
| preflight | nothing | fix the cause (login, environment, policy), rerun |
| storing the root | maybe the Proton item | in Proton delete "orch release root key" if present, rerun |
| CI key to Proton / GitHub secret | root item, maybe CI item | delete both Proton items ("orch release CI key rk-1" too); the secret is overwritten on rerun; rerun |
| writing files | all external steps | do NOT rerun `init` (it would refuse). Delete both Proton items, rerun; the old secret is overwritten |
| secret list shows MISSING | files written | set the secret again from the Proton item "orch release CI key rk-1": `pass-cli item view --vault-name Agents --item-title "orch release CI key rk-1" --field password \| gh secret set ORCH_RELEASE_KEY --env release --repo severinlindenmann/orch-core` |

`init` refuses while the root item exists, so a half-finished run is always cleaned up by hand first. Deleting is safe
only while nothing has been published: once `release/keys/` is merged, rotation (a new root) is a separate task.
`resign-revocations` can simply be rerun after any failure. Never paste a seed, never put one in a command line.
