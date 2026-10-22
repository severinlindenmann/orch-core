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

Triggers: `push` to `main` → channel `main`; `push` to `develop` → channel `develop`; `workflow_dispatch` with input
`channel: test-*` (e2e); `schedule` weekly → **refresh** (re-sign the current channel manifests with a new
`issued_at`/`expires_at`, nothing rebuilt). `concurrency: release-${channel}` with `cancel-in-progress: false`.

Job `build` (ubuntu-latest, no secrets):
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
7. Upload `dist/` as a workflow artifact.

Job `publish` (needs `build`; `environment: release`, which holds the secret `ORCH_RELEASE_KEY`; deployment branches
`main` and `develop` only):
1. Download the artifact. Compute sha256 and size for each file.
2. `scripts/release/manifest.py` writes the release manifest (§4.2). `scripts/release/sign.py` wraps it in a signed
   envelope. The key is read from the environment and never written to disk.
3. `gh release create v$V --prerelease --target $SHA` with assets: both wheels, `constraints.txt`, `release.json` (the
   envelope for this exact version). Repo setting: **immutable releases** on, so the assets of a version tag cannot
   change after publish.
4. Channel pointer: the release tagged `channel-$CHANNEL` (one per channel, created once, the only mutable release)
   gets `channel.json` replaced with `gh release upload --clobber`. `channel.json` has the same format as
   `release.json` and points at the newest version. The fetch URL is fixed:
   `https://github.com/severinlindenmann/orch-core/releases/download/channel-<ch>/channel.json`.
   One file holds payload and signature together, so a reader never sees a new manifest with an old signature.

## 4. Signing and key custody

### 4.1 Choice: Ed25519, a root key kept off CI plus an online release key

- **Root key** (Ed25519): generated in memory by `scripts/release/rootkey.py init` and stored **only** in Proton Pass,
  vault "Agents", in its own item, which nothing but the certify script uses. It never exists as a file, on GitHub or
  on CI (ceremony: §4.5). Its public key is embedded in `orch/update/keys.py` and in `install.sh`, and its fingerprint
  is recorded in `release/keys/root-1.pub.json`.
- **Release key** (Ed25519): the private key is the `ORCH_RELEASE_KEY` secret of the `release` environment. Its
  **certificate** is signed by the root key:
  `{"format":"orch.release-key/1","key_id","public_key","serial","purpose":"core","channels":["main","develop"],"not_after"}`.
  It is valid for 180 days, and every envelope carries it.
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
(issued + 21 days; the weekly refresh keeps it fresh), `files: [{role: core|dashboard|constraints, name, sha256, size,
url}]`, `changelog` (plain text, at most 4 KB, from the merge titles since the previous release), `min_from` (the oldest
version that may update straight to this one; older versions are told to rerun install.sh).

### 4.3 Verification rules (core `orch/update/verify.py` and install.sh share the test vectors)

Every rule fails closed. Verification checks, in order: root key id known → cert signature → `purpose == "core"`,
channel listed, `not_after` > now, `serial >= min_serial` and not in `revoked` → envelope signature → manifest
`channel` equals the requested channel → `expires_at` > now → `seq` ≥ the last `seq` seen for the channel (stored in
the install record) → each downloaded file's sha256 and size. The revocation list is only accepted with a higher
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
- **Test key:** a separate root-certified key with `channels: ["test-*"]` signs the e2e channels. Production installs
  accept `test-*` only when the install record already names that test channel.

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
- **New CI release key:** `rootkey.py release-key` generates it in memory and pipes the private key into
  `gh secret set ORCH_RELEASE_KEY --env release` on stdin. Only the public key is printed, and then certified. A lost
  release key is simply replaced.
- **Reminder:** `release.yml` emits a warning and opens or updates the issue "release key expires on <date>" when
  `not_after` is less than 30 days away, and fails once it has passed. The lead's plan carries the date as a task.
