# Releasing orch: operator guide

One page for the owner. Design: `docs/design/install-update.md` (sections 3 and 4). Workflow: `.github/workflows/release.yml`.

## What happens

- Push to `main` or `develop`: build the dashboard page, stamp version `2.0.0.dev<run number>`, build both wheels,
  check them, write the manifest (seq = run number, expires in 21 days), sign it, and publish two things:
  an immutable release `v<version>` (wheels, `constraints.txt`, `release.json`) and the fixed release
  `channel-<main|develop>` whose `channel.json` is replaced (never moved back to an older seq).
- Every Monday 04:17 UTC the workflow re-signs both channels with fresh `issued_at`/`expires_at`. One rule decides
  what CI may sign (`scripts/release/trusted.py`, `trusted_current`): the current `channel.json` must verify NOW
  (certificate valid now, not revoked by `release/keys/revocations.json`; an expired manifest is accepted only as the
  reason to refresh), the immutable release of exactly that version must verify and agree with it, and the git tag must
  point at its commit. Refresh re-signs that version, seq and commit and nothing else. Publish uses the same function
  and never moves the pointer to a lower seq. Every failure fails the job; only a channel that was never published is
  skipped.
- Manual test channel: Actions, "release", Run workflow, channel `test-<name>`. It uses the test key and environment.

## One-time setup (owner)

Only the owner's account can push workflow files. Push `.github/workflows/release.yml` and the `scripts/release/` files.

| What | Where | Value |
|---|---|---|
| Environment `release` | Settings, Environments | Deployment branches: `main` and `develop` only; required reviewer optional (adds a click per release) |
| Secret `ORCH_RELEASE_KEY` | environment `release` (not a repository secret) | base64 of the 32-byte Ed25519 seed of the release key. Create it with `rootkey.py release-key`, which pipes it into `gh secret set ORCH_RELEASE_KEY --env release`; never paste it into a file or a terminal history |
| Variable `RELEASE_KEY_ID` | repository variable | for example `rk-1`: the key id of the certificate in `release/keys/` |
| Environment `release-test` | Settings, Environments | for `test-*` channels only; no branch restriction needed |
| Secret `ORCH_RELEASE_KEY` + variable `RELEASE_TEST_KEY_ID` | environment `release-test` / repository variable | a separate test key whose certificate lists `test-*` |
| Repository files | in git | `release/keys/<key id>.cert.json` (root-signed certificate), `release/keys/revocations.json` (root-signed list) |
| Tag protection | Settings, Rules, Tags | protect `v*` tags (no deletion, no update) so a version tag cannot be moved to another commit. Recommended; the workflow also checks tag and manifest commit agree |
| Immutable releases | Settings, General | on. Note: this must still allow replacing `channel.json` on `channel-*`; if GitHub blocks that, the channel pointer needs another home (see Open issues) |

The root key is never on GitHub. Only the sign step reads the secret (as the variable `ORCH_RELEASE_SIGNING_KEY`);
nothing prints it. Do not enable step debug logging on the release environment.

## Re-certify the release key (every 180 days)

The workflow warns and opens or updates the issue "release key expires on <date>" 30 days before `not_after`, and
every release fails after it.

1. Generate the new key: `rootkey.py release-key` (sets the secret in the environment and prints the public key only).
2. Certify with the root key, which stays in Proton Pass (`pass-cli run --env-file release/keys/root.env -- ... rootkey.py certify ...`).
3. Commit `release/keys/rk-<n>.cert.json`, set the repository variable `RELEASE_KEY_ID` to `rk-<n>`, merge.
4. Check the next release run is green. Overlap is one release.

Compromise: the owner signs a revocation list (`rootkey.py revoke`), certifies a new key, commits
`release/keys/revocations.json`, and publishes a release.

## Open issues

- With repository-wide "immutable releases", `gh release upload --clobber` on `channel-*` may be refused. Verify on the
  first real run; fallback is to publish the pointer as a file in a branch or Pages.
- The merge-time title list for the changelog uses `git log --no-merges` since the commit in the current `channel.json`.
