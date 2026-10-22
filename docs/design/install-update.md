# One-line installer, signed releases, `orch update`

Status: design draft, 22 Oct 2026. Owner request: `curl -LsSf https://…/install.sh | sh` installs uv, orch core and the
dashboard; `orch update` keeps them current. Owner decisions (22 Oct) are taken as given: install.sh is served from
`https://raw.githubusercontent.com/severinlindenmann/orch-core/main/install.sh`; CI builds both wheels with the dashboard
page **prebuilt** (the user needs only uv: no Node, no git); releases are signed GitHub release assets with a per-channel
manifest. Add-on versions, the add-on catalog and the dashboard "Versions" view are in
[addon-platform.md](addon-platform.md). Part A decisions (D1-D29) are untouched.

## 1. Today

- Install: `uv tool install --force --with "orch-dashboard @ $G#subdirectory=addons/dashboard/server" "$G#subdirectory=plugins/orch-core"`
  with `G=git+https://github.com/severinlindenmann/orch-core@<sha>`. uv clones the repo and runs
  `addons/dashboard/server/hatch_build.py`, which runs `npm ci && npm run build` (Node >= 24) unless
  `src/orch_dashboard/static/index.html` already exists. That hook already reuses a prebuilt page: CI only has to build
  the page first.
- Versions: `orch-core` is `2.0.0.dev0` (pyproject and `orch.__version__`), `orch-dashboard` is `0.1.0`. `orch --version`
  prints the fixed line `orch v2 (in development)`. The real identity of running code is the **release digest**
  (`orch.host.release.code_digest`, D-P2-4): `orch host enable` copies the package to `releases/<version>/`, the host
  reports the digest in its welcome, and `enable` restarts a host or runner whose digest differs (host: drain, then
  restart; runner: only with no live runs unless `--restart-runner`).
- Nothing records how orch was installed, and nothing checks for a newer version.

## 2. Versions and channels

- **One release train.** Core and dashboard share one version per build: `2.0.0.devN`, N = `github.run_number` of
  `release.yml`. The run number is monotonic per workflow across branches, so N orders every build of both channels.
  PEP 440 orders `2.0.0.dev41 < 2.0.0.dev42 < 2.0.0`; a later stable line (`2.0.0`, `2.0.1`, …) needs no format change.
- The dashboard wheel requires `orch-core==<same version>` (exact pin, written by the stamp step). An installed pair is
  therefore always a pair from one build.
- **Stamping.** `scripts/release/stamp.py V CHANNEL COMMIT` rewrites both `pyproject.toml` versions and
  `orch/__init__.py`, and writes `orch/_build.json` `{version, channel, commit, built_at}`. Source checkouts have no
  `_build.json` and stay `2.0.0.dev0` ("development build").
- **`orch --version`** prints `orch 2.0.0.dev42 (channel develop, commit 1a2b3c4, release sha256:9f…)`; without
  `_build.json`: `orch 2.0.0.dev0 (development build, release sha256:9f…)`. `--json` gives the same fields. The
  release digest stays the restart key; the version is what people and the update check compare.
- **Channels:** `main` (push to main, which follows develop since the 15 Oct cutover) and `develop` (push to develop).
  `test-<name>` channels exist only for the CI e2e job (§9) and are signed by a test-only key (§4.4).

## 3. Release workflow (`.github/workflows/release.yml`)

> Workflow files can only be pushed with the owner's account. The implementer writes the file on the branch and checks
> it with `actionlint`. The owner pushes it. No agent pushes anything under `.github/workflows/`.
> Hardened after the Opus review of #810 (22 Oct). The owner's GitHub settings that the hardening depends on are in
> §13.

**Triggers:** `push` to `main` → channel `main`; `push` to `develop` → channel `develop`; `workflow_dispatch` with
input `channel: test-*` (e2e, ref `develop` only); `schedule` → **refresh** (re-sign the channel pointer with new
`issued_at`/`expires_at`, nothing rebuilt; develop daily, main weekly). `concurrency: release-${channel}` with
`cancel-in-progress: false`. There is no `pull_request` or `pull_request_target` trigger. Defaults are
`permissions: {}` with minimal grants per job, actions are pinned by SHA, and checkouts use `persist-credentials: false`.

**One environment and one key per channel:**

| Environment | Deployment branches | Required reviewer | Secrets |
|---|---|---|---|
| `release-main` | `main` only | the owner (one click per main release and per main refresh) | `ORCH_RELEASE_KEY_MAIN` (cert `channels: ["main"]`), `CHANNELS_DEPLOY_KEY_MAIN` |
| `release-develop` | `develop` only | none | `ORCH_RELEASE_KEY_DEVELOP` (cert `["develop"]`), `CHANNELS_DEPLOY_KEY_DEVELOP` |
| `release-test` | `develop` only | none | `ORCH_RELEASE_KEY_TEST` (cert `["test-*"]`), `CHANNELS_DEPLOY_KEY_TEST` |

Write access to develop can therefore never sign `main`: the develop key's cert does not list it, and the main key
only runs from `main` after an owner click.

**No caches anywhere near a key.** The build, publish and refresh jobs set `setup-uv` `enable-cache: false`, drop
`cache: npm`, and use no `actions/cache`. uv is pinned with `version: <x.y.z>`. `hatchling` is pinned (`==`) in both
build backends (orch-core gets `hatchling==1.27.0` like the dashboard).

