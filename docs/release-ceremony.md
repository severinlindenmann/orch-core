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

Reads the root from Proton, checks it is the root embedded in `keys.py` and `install.sh`, re-signs the list with a new `issued_at` (same
`min_serial` and `revoked`), writes `release/keys/revocations.json`, bumps the `install.sh` floor.
**Paste:** the printed `issued_at` and file list. Commit and PR the two files. The list is valid 30 days, clients warn
10 days before it expires, and after expiry updates fail closed (running installs keep running).

## If something fails halfway

The script prints a fixed message (no child output) and removes its temp dir; the repo tree is only written at the
very end, so a failure earlier leaves it untouched. What already happened outside the repo:

Each Proton item's ID comes from the vault listing and is printed only after the read-back of that item matched
(`proton item id (root): ...`, `proton item id (ci): ...`). It is not secret: record both after the run. Items are only
ever named by ID, never by title. If a run stopped before an ID was printed, look the ID up with
`pass-cli item list --vault-name Agents --output json` (it lists `id` and `title` per item) and act on that ID.

| Failed at | Already done | Recovery |
|---|---|---|
| preflight | nothing | fix the cause (login, environment, policy, pass-cli errors), rerun |
| storing the root | maybe the root item | open Proton, delete the item with the printed root ID, rerun |
| CI key to Proton / GitHub secret | root item, maybe CI item | delete the items with the printed root and CI IDs; the secret is overwritten on rerun; rerun |
| writing files | all external steps | the script restores the tree itself. If it says `restoring it failed for: <paths>`: `git restore -- <each tracked path listed>` and `rm <each untracked path listed, by exact name>`; then delete the Proton items with the printed IDs and rerun |
| secret list shows MISSING | files written | set the secret again from the CI item: `pass-cli item view --vault-name Agents --item-id <CI ID> --field password \| gh secret set ORCH_RELEASE_KEY --env release --repo severinlindenmann/orch-core` |

`init` refuses while the root item exists, so a half-finished run is always cleaned up by hand first. Deleting is safe
only while nothing has been published: once `release/keys/` is merged, rotation (a new root) is a separate task.
`resign-revocations` can simply be rerun after any failure. Never paste a seed, never put one in a command line.
