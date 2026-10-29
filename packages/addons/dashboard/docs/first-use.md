# First use: orch core + the dashboard on your own Mac

A runbook for the owner: from nothing to your first ticket, approved by a click in the dashboard. Updated 14 Oct 2026
against `develop` (0d84f707; first written 13 Oct against 456ef023). Every step says how it was checked:

- **verified**: run end to end in a throwaway setup (own venv, own `HOME`, `ORCH_STATE_DIR`, `XDG_CONFIG_HOME`; nothing of
  your real `~/.config/orch`, login keychain or launchd was touched), output gist given.
- **verified with the test custody seam**: run the same way, with the one documented seam that replaces the macOS login
  keychain by a 0600 file in the throwaway state (`server/tests/click_child.py`). The real command does something different
  on your Mac, described in the step.
- **read from the code**: not run, because it would touch your real launchd or keychain; file and line given.

## Known gaps (read first)

Fixed since the first version of this runbook (13 Oct), one line each:

- `orch dashboard` did not exist: fixed by #451, `orch dashboard [args]` launches the addon (exit 3 with an install hint when
  it is not installed).
- The built UI was not in the wheel: fixed by #452, a hatch build hook builds it during `pip install` (Node 24 + npm needed
  at install time; `ORCH_DASHBOARD_SKIP_UI=1` opts out and gives a server without UI, for development only).
- `orch-addon.json` was not in the wheel: fixed by #452, `orch-dashboard addon-manifest --write <workspace>` (step 4b).
- `orch host register` without `--peer` failed after click-to-sign custody: fixed by #451 (either custody is accepted).
- There was no `orch host disable`: fixed by #451 (step 8).
- `--print-url` outside a terminal stopped with a Python traceback: now a one-line refusal (this PR).

Still open:

1. **No service for the dashboard.** The host is a service (`orch host enable`); the dashboard is one detached process
   per OS user that you start with `orch dashboard open` and that ends at logout or reboot. Normal for now, but you will
   re-run `open` each day.
2. **Installing straight from git (`pip install git+https://...`) is not recommended.** With a token in the URL, macOS git
   hands the credential to `git-credential-osxkeychain`, which tries to store your GitHub token in the login keychain
   (the attempt hung there). Use a clone and a local path (step 1).

`orch dashboard open --print-url` prints the single-use URL only to a terminal, on purpose (a launch code must not end up
in a pipe or a log); elsewhere it refuses with one line and exit 1. `orch addon grant dashboard` asks you to type
`dashboard` + Enter at the review before the passphrase: that is the normal signing review, not a gap (step 4b).