Job `build` (ubuntu-latest, no secrets, `contents: read`):
1. Check out the pushed commit. Set up Node 24 and uv. Run `npm ci && npm run build` in `addons/dashboard`.
2. `stamp.py 2.0.0.dev$RUN_NUMBER $CHANNEL $SHA`.
3. `uv build --wheel plugins/orch-core` and `uv build --wheel addons/dashboard/server` with
   `ORCH_DASHBOARD_REQUIRE_PREBUILT=1` (new: the hook fails instead of building).
4. `scripts/release/check_wheels.py dist/*.whl`. It checks that both versions equal V; the dashboard `Requires-Dist`
   has `orch-core==V`; the dashboard wheel holds `orch_dashboard/static/index.html` and `orch_dashboard/orch-addon.json`;
   the core wheel holds `orch/_build.json` and `orch/addons/manifests/*.json`; there are no `.pyc`, `tests/` or
   `node_modules` entries; the sizes are under the caps (core 8 MB, dashboard 20 MB).
5. Smoke test: `uv tool install` both wheels from local paths into a temp `UV_TOOL_DIR`, using a temp HOME, then
   `orch --version --json` must name V.
6. `uv export --frozen --no-dev` for both packages → `constraints.txt`. This pins every dependency version (§5, step 8).
7. Changelog from the commit subjects, with C0, C1 and bidi controls stripped and capped at 4 KB. Clients sanitise
   again on display.
8. Upload `dist/` and the changelog as a workflow artifact.

Job `publish` (needs `build`; `environment: release-<ch>`):
1. **Prepare the tools, with no key:** check out the same SHA, then
   `uv sync --frozen --no-dev --no-install-project --project scripts/release`. `scripts/release/` is its own tiny
   project whose `uv.lock` pins only `cryptography`. Download the build artifact. Nothing from the project under
   release is installed or built in this job.
2. **Sign** (the only step with `ORCH_RELEASE_KEY_<CH>` in its `env`):
   `scripts/release/.venv/bin/python -I scripts/release/sign.py`. It writes the manifest (§4.2) and `release.json`.
   The key is never written to disk.
3. **Rollback check** (no key): `pointer_check.py`. It lists all `v*` tags. For each tag it fetches `release.json` and
   keeps the ones that **verify at now**: signature, cert not revoked and within `not_after`, and the **signed**
   `channel` equal to this channel. The manifest's own `expires_at` is ignored for this, because old versions are
   expected to have expired. The highest seq among them is the **channel max**. A `v*` tag with no release behind it
   is `Refused`. Release titles and bodies are never read, because write users can edit them. The new seq must be
   greater than the channel max.
4. `gh release create v$V --prerelease --target $SHA` with assets: both wheels, `constraints.txt`, `release.json`.
   **Immutable releases** are on (§13), so neither the assets nor the tag can change afterwards.
5. **Pointer** (only `CHANNELS_DEPLOY_KEY_<CH>` in env): commit `<ch>.json` to the pointer repo (below).

Job `refresh` (schedule; `environment: release-<ch>`): the same tool preparation, then the rollback check. The pointer's
seq must **equal** the channel max, otherwise the job is `Refused` and opens or updates the issue "channel pointer
mismatch". It never re-signs an older envelope. After that it signs a fresh pointer for the channel max and commits
it. Job `cert-check` opens or updates the expiry issue only on `schedule` or `push`, never on `workflow_dispatch`
from a branch.

**Pointer home: the public repo `severinlindenmann/orch-channels`.** It holds `main.json`, `develop.json` and
`test-*.json`. Clients fetch `https://raw.githubusercontent.com/severinlindenmann/orch-channels/main/<ch>.json`.
Pushes come only from the deploy keys held in the three environments. Its `main` branch has a ruleset: no force-push,
no deletion, and no other pushers. The git history doubles as an audit log of every pointer change.

Other options considered:
- `gh release upload --clobber` on a `channel-*` release: breaks once immutable releases are on.
- An immutable `channel-<ch>-<seq>` release per publish: clients would have to list releases through the rate-limited
  API.
- GitHub Pages in orch-core: any workflow with `pages: write` can deploy.

A deploy key writes the whole repo, so the develop key could replace `main.json`. Trust never rests on the location,
though. The client trusts only signature, cert channel, seq and expiry, so the worst a pointer writer can do is
roll back or freeze within the limits below.

**Client-side rollback probe** (install.sh and `orch update`, defence in depth): after verifying the pointer, list
the `v*` tags newer than the pointer's seq through the tags API (one unauthenticated call). Fetch at most 5 of their
`release.json` files, newest first. If one verifies for this channel at now, refuse with `update.stale`
("a newer signed release exists than the channel pointer names"). Tags are created only by Actions and cannot be
moved or deleted (§13). If the API cannot be reached, print a warning, not an error.

**First-install exposure.** A fresh install trusts the newest pointer it can see. Someone who can write the pointer
(a deploy key, or the owner account) can serve an older envelope that is still valid until its `expires_at`. The
probe above catches that whenever the API answers. The pointer expiry is therefore **shortened per channel**:
`develop` 7 days with a daily refresh, `main` 14 days with a weekly refresh. Every main release also resets the main
expiry, so the owner's refresh click is needed only in a week without a main release. Version `release.json` files
keep a 21-day expiry; it matters only for `--version` installs, which are explicit and shown before they run.

