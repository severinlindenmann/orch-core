# First use: orch core + the dashboard on your own Mac

A runbook for the owner: from nothing to your first ticket, approved by a click in the dashboard. Written 13 Oct 2026
against `develop` (456ef023). Every step says how it was checked:

- **verified**: run end to end in a throwaway setup (own venv, own `HOME`, `ORCH_STATE_DIR`, `XDG_CONFIG_HOME`; nothing of
  your real `~/.config/orch`, login keychain or launchd was touched), output gist given.
- **verified with the test custody seam**: run the same way, with the one documented seam that replaces the macOS login
  keychain by a 0600 file in the throwaway state (`server/tests/click_child.py`). The real command does something different
  on your Mac, described in the step.
- **read from the code**: not run, because it would touch your real launchd or keychain; file and line given.

## Known gaps (read first)

1. **No `orch dashboard` command, and no way to install the dashboard from git.** The launch path is the addon's own
   console script `orch-dashboard` (`server/pyproject.toml:9`, `[project.scripts]`), also `python -m orch_dashboard`.
   `server/README.md` says it plainly: "`orch dashboard` is the intended host CLI integration; M0 deliberately adds no
   command to `plugins/orch-core`". Smallest fix: a core `orch dashboard [open|stop]` that runs the installed
   `orch-dashboard` (one subparser in `plugins/orch-core/src/orch/cli`; core change, not made here).
2. **The built UI is not in git, so `pip install git+https://...#subdirectory=addons/dashboard/server` gives a server with
   no UI.** `server/src/orch_dashboard/static/` is gitignored (`addons/dashboard/.gitignore`); `npm run build` creates it
   and copies `dist/` there, and the wheel contains it only when the build ran before packaging. Verified: a non-editable
   `pip install ./addons/dashboard/server` after `npm run build` ships `orch_dashboard/static/{index.html,assets,favicon.svg}`
   and serves the real UI (`GET /` returns the app shell). Until fixed, **install the dashboard from your local checkout
   after building the UI** (step 1). Smallest fix: build the wheel in CI and publish it as a release asset (or a hatch
   build hook that runs `npm ci && npm run build`); then step 1 becomes one `pip install <wheel url>`.
3. **`orch-addon.json` is not in the wheel either.** The dashboard server refuses to serve unless the workspace has the
   addon installed and granted (`gateway.py:90-92`, `addon.disabled`). You copy the manifest from the checkout into the
   workspace once and sign `orch addon grant dashboard` (step 4b). Smallest fix: `orch addon install` or shipping the
   manifest inside the wheel (`orch-dashboard manifest`); not done here.
4. **No service for the dashboard.** The host is a service (`orch host enable`); the dashboard is one detached process
   per OS user that you start with `orch-dashboard open` and that ends at logout or reboot. Normal for now, but you will
   re-run `open` each day.
5. **`orch host register` (no `--peer`) fails after you move the key to convenience custody** (`no device key of yours for
   this workspace on this machine`: `default_peers` looks for the passphrase key file, `host/cli.py:358-360`). Verified:
   it works with an explicit `--peer 501=person:p_...`. Not a problem on the `init --host` path (the host is registered
   at init). Smallest fix in core: let `default_peers` accept the convenience key as proof of "your device key".
6. **There is no `orch host disable`.** Undoing the service is `launchctl bootout` plus deleting the plist (step 8).

## Before you start

- **Python 3.11 or newer** (both packages say `requires-python >=3.11`). macOS ships older; use Homebrew:
  `brew install python@3.11` (or `python@3.13`).
- **Node 24 and npm** only to build the UI (gap 2). Check with `node -v`.
- **The repo is private.** Clone it with `gh auth login` done once, or any git credential that can read
  `severinlindenmann/orch-core`. Everything below uses a local checkout; the git+https form for core alone is in step 1.