- **Trade-off:** the root now lives in the same trust domain as the agent machine (Proton Pass "Agents"), not with the
  owner. Anyone who controls this machine's Proton Pass session can certify a release key. Mitigations: a separate
  item read only by `rootkey.py certify`; certificates last 180 days and revocation goes through the root; every
  certify run is a commit (`release/keys/*.cert.json`), so an unexpected certificate is visible in git; the CI key
  stays short-lived and is usable only from the `release` environment on main/develop. Moving the root to an
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
5. **Fetch:** into `T=$(mktemp -d)` (removed at exit by its exact path), fetch `channel.json`, or `release.json` of
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

Flow: read the record → fetch `channel.json` (a 10 s timeout) → verify (§4.3) → compare. With `--check`, print the
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
| Compromised GitHub account (owner's) | Releases need the release key, which is only usable by the `release` environment on main/develop. The account could push a workflow that uses it. | A signed malicious release within cert validity. Response: revocation (§4.4). install.sh itself comes from the account: first install trusts GitHub (TOFU). |
| Compromised CI / Actions secret | The root is not on CI (it is in Proton Pass), so the cert expires in at most 180 days and is revocable. | Releases signed until revocation. |
| Compromised agent machine / its Proton Pass session | It can certify a release key (trade-off in §4.5). Every certificate is a visible commit, and revocation goes through the root. | The root must be rotated, through a new install.sh, if the vault item leaks. |
| MITM / CDN tampering | TLS plus signature plus sha256 of each file. | None beyond TLS for the very first install.sh fetch. |
| Rollback / freeze (stale `channel.json` served) | `expires_at` (21 d), `seq` ≥ last seen, the downgrade rule, revocation continuity. | A freeze of up to 21 days for a client that never saw a newer manifest. |
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
| U5 | `release.yml` written plus `actionlint` (the owner pushes it) | `.github/workflows/release.yml` | `actionlint .github/workflows/release.yml` |
| U3b | Root ceremony in `rootkey.py` (`init` piped to `pass-cli` on stdin, `certify` from the env var, `release-key` to `gh secret set` on stdin), `release/keys/root.env`, the pinning test; run once by the lead (Tier S) | `scripts/release/rootkey.py`, `release/keys/` | `uv run pytest tests/release/test_rootkey.py -x -q` |
| U6a | install.sh core plus macOS: args, uv, verifier, install, record, PATH, Rosetta | `install.sh`, `tests/install/test_install_sh.py` | `shellcheck -s sh install.sh && uv run pytest tests/install/test_install_sh.py -x -q` |
| U6b | install.sh on Linux: platform and glibc check, systemd user bus/linger hint, bwrap probe, headless `--print-url` plus the ssh -L line | `install.sh` (platform functions only), `tests/install/test_install_linux.py` | `shellcheck -s sh install.sh && uv run pytest tests/install/test_install_linux.py -x -q` |
| U7 | Install record, `--adopt`, `update --check`, the catalog codes and goldens | `orch/update/{record,check}.py`, `orch/ops/errors.py`, goldens | `uv run pytest tests/update/test_check.py tests/cli/test_golden.py -x -q` |
| U8 | `update` apply: download cache, uv install, exec enable, runner question, rollback | `orch/update/apply.py`, `orch/cli/update.py` | `uv run pytest tests/update/test_apply.py -x -q` |
| U9 | Guard: shared `agent_refusal`, plugin deny rules | `orch/cli/agentenv.py`, guard rules | `uv run pytest tests/update/test_guard.py tests/cli/test_dashboard_cmd.py -x -q` |
| U10a | Local e2e script (macOS, isolated HOME) | `e2e/install-update.sh` | `e2e/run.sh install-update` |
| U10b | `e2e-install.yml` matrix: macOS, Ubuntu x86_64, Ubuntu arm64 (the owner pushes it) | `.github/workflows/e2e-install.yml` | `actionlint .github/workflows/e2e-install.yml` |

Order: U0 → (U1, U3, U4 in parallel) → U2, U3b → U6a, U7 → U6b, U8, U9 → U5, U10a, U10b. Opus security review: U0, U2,
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