## 4. Signing and key custody

### 4.1 Choice: Ed25519, a root key kept off CI plus an online release key

- **Root key** (Ed25519): generated in memory by `scripts/release/rootkey.py init` and stored **only** in Proton Pass,
  vault "Agents", in its own item, which nothing but the certify script uses. It never exists as a file, on GitHub or
  on CI (ceremony: §4.5). Its public key is embedded in `orch/update/keys.py` and in `install.sh`, and its fingerprint
  is recorded in `release/keys/root-1.pub.json`.
- **Release keys** (Ed25519, **one per channel**): `ORCH_RELEASE_KEY_MAIN` in `release-main`,
  `ORCH_RELEASE_KEY_DEVELOP` in `release-develop`, and a test key in `release-test` (§3). Each **certificate** is
  signed by the root key and binds a channel list:
  `{"format":"orch.release-key/1","key_id","public_key","serial","purpose":"core","channels":["main"],"not_after"}`.
  The develop cert lists `["develop"]` and the test cert `["test-*"]`. A cert is valid for 180 days, and every envelope
  carries it. The add-on catalog keys (addon-platform §3) follow the same rule: one per channel, `purpose:
  "addon-catalog"`.
- **Why not Sigstore / GitHub attestations:** offline checking needs `sigstore-python` with a TUF trust root and Rekor
  data. That is a heavy dependency in core, and install.sh has no Python before uv. More important: its identity is
  "this workflow in this repo", so anyone who controls the GitHub account or CI produces valid attestations. The
  transparency log only helps detect misuse afterwards. Ed25519 is already in core (`cryptography`), it can be checked
  in about 60 lines of pure Python inside install.sh, and the separate root gives what Sigstore cannot: a CI compromise
  is limited by the certificate lifetime and can be revoked.
- **Why not a single CI key:** with one key, rotation and revocation would need the key that was compromised.

### 4.2 Envelope and manifest

```json
{"format":"orch.release-envelope/1",
 "payload":"<base64 of the manifest bytes>",
 "sig":{"alg":"ed25519","key_id":"rk-3","value":"<base64>"},
 "key_cert":{"cert":{…release-key/1…},"root_key_id":"root-1","sig":"<base64>"},
 "revocations":{"doc":{"format":"orch.revocations/1","min_serial":3,"revoked":[2],"issued_at":"…"},"sig":"<base64>"}}
```

The signature covers `b"orch-release/1\n" + payload` (domain separated). The certificate signature covers
`b"orch-release-key/1\n" + JCS(cert)`, and the revocation list signature covers `b"orch-revocations/1\n" + JCS(doc)`.
The manifest bytes are signed as they are, so the verifier never canonicalises anything.

Manifest (`orch.release/1`): `channel`, `version`, `seq` (= N), `commit` (40 hex), `issued_at`, `expires_at`
(version `release.json`: issued + 21 days; the channel pointer: 7 days on develop, 14 on main, kept fresh by the refresh, §3), `files: [{role: core|dashboard|constraints, name, sha256, size,
url}]`, `changelog` (plain text, at most 4 KB, from the merge titles since the previous release), `min_from` (the oldest
version that may update straight to this one; older versions are told to rerun install.sh).

### 4.3 Verification rules (core `orch/update/verify.py` and install.sh share the test vectors)

Every rule fails closed. Verification checks, in order: root key id known → cert signature → `purpose == "core"`,
channel listed (a `test-*` channel or the test cert is refused unless this build is a test build, §4.4), `not_after` > now, `serial >= min_serial` and not in `revoked` → envelope signature → manifest
`channel` equals the requested channel → `expires_at` > now → `seq` ≥ the last `seq` seen for the channel (stored in
the install record) → the newer-tag probe (§3; a warning when it cannot run) → each downloaded file's sha256 and size. The revocation list is only accepted with a higher
`issued_at` than the stored one (continuity).

### 4.4 Rotation, revocation, compromise

- **Release key rotation (routine, a lead task):** before `not_after`, the lead runs the certify step (§4.5) on a new
  key, and the result goes into the repo as `release/keys/rk-<n>.cert.json`. Then the environment secret is swapped.
  The overlap is one release.
- **Root rotation:** the new root public key, signed by the old root (`orch.root-next/1`), ships in a release. Installed
  orch adds it to the stored trust set (continuity). install.sh embeds the new root.
- **Release key compromise:** the owner signs a revocation list (`min_serial` = next serial), certifies a new key and
  publishes a release. Clients refuse the old key from then on. `orch update` warns when the installed version was
  signed by a revoked serial ("reinstall with install.sh"). An attacker who holds back the revocation is limited by
  `expires_at` (21 days) and `not_after` (180 days).
- **Root compromise:** there is no in-band recovery. Announce it and reinstall with a new install.sh. That is why the
  root key never touches CI and never exists as a file (§4.5).
- **Test key:** a separate root-certified key with `channels: ["test-*"]` signs the e2e channels. It runs only from
  `develop` (`release-test`). **Production verifiers refuse every `test-*` channel and the test cert outright.** Only
  a build whose `_build.json` has `test_build: true` accepts them; the stamp step sets that for test-channel builds
  only. A leaked test key can therefore never reach a production install.