- Pick your own short paths. Unix socket paths are limited to about 100 bytes (`host/enable.py` refuses a longer run
  directory), so do not put the state in a deep folder. The default `~/.config/orch` is fine.

## 1. Install core and the dashboard, not editable, into a venv outside any checkout

**Status: verified** (local-checkout form, Python 3.11, macOS).

The venv must live **outside every git work tree**: `orch host enable` refuses an interpreter or package inside a
checkout ("the service would keep running whatever that tree holds next", `host/enable.py:181-217`). A folder in your home
directory is fine. The service will run this venv's Python, so do not move or delete the venv while the service is on.

```sh
python3.11 -m venv ~/orch-venv

# the checkout (once); main is orch v1, v2 lives on develop
git clone -b develop https://github.com/severinlindenmann/orch-core.git ~/src/orch-core   # needs gh auth / a token
cd ~/src/orch-core

# build the UI first, so that the dashboard wheel contains it (gap 2)
(cd addons/dashboard && npm ci && npm run build)      # ends with: built in ~1 s; writes server/src/orch_dashboard/static/

# install core first, then the dashboard (non-editable: no -e)
~/orch-venv/bin/pip install ./plugins/orch-core ./addons/dashboard/server
~/orch-venv/bin/pip list | grep -i orch               # orch-core 2.0.0.dev0, orch-dashboard 0.1.0
```

Result seen: `orch-core 2.0.0.dev0`, `orch-dashboard 0.1.0`, and `site-packages/orch_dashboard/static/{index.html,assets,favicon.svg}`.
Two console scripts appear in `~/orch-venv/bin`: `orch` and `orch-dashboard`. Put the venv's `bin` first in `PATH` for the
rest of this runbook (`export PATH=$HOME/orch-venv/bin:$PATH`) or use the full paths.

Core alone straight from git (**not verified**: the attempt did not finish, and it showed a trap). With a token in the URL
(`git+https://x-access-token:<token>@github.com/...`) macOS git hands the credential to `git-credential-osxkeychain`, which
tries to store your GitHub token in the login keychain; the attempt hung there and was killed. Prefer the clone above (it
uses your normal `gh auth` setup) and the `./plugins/orch-core` path form; if you do want the URL form, run it once from a
shell where `credential.helper` is empty (`git -c credential.helper= ...` does not reach pip's own git calls, so set
`GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0=`):

```sh
~/orch-venv/bin/pip install "orch-core @ git+https://github.com/severinlindenmann/orch-core.git@develop#subdirectory=plugins/orch-core"
```

If you rebuild the UI later, reinstall the dashboard: `pip install --force-reinstall --no-deps ./addons/dashboard/server`.

## 2. Identity and first run

**Status: verified** (`orch init`, in a pty).

There is no separate "create identity" command: `orch init` creates your person key, your device key and the workspace in
one go (step 4). You will be asked, in this order:

1. **Passphrase for this device.** orch prints a generated passphrase of six words. Either type it again to confirm
   (`Passphrase:`, `Repeat your passphrase:`), or type a passphrase of your own instead. It unlocks the device key; every
   terminal signature (`orch approve`, `orch grant`, `orch sign`, `orch addon grant`) asks for it. **Keep it** in your
   password manager.
2. **The 24-word recovery code**, shown once. It is the only way back to your *person* key if you lose this Mac or the
   passphrase; orch does not store it ("nowhere else"). Write it down and keep it somewhere safe and offline.
   `Press Enter when you have written the code down:`, then it asks **three of the words** by position
   (`Word 2:`, `Word 11:`, `Word 21:`) to check you wrote it down.
3. The screen clears and a review of the signed "workspace.created" event is printed; you are asked for the passphrase a
   last time (`Passphrase:`), then `ok init TEST seq=1`.