On a headless machine (a VPS) the dashboard listens on the fixed loopback port 47821 (or the number in
`<state>/dashboard/port`), and `--print-url` also prints the `ssh -o ExitOnForwardFailure=yes -L ...` line to run on your own
computer; its link lives 5 min. **A page that did not come from a fresh `--print-url` link is not orch.** After the
dashboard stops, another user on the same machine can bind the fixed port, so an old tunnel or bookmark may reach their
page. Always start from a new link. HTTPS with a passkey login (remote mode, #1198 R1) closes this later.

## Before you start

- **Python 3.14 or newer** (both packages say `requires-python >=3.14`). macOS ships older; use Homebrew:
  `brew install python@3.14`.
- **Node 24 and npm** on `PATH` at install time: the dashboard's build hook runs `npm ci && npm run build` (step 1).
  Check with `node -v`. Not needed at run time.
- **The repo is private.** Clone it with `gh auth login` done once, or any git credential that can read
  `severinlindenmann/orch-core`. Everything below uses a local checkout (the git+https form is not recommended, see Known gaps).
- Pick your own short paths. Unix socket paths are limited to about 100 bytes (`host/enable.py` refuses a longer run
  directory), so do not put the state in a deep folder. The default `~/.config/orch` is fine.

## 1. Install core and the dashboard, not editable, into a venv outside any checkout

**Status: verified** (local-checkout form, Python 3.11, Node 24.21, macOS).

The venv must live **outside every git work tree**: `orch host enable` refuses an interpreter or package inside a
checkout ("the service would keep running whatever that tree holds next", `host/enable.py:181-217`). A folder in your home
directory is fine. The service will run this venv's Python, so do not move or delete the venv while the service is on.

```sh
python3.14 -m venv ~/orch-venv

# the checkout (once); main is orch v1, v2 lives on develop
git clone -b develop https://github.com/severinlindenmann/orch-core.git ~/src/orch-core   # needs gh auth / a token
cd ~/src/orch-core

# install core first, then the dashboard (non-editable: no -e). Node 24 + npm must be on PATH: the dashboard's
# hatch build hook (server/hatch_build.py) runs `npm ci && npm run build` and puts the UI into the wheel.
~/orch-venv/bin/pip install ./packages/core ./addons/dashboard/server
~/orch-venv/bin/pip list | grep -i orch               # orch-core 2.0.0.dev0, orch-dashboard 0.1.0
```

Result seen: `orch-core 2.0.0.dev0`, `orch-dashboard 0.1.0` (the whole install took about 15 s), and
`site-packages/orch_dashboard/{static/index.html,static/assets,static/favicon.svg,orch-addon.json}`, no separate `npm run
build`. Without Node 24 the install fails with "the dashboard UI could not be built: install Node >= 24 ...";
`ORCH_DASHBOARD_SKIP_UI=1 pip install ...` skips the UI (development only, the server then has no app). The hook reuses a
UI already built in `server/src/orch_dashboard/static/`, so after pulling UI changes reinstall with
`pip install --force-reinstall --no-deps ./addons/dashboard/server` (delete that `static/` folder first if an old build
sits there). Two console scripts appear in `~/orch-venv/bin`: `orch` and `orch-dashboard`. Put the venv's `bin` first in
`PATH` for the rest of this runbook (`export PATH=$HOME/orch-venv/bin:$PATH`) or use the full paths.

Do not install from `git+https://...` (see Known gaps: the keychain credential helper hung the attempt; that form was
never completed).

## 2. Identity and first run

**Status: verified** (`orch setup`; the page was played over the setup socket and the terminal in a pty).

There is no separate "create identity" command: `orch setup` (V3-D38; `orch init` is gone) creates your person key, your
device key and the workspace in one go (step 4), through a setup page in your browser and your Terminal. You will be shown
and asked, in this order:

1. **Passphrase for this device.** The page shows a generated passphrase of six words, once. It unlocks the device key;
   every terminal signature (`orch approve`, `orch grant`, `orch sign`, `orch addon grant`) asks for it. **Keep it** in
   your password manager.
2. **The 24-word recovery code**, shown once. It is the only way back to your *person* key if you lose this Mac or the
   passphrase; orch does not store it ("nowhere else"). Write it down and keep it somewhere safe and offline.
   The page then asks **three of the words** by position to check you wrote it down.
3. A review of what is created is shown; press Create. With **One click on this computer** that is all: setup runs in the
   browser and needs no terminal check. With **Terminal approval**, compare the three words your Terminal shows with the
   page's and press Enter (do not type `yes`). The page lists the steps as done and the Terminal prints
   `workspace TEST set up in <dir>`.

What to keep: **passphrase**, **recovery code**. State lives in `~/.config/orch/` (device key under
`hosts/<workspace-id>/person/`; the host's workspace key under `hosts/<workspace-id>/keys/`).

## 3. Start the host: `orch host enable` (service) or the foreground host

**Status: the service path is read from the code; the unit and the release install were produced and inspected with the
service manager switched off; the foreground host is verified.**

`orch host enable` (no `--dev`, `host/cli.py:226-313`, `host/enable.py`):

1. Installs an **immutable release** of core to `~/.local/share/orch/releases/<version>+<hash>/` and `.../current`.
2. Writes the macOS **LaunchAgent** `~/Library/LaunchAgents/io.orch.host.plist` (`RunAtLoad`, `KeepAlive`, `Umask 63`),
   which runs `<your venv>/bin/python3.14 -I ~/.local/share/orch/releases/current/bin/orch-host run --state
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

Use the foreground host for the very first try. `orch host register` and `orch setup` start the service for you if no
host answers (`ensure_service`), so if you want no service at all, start the foreground host first (setup uses a host
that already answers) and pass `--no-service` to `register`.

## 4. First workspace

**Status: verified** (foreground host, empty folder).

```sh
mkdir -p ~/orch/test && cd ~/orch/test      # an EMPTY folder
orch setup
```

`orch setup` creates the workspace through the running host: the host keeps the workspace key and serves the workspace at
once (registration included). Prompts: step 2 (name `Your Name`, prefix `TEST` on the page). Check: `orch host list` shows `TEST ... state ok`, `orch status` prints `ok status
person=Your Name`.

Alternative, a workspace that already exists (restored on this Mac): `orch host register <dir>`.
Verified: it answers `{"ok": true, ... "workspace_id": ...}` and `orch status` then says `host mode`. Without `--peer`, `register` maps your UID to the person whose device key for the workspace is on this Mac, in either
custody (verified in passphrase custody; verified with the test custody seam after the key moved to convenience, same
command). `--peer 501=person:p_...` is still required when core finds no usable key of yours: the key was removed,
revoked or expired, it lacks `decide`, or its person left (`docs/architecture/orch-v2-host.md`, "Registration turns host
mode on").

### 4b. Install and grant the dashboard addon in the workspace

**Status: verified** (in the same run; the grant went through a pty). (Without it, the dashboard refuses the workspace: `addon.disabled`.)

```sh
cd ~/orch/test
orch-dashboard addon-manifest --write .       # writes packages/addons/dashboard/orch-addon.json from the installed wheel
orch addon grant dashboard                    # review; type "dashboard" + Enter; then the passphrase
orch addon list                               # dashboard 0.1.0 active
```

`addon-manifest` without `--write` prints the manifest; `--write WORKSPACE` creates `packages/addons/dashboard/` and the file (it
refuses to replace a different file without `--force`, and never follows symlinks). Verified: a second identical run is
a no-op; `orch addon list` shows `not granted` before and `active` after the grant. The grant is signed with your passphrase; it records the manifest hash and the single capability `serve_http`.
If the manifest changes (a dashboard update), grant again.

## 5. Launch the dashboard against the real host

**Status: verified** (non-editable install, real host, real workspace; no browser was opened, the link was exchanged by
HTTP).

```sh
orch dashboard open                  # same as: orch-dashboard open, python -m orch_dashboard open
orch dashboard stop                  # everything after `orch dashboard` goes to the addon unchanged
```

- Starts the dashboard server if none runs (one per OS user: `python -m orch_dashboard serve --launcher-lock`, detached,
  `__main__.py:187-207`), checks an HMAC proof on the port it chose, then opens your default browser on a private 0600
  redirect file (`<state>/dashboard/launch-*.html`, deleted after use or 60 s) that carries the one-time code.
- **One-time code flow:** the server listens on `127.0.0.1` on a random port; the code (256 bits, 30 s, single use) is
  exchanged for a bearer token (12 h idle / 24 h absolute) held in the browser tab; restart ends every session.
- No browser available, or you want to see the link: `orch dashboard open --print-url` in a terminal prints
  `http://127.0.0.1:<port>/#launch=<code>&to=%2F` (single use, 30 s). Observed: the page `GET /` returns the shell; the code
  exchange returns a session; `GET /api/workspaces` lists `TEST` with `host_state: ok` and your name as owner.
- Open a path directly: `orch dashboard open /w/TEST/tickets`. Foreground, with logs on the terminal: `orch dashboard serve`
  (then `orch dashboard open` from a second terminal still opens a link). `orch dashboard --help` shows the addon's usage.
- Stop it: `orch dashboard stop` (or `--stop`), through the 0600 control socket.
- `orch dashboard` finds the addon beside the interpreter that runs `orch`, then as a module, then on `PATH`; not installed:
  exit 3 and a message that says how to install it. Checked: `GET /` on the printed port serves the app shell
  (`<!doctype html> ... <title>orch core`), and a code exchange returns a session whose `GET /api/workspaces` lists `TEST`
  with `host_state: ok`, you as owner, `click.custody: passphrase`.

## 6. Click-to-sign: `orch host convenience on`

**Status: verified with the test custody seam.** On your Mac the real commands use the login keychain; see below.

Without it, every sign action in the dashboard gives you a code and you sign in a terminal (step 7, fallback). To sign by a
click:

```sh
orch host convenience on  --workspace TEST     # only flips a flag in ~/.config/orch/host/ (no key is touched)
orch keys custody convenience --workspace TEST # asks the passphrase ONCE, moves the device key to convenience custody
orch dashboard stop; orch dashboard open       # restart so the signer picks it up
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

**Status: dashboard stop, foreground host stop: verified. `orch host disable` was run with the service manager switched off
(`ORCH_NO_SERVICE_MANAGER=1`): it refused `launchctl` (`host.unavailable ... service manager is off`) and left the unit
and releases alone; the removal itself is read from the code (`host/cli.py` `disable`, spec `orch-v2-host.md`).**

| What | Command |
| --- | --- |
| Dashboard | `orch dashboard stop` (ctl socket; the process exits, the signer child with it) |
| Click-to-sign off | `orch host convenience off --workspace TEST`; `orch keys forget --workspace TEST` removes the keychain item (macOS: item `io.orch.device-key`, account `<ws>/<device>`) |
| Foreground host | Ctrl-C in its terminal (or `kill <its PID>`; never `pkill`) |
| Host as a service | `orch host disable` (stops and unloads the service, removes the unit file, keeps the state dir; `--purge-releases` also deletes the release copies under `~/.local/share/orch/releases`; refuses a unit made for another state or run dir and a runner service that uses the releases, unless `--force`). Registered workspaces then answer `host.unavailable` until `orch host enable`. Fallback by hand: `launchctl bootout gui/$(id -u)/io.orch.host`, then delete `~/Library/LaunchAgents/io.orch.host.plist` |
| Workspace off the host | `orch host unregister <workspace-id>` (id from `orch host list`); the folder and your signed history stay; the workspace is back in direct mode (`orch host status` says "direct mode (not registered)") |
| Addon | `orch addon disable dashboard` (data stays), `orch addon purge dashboard` (deletes its data) |
| Uninstall | `rm -rf ~/orch-venv` (after `orch host disable --purge-releases`; by hand the releases are read-only: `chmod -R u+w ~/.local/share/orch/releases && rm -rf ~/.local/share/orch/releases`) |
| Delete all state | `rm -rf ~/.config/orch` **destroys your device key; keep the recovery code**, workspace folders such as `~/orch/test` are separate |

Where state lives: `~/.config/orch/` (or `$ORCH_STATE_DIR`): `hosts/<ws>/` (signed log, workspace key, your device key under
`person/`), `host/` (registry `workspaces.json`, `convenience.json` flag, `host.log`), `run/` (sockets), `dashboard/` (ctl
socket, lock, short-lived launch files), `person/` (convenience custody; macOS keeps the key in the keychain instead). The
workspace itself (tickets, config) is the folder you ran `orch setup` in.

## How this was checked

Re-checked 14 Oct 2026 against `develop` 0d84f707. A throwaway venv, `HOME`, `XDG_CONFIG_HOME` and `ORCH_STATE_DIR` under one
short disposable folder with a `.orch-dev` marker (deleted afterwards); Python 3.11 and Node 24.21 on `PATH` for the
install; non-editable installs of `packages/core` and `packages/addons/dashboard/server` from the same checkout (the wheel
holds `orch_dashboard/static/index.html` and `orch_dashboard/orch-addon.json`); one foreground host and one dashboard
server on 127.0.0.1; no browser, no launchd, no keychain. Prompts were answered through a pty. `orch setup`, `orch host
register` without `--peer` (passphrase custody, then again after `keys custody convenience` through the
`click_child.py` file-custody seam), `orch-dashboard addon-manifest --write`, `orch addon grant`, `orch dashboard
open --print-url`, `GET /` and the code exchange all ran. Steps 6 and 7 were not repeated (unchanged apart from the
launch command). The service step ran with `ORCH_NO_SERVICE_MANAGER=1`: `orch host enable` wrote the unit and the release
and refused `launchctl`; `orch host disable` and `disable --purge-releases` refused `launchctl` too, so they stopped before
removing anything. Convenience custody on the real login keychain is read from the code.