### 4.5 Root ceremony: no key on disk (owner decision 2, 22 Oct)

- **Init (once):** `uv run --no-project python -I scripts/release/rootkey.py init`. The script generates the key with
  `cryptography` in memory. It starts `~/.local/bin/pass-cli item create note --vault-name Agents --title
  "orch release root key" --from-template -` itself and writes the template JSON (the note holds the base64 private
  key) to that process's **stdin**. The key is never put in argv, where `ps` could see it, nor on stdout or in a file.
  The script then prints only the public key and its fingerprint (sha256 of the raw public key). The public key goes
  to `release/keys/root-1.pub.json`, `orch/update/keys.py` and `install.sh`, and a test pins all three to the same
  value.
- **Certify (every 180 days, and at rotation or revocation):** `pass-cli run --env-file release/keys/root.env --
  uv run --no-project python -I scripts/release/rootkey.py certify rk-<n>.pub.json`. `root.env` is committed and holds
  only the reference `RELEASE_ROOT_KEY=pass://Agents/orch release root key/note`. The script reads the variable,
  removes it from its environment, signs, and prints the certificate. `pass-cli run` masks the secret if it ever
  reaches output.
- **CI keys (three):** `rootkey.py release-key --channel main|develop|test` generates one key in memory. It pipes the
  private key into `gh secret set ORCH_RELEASE_KEY_<CH> --env release-<ch>` on stdin, prints only the public key, and
  then certifies it with the channel list `["main"]`, `["develop"]` or `["test-*"]`. The certs are committed as
  `release/keys/rk-<ch>-<n>.cert.json`. A lost key is simply replaced. The pointer deploy keys are made the same way:
  `ssh-keygen` into a temp file the script creates and deletes by its exact path, the public half added as a deploy key
  of `orch-channels`, and the private half sent to `gh secret set CHANNELS_DEPLOY_KEY_<CH>` on stdin. (Implementation
  note: `release-key` is not in #808 yet; it is added by U3b.)
- **Reminder:** `release.yml` emits a warning and opens or updates the issue "release key expires on <date>" when
  `not_after` is less than 30 days away, and fails once it has passed. The lead's plan carries the date as a task.
- **Trade-off:** the root now lives in the same trust domain as the agent machine (Proton Pass "Agents"), not with the
  owner. Anyone who controls this machine's Proton Pass session can certify a release key. Mitigations: a separate
  item read only by `rootkey.py certify`; certificates last 180 days and revocation goes through the root; every
  certify run is a commit (`release/keys/*.cert.json`), so an unexpected certificate is visible in git; the CI key
  stays short-lived and is usable only from its own channel's environment and branch. Moving the root to an
  owner-held key later is a routine root rotation (§4.4).

## 5. install.sh

POSIX sh, `set -eu`. The whole script sits in `main "$@"` on its last line, so a download cut short runs nothing.
Flags: `--channel main|develop` (default main), `--version V`, `--no-start`, `--yes`. Steps:

1. **Platform:** macOS (arm64, x86_64) and Linux (x86_64, aarch64, glibc) are supported. Anything else is refused with
   "not supported", including musl/Alpine (`ldd --version` lacks glibc). On macOS, if
   `sysctl -n sysctl.proc_translated` = 1 (Rosetta), stop with "this terminal runs under Rosetta: open a native
   terminal and rerun" (uv would install x86_64 Python and break the arm64 keychain helpers). On Linux there is no
   Rosetta case.
   - **Linux service:** run `systemctl --user show-environment`. If no user bus answers (a plain SSH login, a
     container), warn: "the orch host needs a systemd user service: run `loginctl enable-linger $USER` (or log in
     once on the console)". Then continue with `--no-start` behaviour.
   - **Linux sandbox (bubblewrap):** look for `bwrap` at the paths of `orch/land/sandbox.py` (`BWRAP_CANDIDATES`)
     and probe it with `bwrap --unshare-all --ro-bind / / true`. If it is missing, warn: "Land's build sandbox needs
     bubblewrap: `sudo apt install bubblewrap` (or your distro's package); orch works without it, but sandboxed
     builds are refused". If the probe fails (for example Ubuntu 24.04's AppArmor
     `kernel.apparmor_restrict_unprivileged_userns=1`), say that and point to the distro's bwrap AppArmor profile.
     The installer never runs sudo.
2. **Tools:** check for `curl`. If it is missing, say so (macOS always ships it; this matters for `wget … | sh`). Also
   require `shasum`, `mktemp` and `uname`.
3. **uv:** use `uv` from PATH or `~/.local/bin/uv` if it is at least the pinned minimum. Otherwise download the pinned
   official installer `https://astral.sh/uv/<UV_VERSION>/install.sh`, check its sha256 (pinned in install.sh), and run
   it with `UV_NO_MODIFY_PATH=1`.
4. **Python:** `uv python install 3.14` (core needs ≥ 3.14).
5. **Fetch:** into `T=$(mktemp -d)` (removed at exit by its exact path), fetch the channel pointer `<ch>.json` from `orch-channels` (§3), or `release.json` of
   `v$V` when `--version` is given. Verify it with the embedded verifier, run as
   `uv run --no-project --isolated --python 3.14 python -I "$T/verify.py"`. The verifier and the root key are embedded
   in install.sh as a heredoc and use only the stdlib. A failure prints the reason and stops.