What to keep: **passphrase**, **recovery code**. State lives in `~/.config/orch/` (device key under
`hosts/<workspace-id>/person/`; the host's workspace key under `hosts/<workspace-id>/keys/`).

## 3. Start the host: `orch host enable` (service) or the foreground host

**Status: the service path is read from the code; the unit and the release install were produced and inspected with the
service manager switched off; the foreground host is verified.**

`orch host enable` (no `--dev`, `host/cli.py:226-313`, `host/enable.py`):

1. Installs an **immutable release** of core to `~/.local/share/orch/releases/<version>+<hash>/` and `.../current`.
2. Writes the macOS **LaunchAgent** `~/Library/LaunchAgents/io.orch.host.plist` (`RunAtLoad`, `KeepAlive`, `Umask 63`),
   which runs `<your venv>/bin/python3.11 -I ~/.local/share/orch/releases/current/bin/orch-host run --state
   ~/.config/orch --run ~/.config/orch/run`, logging to `~/.config/orch/host/host.log`.
3. `launchctl bootstrap gui/<uid>` it (or `kickstart` when already loaded), then waits up to 20 s for the control socket
   `~/.config/orch/run/control/host.sock`. Output on success: `{"ok": true, "result": {"data": {"mode": "host", "service": "launchd", ...}}}`.

macOS may show a "background item added" notification for `io.orch.host`. Refusals you can hit: the venv is in a git
checkout; a unit for another state, run dir or interpreter already exists (`--replace`, which stops that host).
Check with `orch host status`.

**Foreground alternative (no service, ends with the terminal; verified):**

```sh
orch host run --foreground                 # leave it running in its own terminal tab; Ctrl-C stops it
orch host status                           # in another tab
#   service: launchd ... installed=no ...  /  host: answers on .../run/control/host.sock  /  ok
```

Use the foreground host for the very first try. `orch host register` and `orch init --host` start the service for you if no
host answers (`ensure_service`), so if you want no service at all, start the foreground host first and pass `--no-service`
to `register`.

## 4. First workspace

**Status: verified** (foreground host, empty folder).

```sh
mkdir -p ~/orch/test && cd ~/orch/test      # an EMPTY folder
orch init --prefix TEST --name "Your Name" --host
```

`--host` creates the workspace through the running host: the host keeps the workspace key and serves the workspace at once
(registration included). Prompts: step 2. Output ends with `workspace TEST in <dir>`, `the workspace key stays with the orch
host`, and `next: orch grant ...`. Check: `orch host list` shows `TEST ... state ok`, `orch status` prints `ok status
person=Your Name`.

Alternative, a workspace that already exists (made with plain `orch init`, no `--host`): `orch host register <dir>`.
Verified: it answers `{"ok": true, ... "workspace_id": ...}` and `orch status` then says `host mode`. `register` takes your
UID as the person whose passphrase device key is on this Mac (gap 5 if the key was moved to convenience).

### 4b. Install and grant the dashboard addon in the workspace

**Status: verified.** (Without it, the dashboard refuses the workspace: `addon.disabled`.)

```sh
cd ~/orch/test
mkdir -p addons/dashboard
cp ~/src/orch-core/addons/dashboard/orch-addon.json addons/dashboard/   # not in the wheel, gap 3
orch addon grant dashboard                    # prompts: passphrase (twice: review, then sign)
orch addon list                               # dashboard 0.1.0 active
```

The grant is signed with your passphrase; it records the manifest hash and the single capability `serve_http`.
If the manifest changes (a dashboard update), grant again.

## 5. Launch the dashboard against the real host

**Status: verified** (non-editable install, real host, real workspace; browser launch checked with `BROWSER` stubbed).

```sh
orch-dashboard open                  # same as: python -m orch_dashboard open
```

- Starts the dashboard server if none runs (one per OS user: `python -m orch_dashboard serve --launcher-lock`, detached,
  `__main__.py:187-207`), checks an HMAC proof on the port it chose, then opens your default browser on a private 0600
  redirect file (`<state>/dashboard/launch-*.html`, deleted after use or 60 s) that carries the one-time code.
- **One-time code flow:** the server listens on `127.0.0.1` on a random port; the code (256 bits, 30 s, single use) is
  exchanged for a bearer token (12 h idle / 24 h absolute) held in the browser tab; restart ends every session.
- No browser available, or you want to see the link: `orch-dashboard open --print-url` in a terminal prints
  `http://127.0.0.1:<port>/#launch=<code>&to=%2F` (single use, 30 s). Observed: the page `GET /` returns the shell; the code
  exchange returns a session; `GET /api/workspaces` lists `TEST` with `host_state: ok` and your name as owner.
- Open a path directly: `orch-dashboard open /w/TEST/tickets`. Foreground, with logs on the terminal: `orch-dashboard serve`
  (then `orch-dashboard open` from a second terminal still opens a link).
- Stop it: `orch-dashboard stop` (or `--stop`), through the 0600 control socket.

## 6. Click-to-sign: `orch host convenience on`

**Status: verified with the test custody seam.** On your Mac the real commands use the login keychain; see below.

Without it, every sign action in the dashboard gives you a code and you sign in a terminal (step 7, fallback). To sign by a
click:

```sh
orch host convenience on  --workspace TEST     # only flips a flag in ~/.config/orch/host/ (no key is touched)
orch keys custody convenience --workspace TEST # asks the passphrase ONCE, moves the device key to convenience custody
orch-dashboard stop; orch-dashboard open       # restart so the signer picks it up
```

What was run in the throwaway setup (the seam: `python server/tests/click_child.py cli ...`, same commands, file store):
`click-to-sign is on for TEST`, the risk line, then `moves to: convenience / this unlocks the key once to move it; nothing is
signed / Passphrase:` and a JSON with `"custody": "convenience"`. In the dashboard: `GET /api/workspaces` shows
`click: {custody: convenience, enabled: true, available: true}`, and Settings, General has a "Signing" card.

**On your Mac (read from the code, `custody/convenience.py:10`, `custody/keychain.py`):** the key becomes a generic-password
item in your **login keychain**, service `io.orch.device-key`, account `<workspace-id>/<device-id>`, readable without a
passphrase by any process running as you (Keychain may ask you to allow access the first time). It may end up in Time Machine
backups and Mac migrations. Turn it off and forget it with `orch host convenience off --workspace TEST` and
`orch keys forget --workspace TEST` (removes local custody; not a revocation).

**The risk line**, shown on every click-sign card: "Convenience signing: agents running as you, or anything that takes over
this dashboard, could sign." You are trading the passphrase for a click, so any agent running under your user could approve
its own work.

**Conditions** (host refuses otherwise, `host/convenience.py` `reason()`): the flag is on; the workspace has **one person, no
other member**; you have **no other device** (no phone); the workspace is **not paired with a relay**; core knows the
`convenience` auth value. `on` also refuses while terminal hand-off requests are pending (they last 10 minutes). Also,
with the flag on, the dashboard prepares no terminal hand-off ("One signing path per workspace"): you either click or
type, not both; to go back, `orch host convenience off --workspace TEST` (verified: the next hand-off gives a code again).
Do not use convenience if you plan the phone or the relay soon.

## 7. First ticket and first signature

**Status: ticket creation by agent CLI: verified. Click-sign: verified with the test custody seam. Terminal `orch sign
CODE`: verified (real passphrase signer).**

A ticket is made by an agent or by you with a grant. Give your shell a grant for the session:

```sh
cd ~/orch/test
orch grant --hours 8 --label "first use"      # passphrase; prints ORCH_GRANT=gr_... ONCE
export ORCH_GRANT=gr_...                      # the printed value
orch new "First ticket" --type feature --priority high      # ok TEST-0001 ticket.created
orch ac add "It works" --ref TEST-0001
orch section set requirements --ref TEST-0001 -m "Do the first thing"
orch section set context --ref TEST-0001 -m "why"
orch section set out_of_scope --ref TEST-0001 -m "nothing else"
```

(`orch new` without a grant answers `grant.required`: creating tickets is an agent verb; you sign the *decisions*.) Reload
the dashboard: the ticket shows in Tickets and on Today ("needs you" once a gate is waiting).

**With click-to-sign on (step 6):** open the ticket, press "Approve requirements". A review card appears first with the risk
line, the lines the signer will sign, the fingerprint and the expiry; the **Sign** button turns on after about a second
(pressing it earlier is refused) and again after you switch windows. Press Sign: the gate shows approved. Observed at the
API: `prepare` answers `confirm: click`, `view` shows `auth: convenience`, `sign` answers `state: submitted`. The dashboard
can also create tickets this way (title, type): `new` was signed by click and became `TEST-0002`. Policy and member changes
make you type a word the card shows.

**Terminal fallback, convenience off (verified with the real passphrase signer):** pressing an action in the dashboard
shows a code like `HE6C-XKQH`. In a terminal:

```sh
unset ORCH_GRANT                  # an agent grant is refused for signing
orch sign HE6C-XKQH               # shows what you sign; type TEST-0001/<last 8 of the ticket id> + Enter; passphrase
#   signed approve on TEST-0001: seq 7        (the dashboard dialog turns to "Signed")
```

Settings changes (`settings.set`, `policy.set`) are signed with `orch sign` or by click; see HANDOVER "P2 on the real host".

## 8. Stop and undo everything

**Status: dashboard stop, unregister, convenience off, foreground host stop: verified. Service removal: read from the
code.**

| What | Command |
| --- | --- |
| Dashboard | `orch-dashboard stop` (ctl socket; the process exits, the signer child with it) |
| Click-to-sign off | `orch host convenience off --workspace TEST`; `orch keys forget --workspace TEST` removes the keychain item (macOS: item `io.orch.device-key`, account `<ws>/<device>`) |
| Foreground host | Ctrl-C in its terminal (or `kill <its PID>`; never `pkill`) |
| Host as a service | `launchctl bootout gui/$(id -u)/io.orch.host`, then delete `~/Library/LaunchAgents/io.orch.host.plist` (`host/enable.py:293`, `:243`) |
| Workspace off the host | `orch host unregister <workspace-id>` (id from `orch host list`); the folder and your signed history stay; the workspace is back in direct mode (`orch host status` says "direct mode (not registered)") |
| Addon | `orch addon disable dashboard` (data stays), `orch addon purge dashboard` (deletes its data) |
| Uninstall | `rm -rf ~/orch-venv` (after the service is gone), `chmod -R u+w ~/.local/share/orch/releases && rm -rf ~/.local/share/orch/releases` (releases are read-only) |
| Delete all state | `rm -rf ~/.config/orch` **destroys your device key; keep the recovery code**, workspace folders such as `~/orch/test` are separate |

Where state lives: `~/.config/orch/` (or `$ORCH_STATE_DIR`): `hosts/<ws>/` (signed log, workspace key, your device key under
`person/`), `host/` (registry `workspaces.json`, `convenience.json` flag, `host.log`), `run/` (sockets), `dashboard/` (ctl
socket, lock, short-lived launch files), `person/` (convenience custody; macOS keeps the key in the keychain instead). The
workspace itself (tickets, config) is the folder you ran `orch init` in.

## How this was checked

A throwaway venv, `HOME`, `XDG_CONFIG_HOME` and `ORCH_STATE_DIR` under one short disposable folder with a `.orch-dev` marker;
Python 3.11 non-editable installs of `plugins/orch-core` and `addons/dashboard/server` from the same checkout; one
foreground host and one dashboard server (a `click_child.py dashboard serve` for the seam steps); no launchd, no keychain.
The service step ran with `ORCH_NO_SERVICE_MANAGER=1`, which writes the unit and the release but refuses `launchctl`;
the generated plist is what step 3 describes.