6. **Download** the files the manifest lists into `$T`. The verifier checks each sha256 and size.
7. **Downgrade check:** if `orch` is installed, read `orch --version --json`. A newer installed version is refused
   unless `--version` was given ("installed 2.0.0.dev50 is newer than channel main's 2.0.0.dev48; pass --version to
   go back"). The same version is a no-op install that continues with step 9 (rerunning is safe).
8. **Install from the verified local files** (no URL after verification, so nothing can be swapped between check and
   install):
   `uv tool install --force --python 3.14 --constraints "$T/constraints.txt" "$T/orch_core-V…whl" --with "$T/orch_dashboard-V…whl"`.
9. **Install record:** `orch update --adopt "$T/<envelope>"`. The new orch verifies the envelope again with its own
   keys and writes the record (§6).
10. **PATH:** if `command -v orch` fails, run `uv tool update-shell` (it edits the rc file of zsh, bash or fish on
    both platforms) and print "open a new terminal, then run: orch".
11. **Start** (skipped by `--no-start`): `orch host enable` (launchd on macOS; the systemd user unit on Linux, through
    the same `orch/host/enable.py` / `units.py` path), then `orch dashboard open`.
    - **Headless** (Linux or macOS with `SSH_CONNECTION` set, or Linux with no `DISPLAY`/`WAYLAND_DISPLAY`): run
      `orch dashboard open --print-url` instead. It prints the loopback URL with its one-time code and a copyable
      tunnel line, `ssh -L <port>:127.0.0.1:<port> <user>@<host>`, to run on the person's own computer. Nothing
      listens beyond loopback. Any wider access belongs to the separate remote-setup design (ssh -L first, Tier S
      design check).

| Failure | Behaviour |
|---|---|
| No network or GitHub down | Stop before installing anything, exit 1, "nothing was changed". |
| Signature, expiry or hash wrong | Stop, exit 1, "the download could not be verified; nothing was changed". Never retry with another URL. |
| uv installer hash mismatch | Stop: the pinned uv version is gone or was altered. |
| Partial install (`uv tool install` fails) | uv keeps the old tool environment or none. Rerunning install.sh is the fix and says so. |
| `orch host enable` fails | Orch is installed. Print the enable error and "run `orch host enable` again after fixing it". |
| Run under an agent (`ORCH_GRANT`/`ORCH_SESSION` set) | Refuse in step 0, like the CLI (§8). |
| Linux without a systemd user bus | Install, skip start, print the `loginctl enable-linger` hint, exit 0. |
| Linux without a working bwrap | Install and start. Warn about sandboxed builds (step 1). |

## 6. Install record

`$XDG_DATA_HOME/orch/install.json` (default `~/.local/share/orch/install.json`, mode 0600, directory 0700). It holds
`{format, channel, version, seq_seen: {channel: seq}, revocations_issued_at, min_serial, trusted_roots, installed_at}`.
The repo URL is a **code constant** (`orch/update/source.py`), never config. The channel is one of `main`, `develop`,
or a `test-*` channel set by install.sh. The record never comes from workspace files, `ORCH_*` variables or
agent-writable config. It changes only through `orch update --adopt` (install.sh) and `orch update --channel X`
(human-only, asks for confirmation). Same-UID processes can still write the file (tier A, same as every other state
file). The damage is bounded: any channel still needs a valid signature, `seq` never goes back, and an update to
another channel is shown before it runs.

## 7. `orch update`

```
orch update [--check] [--yes] [--json]                 core + dashboard, then add-ons (addon-platform.md)
orch update core [--check] [--version V] [--yes]       core + dashboard only
orch update --channel main|develop                     switch channel (asks, then updates)
```

Flow: read the record → fetch the channel pointer (a 10 s timeout) → verify (§4.3) → compare. With `--check`, print the
result and stop. Otherwise show the plan and ask (`--yes` skips the question but never the restart question when
agents run):

```
orch 2.0.0.dev41 → 2.0.0.dev47 (channel develop, signed 22 Oct 07:40)
  Dashboard 2.0.0.dev41 → 2.0.0.dev47
  Changes: Wiki add-on: page links; fix terminal resize; …
  After the update the host restarts (a few seconds). 2 agents and 1 terminal are running in the agent runner.
Update now? [y/N]
```

Apply: download to `$XDG_CACHE_HOME/orch/downloads/V/` → verify again → `uv tool install` from those paths (§5,
step 8). Then **exec the newly installed** `orch host enable --json`. The running process is old code, and the new code
does the restart with the existing `enable()` logic: release copy, host drain-restart, runner restart only with no
live runs. If `enable` reports `runner restart_needed`, ask: "The agent runner still runs the old version and has 3
running agents/terminals. Restarting it stops them. Restart now? [y/N]". Yes runs `orch host enable --restart-runner`.
No (or no TTY) leaves the runner alone: new starts get `runner.stale` until it restarts, and the message says so. A
dashboard server started by the person (its pidfile) is stopped and opened again. Agents and terminals are never killed
without a yes. If the new install fails its own `orch --version --json` check, reinstall the previous version from the
download cache (the last two are kept) and report the failure.

Exit codes and new catalog codes (`orch/ops/errors.py`). Each gets CLI goldens in `tests/cli/golden/errors/`, a line in
`_exit-codes.txt`, and declarations in `tests/cli/test_registry.py` for the `update` front command:

| Code | Exit | Meaning / wording |
|---|---|---|
| (ok) | 0 | updated, already current, or `--check` done (`update_available` in JSON) |
| `update.unreachable` | 9 retryable | "could not reach the release server; nothing was changed" |
| `update.unverified` | 5 | bad signature, unknown or revoked key, hash or size mismatch: "the update could not be verified; nothing was changed" |
| `update.stale` | 5 | `expires_at` passed, or `seq` lower than already seen: "the release information is out of date (possible replay)" |
| `update.downgrade` | 3 | the target is older than the installed version and `--version` was not given |
| `update.no_record` | 2 | orch was not installed by install.sh (git or development install): "install with the one-line installer first" |
| `update.too_old` | 5 | installed < `min_from`: "rerun the installer" |
| `human_only` | 3 | existing code, see §8 |

## 8. Guard rule

`orch update`, `orch update core`, `--channel` and `--adopt` refuse with `human_only` (exit 3) when `ORCH_GRANT` or
`ORCH_SESSION` is set and non-empty. This is the `orch dashboard` rule (`cli/dashboard.py agent_refusal`) moved into a
shared helper. `orch update --check` is allowed: it only reads, and it changes nothing except the cache. install.sh
refuses under the same variables. As defence in depth, the plugin guard (tier C) denies Bash commands that run
`orch update` without `--check`, `uv tool install|upgrade` with an orch package, `install.sh`, and writes to
`install.json`. Same-UID processes are trusted in tier A (§7 of orch-v2-host): these rules are speed bumps there and
boundaries only in tier B.

## 9. Threat model

| Threat | What stops it | What remains |
|---|---|---|
| Compromised GitHub account (owner's) | Releases need the channel's release key, which is only usable by its `release-<ch>` environment and branch (main also needs the owner's click). The account could push a workflow that uses it. | A signed malicious release within cert validity. Response: revocation (§4.4). install.sh itself comes from the account: first install trusts GitHub (TOFU). |
| Compromised CI / Actions secret | The root is not on CI (it is in Proton Pass), so the cert expires in at most 180 days and is revocable. | Releases signed until revocation. |
| Compromised agent machine / its Proton Pass session | It can certify a release key (trade-off in §4.5). Every certificate is a visible commit, and revocation goes through the root. | The root must be rotated, through a new install.sh, if the vault item leaks. |
| MITM / CDN tampering | TLS plus signature plus sha256 of each file. | None beyond TLS for the very first install.sh fetch. |
| Rollback / freeze (an older signed envelope put in the pointer) | The refresh and publish rule pointer seq = channel max (from signed content only); pointer expiry 7 d (develop) or 14 d (main); the client's newer-tag probe; `seq` ≥ last seen; the downgrade rule. | A fresh install while the tags API is unreachable: up to 7 or 14 days old. |
| Write access to develop used against main | Per-channel keys and certs; `release-main` only from `main` with an owner click; CODEOWNERS on release paths (§13). | None without the owner's click. |
| Poisoned cache or unpinned build tool while a key is loaded | No caches in the build, publish and refresh jobs; uv and hatchling pinned; the tools are synced before the key step; the key only in the sign step's env. | |
| TOCTOU between verify and install | Install only from the verified local path in a private temp or cache directory. A URL is never handed to uv. | None. |
| Malicious dependency on PyPI | `constraints.txt` (signed in the manifest) pins exact versions. | Hash pinning: follow-up (uv tool install has no `--require-hashes`). |
| Malicious agent on the machine | `human_only` refusal, the guard deny rules, prompts need a TTY. | Same UID in tier A: it could run uv itself. That is no worse than today, and it gets no signed release key. |
| Agent changes the channel | The channel lives only in the install record (not config, env or workspace); `--channel` is human-only; another channel still needs a valid signature and is shown before install. | Same UID can edit the record. The result is still a signed release, shown before it runs. |
| Downgrade to a vulnerable version | Refused without an explicit `--version`. Explicit downgrades are shown and need a yes. | A human can still choose it. |

## 10. Test plan

- **Units (T0 < 30 s):** envelope parse and verify against `tests/vectors/release/*.json`. Vectors are written first,
  by the security reviewer: good, bad sig, wrong domain, expired, lower seq, revoked serial, wrong channel, wrong
  purpose, unknown root, root-next chain. Also: version compare and downgrade rules, install record read and write
  (fail closed on a corrupt file), the update planner with a fake fetcher, the restart-question logic with a fake
  `enable()` result, and the guard refusal.
- **install.sh:** `shellcheck -s sh install.sh`. Run `tests/install/test_install_sh.py` (pytest) against a local HTTP
  server with fixture releases and a fake `uv`, under `HOME=$(mktemp -d /tmp/hkXXXX)`. It covers: success, tampered
  wheel, expired manifest, Rosetta (`sysctl` stub), no curl, rerun, downgrade refusal and `--version`, an agent
  environment, and a cut-off script (truncated file runs nothing). The embedded verifier passes the same vectors as
  core.
- **Release scripts:** `check_wheels.py` against wheels with a missing static dir or a version mismatch;
  `manifest.py` and `sign.py` round trip.
- **CI e2e (`e2e-install.yml`, owner pushes):** a matrix of clean runners, `macos-latest` (arm64), `ubuntu-24.04`
  (x86_64) and `ubuntu-24.04-arm` (aarch64; bubblewrap installed, user lingering on), run by `workflow_dispatch` after a
  test-channel release. On Ubuntu the dashboard step uses `--print-url` and is checked with curl against loopback. It runs the curl line against `test-ci` → `orch --version` → `orch host enable` → the dashboard answers. Then
  it publishes release N+1 to `test-ci` and runs `orch update --yes` → the new version, host restarted, the runner
  restart question answered from a fixture.
- **Local:** the same two steps on this machine with `HOME=$(mktemp -d /tmp/hkXXXX)`, `UV_TOOL_DIR`/`UV_CACHE_DIR`
  under that HOME, and a `test-local` channel. This never touches the real `~/.config/orch` or the keychain.

## 11. Tasks (each about 30 min, disjoint files)

| # | Task | Files | T0 |
|---|---|---|---|
| U0 | Test vectors and root/test key fixtures (security reviewer, Opus) | `tests/vectors/release/` | `uv run pytest tests/update/test_vectors_shape.py -x -q` |
| U1 | Version stamp, `_build.json`, `orch --version [--json]` | `scripts/release/stamp.py`, `orch/_build.py`, `cli/main.py` (version line only) | `uv run pytest tests/cli/test_version.py -x -q` |
| U2 | Envelope and verifier (Tier S) | `orch/update/{envelope,verify,keys}.py` | `uv run pytest tests/update/test_verify.py -x -q` |
| U3 | Release tooling: manifest, sign, certify, check_wheels | `scripts/release/{manifest,sign,rootkey,check_wheels}.py` | `uv run pytest tests/release -x -q` |
| U4 | Dashboard hook: `ORCH_DASHBOARD_REQUIRE_PREBUILT`, train version pin | `addons/dashboard/server/hatch_build.py`, `pyproject.toml` | `cd addons/dashboard/server && uv run pytest tests/test_build_hook.py -x -q` |
| U5 | `release.yml` written plus `actionlint` (the owner pushes it) (#810) | `.github/workflows/release.yml` | `actionlint .github/workflows/release.yml` |
| U5a | Rollback rule: `pointer_check.py` (channel max from signed content, at now, ignoring the manifest expiry; a tag without a release → `Refused`); publish requires new seq > max; refresh requires pointer seq == max, else an issue; the review's adversarial tests (`~/orch-dev/state/rev-release-tests/`) move into `tests/release` | `scripts/release/{pointer_check,refresh}.py`, `tests/release/` | `HOME=$(mktemp -d /tmp/hkXXXX) uv run pytest tests/release -k "refresh or pointer" -x -q` |
| U5b | Workflow hardening: `release-main`/`release-develop`/`release-test` environments and per-channel secrets, tools synced before the key step, key only in the sign step's env, uv pinned, every cache off, `cert-check` issue writes only on schedule/push, changelog control-char strip, `__pycache__` removed and ignored (the owner pushes it) | `.github/workflows/release.yml`, `.gitignore`, `scripts/release/changelog.py` | `actionlint .github/workflows/release.yml && uv run pytest tests/release/test_changelog.py -x -q` |
| U5c | Tool project `scripts/release/pyproject.toml` + `uv.lock` (only `cryptography`); `hatchling==1.27.0` in orch-core | `scripts/release/pyproject.toml`, `scripts/release/uv.lock`, `plugins/orch-core/pyproject.toml` (build-system line only) | `uv sync --frozen --no-dev --no-install-project --project scripts/release && uv run pytest tests/release -x -q` |
| U5d | Pointer in `orch-channels`: deploy-key commit step, client fetch URL, newer-tag probe in `orch update` and install.sh (shared vectors) | `scripts/release/push_pointer.py`, `orch/update/{source,probe}.py` | `uv run pytest tests/update/test_probe.py tests/release/test_push_pointer.py -x -q` |
| U5e | Test channels: the `test_build` stamp flag; production verifiers refuse `test-*` and the test cert | `scripts/release/stamp.py`, `orch/update/verify.py` | `uv run pytest tests/update/test_verify.py -k test_channel -x -q` |
| U5f | CODEOWNERS (the owner pushes it, because it covers `.github/**`) | `.github/CODEOWNERS` | `gh api repos/severinlindenmann/orch-core/codeowners/errors` (empty) |
| U3b | Root ceremony in `rootkey.py` (`init` piped to `pass-cli` on stdin, `certify` from the env var, `release-key --channel main\|develop\|test` to `gh secret set` on stdin, certs with channel lists `["main"]`, `["develop"]`, `["test-*"]`, deploy keys for `orch-channels`), `release/keys/root.env`, the pinning test; run once by the lead after the owner's settings (§13) (Tier S) | `scripts/release/rootkey.py`, `release/keys/` | `uv run pytest tests/release/test_rootkey.py -x -q` |
| U6a | install.sh core plus macOS: args, uv, verifier, install, record, PATH, Rosetta | `install.sh`, `tests/install/test_install_sh.py` | `shellcheck -s sh install.sh && uv run pytest tests/install/test_install_sh.py -x -q` |
| U6b | install.sh on Linux: platform and glibc check, systemd user bus/linger hint, bwrap probe, headless `--print-url` plus the ssh -L line | `install.sh` (platform functions only), `tests/install/test_install_linux.py` | `shellcheck -s sh install.sh && uv run pytest tests/install/test_install_linux.py -x -q` |
| U7 | Install record, `--adopt`, `update --check`, the catalog codes and goldens | `orch/update/{record,check}.py`, `orch/ops/errors.py`, goldens | `uv run pytest tests/update/test_check.py tests/cli/test_golden.py -x -q` |
| U8 | `update` apply: download cache, uv install, exec enable, runner question, rollback | `orch/update/apply.py`, `orch/cli/update.py` | `uv run pytest tests/update/test_apply.py -x -q` |
| U9 | Guard: shared `agent_refusal`, plugin deny rules | `orch/cli/agentenv.py`, guard rules | `uv run pytest tests/update/test_guard.py tests/cli/test_dashboard_cmd.py -x -q` |
| U10a | Local e2e script (macOS, isolated HOME) | `e2e/install-update.sh` | `e2e/run.sh install-update` |
| U10b | `e2e-install.yml` matrix: macOS, Ubuntu x86_64, Ubuntu arm64 (the owner pushes it) | `.github/workflows/e2e-install.yml` | `actionlint .github/workflows/e2e-install.yml` |

Order: U0 → (U1, U3, U4 in parallel) → U2, U3b → U6a, U7 → U6b, U8, U9 → U5, U5a–U5f → U10a, U10b. U5a, U5c, U5d and
U5e touch disjoint files and can run in parallel; U5b follows U5c. Opus security review: U0, U2, U5a, U5b, U5d, U5e,
U3b, U6a, U6b, U7, U8, U9. U6a and U6b both edit install.sh, so they run in sequence, never in parallel.
U7 adds catalog codes: run all of `tests/cli` and `tests/host/test_hostclient.py` before pushing.

## 12. Owner questions

### Decisions (owner, 22 Oct)

1. **Linux from the start:** macOS and Linux (x86_64, aarch64), with the systemd user service, the bwrap check,
   headless `--print-url` with an ssh -L line, and a CI matrix (§5, §10; tasks U6b, U10b).
2. **Root key held by the lead/agents** in Proton Pass "Agents" via `pass-cli`, never on disk; the 180-day
   re-certification is a lead task with a reminder (§4.5; task U3b).
3. **Background update check off by default.** People see new versions with `orch update --check` or "Check now" in
   Settings → Versions. A human-only signed setting turns the 6-hourly check on (addon-platform §8).

No open questions.
4. **Release hardening (Opus review of #810, 22 Oct; lead's decisions):**
   - The pointer seq must equal the channel max, taken from signed content only.
   - One key and cert per channel; `release-main` needs an owner click; CODEOWNERS on the release paths.
   - The test key is refused by production builds.
   - No caches and pinned tools wherever a key is loaded; the key is only in the sign step's env.
   - The pointer moves to `orch-channels`.
   - Pointer expiry: 7 days on develop, 14 on main.
   - A client-side probe for newer tags (§3).

## 13. Owner GitHub settings (needed before U3b runs and before immutable releases are turned on)

The release chain's security claims depend on these. The lead checks each one with `gh api` and reports any that are
missing. Only the owner can change them.

1. **Environments in orch-core:**
   - `release-main`: deployment branch `main` only, no tag patterns, the owner as required reviewer.
   - `release-develop`: deployment branch `develop` only.
   - `release-test`: deployment branch `develop` only.
   - Secrets live only in these environments; there is no repository-level `ORCH_RELEASE_KEY*` or
     `CHANNELS_DEPLOY_KEY*`.
   - The default branch is `main`, and `schedule` runs there. The main refresh runs directly (`release-main`, with the
     owner's click). The develop refresh is started by that scheduled run as `workflow_dispatch` with
     `ref: develop` (`actions: write` only in that dispatch job), so it runs from develop under `release-develop`.
2. **Branch rulesets (`main`, `develop`):** block force-push and deletion; require a PR plus green checks; require
   code-owner review for `.github/**`, `scripts/release/**`, `release/keys/**`, `plugins/orch-core/src/orch/update/**`,
   `tests/vectors/release/**`, `install.sh`, `plugins/orch-core/pyproject.toml` and `plugins/orch-core/uv.lock`. The
   bot cannot bypass. Consequence: agents' develop PRs that touch these paths wait for the owner's review.
3. **Tag ruleset on `v*`:** restrict update and deletion; creation only by GitHub Actions; no bypass for the bot.
4. **Immutable releases:** turn them on **after** the pointer has moved to `orch-channels` (U5d).
5. **Bot and agent tokens:** no `workflow` scope. Check that only the owner can push `.github/workflows/**`.
6. **Actions settings:** default workflow permissions read-only; "Allow GitHub Actions to create and approve pull
   requests" off; allowed actions limited to `actions/*` and `astral-sh/setup-uv`; SHA pinning required.
7. **Never** set `ACTIONS_STEP_DEBUG` or `ACTIONS_RUNNER_DEBUG` as secrets or variables. Re-runs of key jobs stay
   behind the environment rules.
8. **New public repo `severinlindenmann/orch-channels`:** a ruleset on `main` (no force-push, no deletion, pushes only
   by the three deploy keys); deploy keys with write access, one per environment; no Actions and no other
   collaborators.

