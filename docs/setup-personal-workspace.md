# Set up a personal workspace on a Mac

This guide takes you from a fresh Mac to a running personal workspace: orch installed, a workspace created, Mission Control
open, and one small ticket taken through its three gates. It uses plain commands you type into Terminal. Every step says
what you should see and what to do if you don't.

**Out of scope.** The phone app, the relay, publishing and the hub are not part of this guide. They come with the hub.

**The one-line installer is parked.** Until it exists, this manual path is the supported way. It uses plain commands rather
than the `orch setup` wizard, so that every step is visible and can be checked on its own. Step 6 (the background service)
is the least tested part.

## Words you will meet

- **Workspace**: a folder (usually your project) where orch keeps tickets, as plain files in a hidden `.orch` folder.
- **Host**: a small background program on your Mac. It is the only thing that holds your keys.
- **Mission Control**: the dashboard in your browser. It shows what is waiting for you.
- **Gate**: a point where only you can say yes (requirements, plan, verdict). You sign each one with your passphrase.
- **Grant**: a time-limited permission you give to agents. An agent without a grant can only ask questions.

## 1. Before you start

You need a Mac with Terminal, a normal user account (not root), and these tools:

| Tool | Why | Check | Install if missing |
|---|---|---|---|
| Homebrew | installs the others | `brew --version` | follow <https://brew.sh> |
| uv | installs orch and Python for you | `uv --version` | `brew install uv` |
| git 2.32 or newer | orch refuses older git | `git --version` | `brew install git` |
| Node 24 or newer | builds the Mission Control page while orch installs | `node --version` | `brew install node@24` (then follow brew's note about the PATH) |
| Claude Code | the agent (optional until step 3) | `claude --version` | <https://docs.claude.com/en/docs/claude-code> |

Python 3.14 is needed too, but uv fetches it by itself.

**What you should see:** every check prints a version number. Node must print `v24` or higher.

**If not:** install the missing tool with the command in the last column and run the check again. If `node --version`
prints something older than v24, the install in step 2 stops with "the dashboard UI could not be built".

The guide was checked on macOS 27 (Apple silicon). Other recent macOS versions are expected to work; if one doesn't,
please report it.

## 2. Install orch

Install orch and Mission Control together, pinned to one exact version. `PIN` is the commit the owner tells you to use.
This guide was checked with `268838a1e46dedf5d0ae88ea1875c5bf9c48a58f`.

```bash
PIN=268838a1e46dedf5d0ae88ea1875c5bf9c48a58f
G=git+https://github.com/severinlindenmann/orch-core@$PIN
uv tool install \
  --with "orch-dashboard @ $G#subdirectory=addons/dashboard/server" \
  "$G#subdirectory=plugins/orch-core"
uv tool update-shell
```

Close the Terminal window and open a new one, then:

```bash
orch --version
```

**What you should see:** the install takes about a minute and ends with `Installed 1 executable: orch`.
`orch --version` prints `orch 2.0.0.dev0 (development build)`.

**If not:**

- `command not found: orch`: you did not open a new Terminal window after `uv tool update-shell`, or the shell did not
  pick up `~/.local/bin`. Run `export PATH="$HOME/.local/bin:$PATH"` and try again.
- `the dashboard UI could not be built`: Node is missing or older than 24 (step 1).
- `Repository not found` or a login prompt: your Mac cannot reach GitHub. Check the network and try again.

Mission Control has no command of its own. It is started by `orch dashboard` (step 7).

## 3. Install the Claude Code plugin

The plugin gives Claude Code the orch instructions and skills.

```bash
claude plugin marketplace add severinlindenmann/orch-core
claude plugin install orch-core@orch-core
claude plugin list
```

**What you should see:** `Successfully added marketplace: orch-core`, then `Successfully installed plugin: orch-core@orch-core`,
and the list shows `orch-core@orch-core`, `Status: enabled`.

**If not:** `claude: command not found` means Claude Code is not installed (step 1). If the marketplace step times out,
run it again.

Note: the marketplace follows the repository's default branch, not your pin. The plugin holds instructions only. If
`orch status` ever says the instructions are stale, run `orch instructions sync`.

Skip this step if you use another agent such as Codex: `orch init` (step 5) writes `AGENTS.orch.md` for every agent.

## 4. Pick the folder for your workspace

Use an existing project folder, or make a new one. It must be owned by you and sit under your home folder (not in `/tmp`).

```bash
mkdir -p ~/projects/my-project && cd ~/projects/my-project
git init
```

## 5. Create the workspace (`orch init`)

Run this yourself, in your own Terminal. An agent can't run it.

```bash
orch init --prefix DEMO --name "Your Name"
```

`DEMO` is the short prefix for your ticket numbers (`DEMO-0001`). Pick something that fits the project.

orch now does three things, and waits for you each time:

1. **Passphrase.** It shows a generated passphrase of six words. Write it down or save it in a password manager: it is
   shown once and unlocks every signature. Then type it again at `Passphrase:`. (You may type your own instead; it needs
   at least 14 characters.)
2. **Recovery code.** It shows 24 words. They are the only way back to your key if you lose this Mac or the passphrase.
   Write them down in order and keep them somewhere safe. Press Enter, then orch asks for three of the words
   (`Word 5:`) to check you wrote them down.
3. **Signing screen.** It shows what you are signing (`create workspace DEMO`). Type the word it asks for, then your
   passphrase again.

**What you should see:** `ok init DEMO seq=1`, the folder of the workspace, and `device key: passphrase`. The folder now
has a hidden `.orch` folder plus `AGENTS.md`, `CLAUDE.md` and skill folders for your agents. Check:

```bash
orch status
orch doctor
```

`orch status` prints `ok status` and `no claim` (it shows your name only once the host is on, step 6). `orch doctor` prints
`ok doctor 0` (zero problems).

**If not:**

- `custody.no_prompt`: you ran it somewhere without a keyboard (a script, an agent). Run it in your own Terminal window.
- `passphrase needs at least 14 characters`: type the generated phrase exactly, or a longer one of your own.
- `custody.unsafe_mode`: the folder (or a parent) can be changed by other users. Use a folder under your home folder.
- `no usable passphrase after three tries; nothing was created`: nothing was written. Run it again.

## 6. Turn on the host

The host keeps your keys and enforces the gates. Turn it on once; macOS then keeps it running.

```bash
orch host enable
orch host register .
orch host status
```

`orch host enable` installs a macOS background service (a LaunchAgent called `io.orch.host`) and starts it.
`orch host register .` tells the host about this workspace. After that, orch commands go through the host.

**What you should see:**

```text
The orch host is running as a launchd service.
...
host: answers on /Users/you/.config/orch/run/control/host.sock
workspace DEMO (...) ok socket=answers person=p_... click-to-sign=off
here: /Users/you/projects/my-project: host mode (...)
```

**If not:**

- `host: does not answer`: run `orch host enable` again, then `orch host doctor` and read what it names.
- `host.unavailable`: the service is not running. `orch host enable` starts it.
- `launchctl refused`: your Terminal has `ORCH_NO_SERVICE_MANAGER` set (a test setting). Unset it.
- A service installed for another folder or Python: `orch host enable --replace`.

Leave `click-to-sign=off`. It is the safe default and you don't need it (see step 8).

## 7. Open Mission Control

Mission Control is an add-on, and an add-on runs only after you turn it on for the workspace. Do that once.

> **Temporary step (until [#951](https://github.com/severinlindenmann/orch-core/issues/951) is fixed).** Today Mission Control
> stays empty until you run this. When #951 is fixed the dashboard will offer it itself and this step goes away.

```bash
orch addon grant dashboard
```

orch shows what you are turning on (`serve web pages from this computer`); type the word it asks for, then your
passphrase. You should see `ok addon.granted dashboard`, then the add-on's version, its package digest and
`capabilities serve_http`. Then open it:

```bash
orch dashboard
```

**What you should see:** your browser opens Mission Control on a `127.0.0.1` address. It shows your workspace `my-project`
(`DEMO`), your name with the role `owner`, and the Today view: "Nothing needs you right now". The link works once and
for 30 seconds, so nobody else can reuse it.

**If not:**

- The page says `Could not load Today` and the sidebar is empty: you skipped `orch addon grant dashboard`, or the host is
  not running (`orch host status`).

- `no browser could be opened`: run `orch dashboard open --print-url` in a Terminal window and open the printed link
  within 30 seconds.
- `the dashboard addon is not installed`: repeat step 2 and keep the `--with` line.
- To close it: `orch dashboard stop`.

## 8. Signing

Every gate is signed with the passphrase you chose in step 5. There is nothing more to set up. In practice:

- In Terminal, `orch approve DEMO-0001 requirements` shows exactly what you are signing, asks you to type the ticket
  reference, then your passphrase.
- In Mission Control, the Approve button shows the same text. If it hands you a short command to run, paste it into
  Terminal and sign there with your passphrase.

There is an optional "click-to-sign" mode that signs with one click and keeps a key in the macOS keychain. It is off by
default, weaker than the passphrase, and it can't be combined with a phone later. Don't switch it on for this guide.

## 9. Give an agent a grant and take a first ticket through the gates

An agent can only do work while you have given it a grant. A grant runs for a limited time and you choose how long.
Use two Terminal windows: **yours** (signs) and **the agent's** (has the grant). Never put the grant in yours.

### 9.1 Give the grant

In your window:

```bash
orch grant --hours 4 --label "first ticket"
```

orch shows what you are granting; you type `GRANT`, then your passphrase.

**What you should see:** a line `ORCH_GRANT=gr_...` shown once, and `ok grant.issued ... until <time>`. Copy the
`ORCH_GRANT=` line. It is the agent's key and orch keeps only a hash of it.

**If not:** `role.denied` means this person may not issue grants. `custody.wrong_passphrase`: run it again.

### 9.2 Start the agent

An agent needs two environment variables: the grant and a session id. Mission Control's "Start agent" sets both for you.
To start Claude Code by hand, in the agent's window, in the workspace folder:

> **Temporary step (until [#950](https://github.com/severinlindenmann/orch-core/issues/950) is fixed).** Today nothing hands a
> hand-started agent a session id, so you make one with the `ORCH_SESSION` line below. When #950 is fixed orch will do it.

```bash
export ORCH_GRANT=gr_...                    # the value from 9.1
export ORCH_SESSION=s_01$(LC_ALL=C tr -dc '0-9A-HJKMNP-TV-Z' </dev/urandom | head -c 24)
claude
```

Use a new `ORCH_SESSION` for each ticket: a session holds one claim. Then tell Claude: *"File a ticket to add a hello page,
write the requirements and a plan, and wait for my approval."*

**What you should see:** Claude runs `orch status`, files a ticket, fills its sections and stops at the gate.

**If not:** `ORCH_SESSION is not set: an agent event needs its session id` means you started `claude` without the second
variable. Quit, export both and start again.

To try it without an agent, type the agent's commands yourself in the agent's window (with both variables exported):

```bash
orch new "Add a hello page" --type feature
orch section set context -m "I want a page that says hello." --ref DEMO-0001
orch section set requirements -m "A file hello.html exists and shows the word Hello." --ref DEMO-0001
orch section set out_of_scope -m "Styling." --ref DEMO-0001
orch ac add "hello.html shows Hello" --ref DEMO-0001
```

Each prints `ok DEMO-0001 ...`. (`grant.required` means the grant is missing; `invalid.input ORCH_SESSION` means the session
id is missing.)

### 9.3 Gate 1: approve the requirements

In your window:

```bash
orch approve DEMO-0001 requirements
```

**What you should see:** a screen with the context, scope, requirements and acceptance criteria. Read it. Type the
reference it shows (like `DEMO-0001/96APJ0A0`), then your passphrase. Result: `ok DEMO-0001 gate.approved requirements`.

You can do the same in Mission Control: open the ticket, press Approve, and sign as in step 8.

### 9.4 Gate 2: approve the plan

The agent writes the plan and the task list (agent window):

```bash
orch section set plan -m "Write hello.html with one heading." --ref DEMO-0001
orch section set decisions -m "Plain HTML." --ref DEMO-0001
orch task add "Write hello.html" --verify "grep -q Hello hello.html" --proves AC1 --ref DEMO-0001
```

You approve in your window:

```bash
orch approve DEMO-0001 plan
```

**What you should see:** the plan and the exact task list, then `ok DEMO-0001 gate.approved plan`.

### 9.5 The agent does the work and hands over

Agent window. The work goes in the work folder that `orch claim` prints (inside `.orch-work`):

```bash
orch claim DEMO-0001
cd .orch-work/DEMO-0001
orch task start T1
echo "<h1>Hello</h1>" > hello.html
orch task done T1 --run
orch artifact add hello.html --ac AC1
orch section set verification -m "hello.html contains Hello."
orch submit
```

**What you should see:** `ok DEMO-0001 claim.taken`, `task.started`, a line `running the verify command ...`, then
`ok ... artifact.added` and `ok ... ticket.submitted testing`.

**If not:** `ac.evidence_missing` means an acceptance criterion has no evidence yet: add it with
`orch artifact add FILE --ac AC1`.

### 9.6 Gate 3: your verdict

In your window:

```bash
orch verdict DEMO-0001 pass
```

**What you should see:** the verification text, the acceptance criteria and the files that count as evidence. After you
type the reference and your passphrase: `ok DEMO-0001 verdict.given pass`. The ticket is now done: `orch show DEMO-0001`
prints `done`, both gates `ok` and `AC1 [x]`.

To send it back instead, use `orch verdict DEMO-0001 fail -m "what is wrong"`.

## 10. Check that everything works

```bash
orch --version        # orch 2.0.0.dev0 ...
orch status           # ok status person=Your Name (once the host is on)
orch doctor           # ok doctor 0
orch check            # ok check 0
orch host status      # host: answers ... workspace DEMO ... ok
orch dashboard --print-url   # a 127.0.0.1 link (open it within 30 seconds)
```

`orch doctor` and `orch check` report `0` problems. If `orch doctor` reports problems, run `orch host doctor` and read what it names. When all is well it prints the host
state (`host: answers on ...`, `workspace DEMO ... ok`) and ends with the line `ok`.

## 11. Uninstall

In this order, so that nothing is left running:

```bash
orch dashboard stop            # close Mission Control
orch host disable              # stop and remove the background service
claude plugin uninstall orch-core@orch-core
claude plugin marketplace remove orch-core
uv tool uninstall orch-core
```

This keeps your workspaces and keys (`~/.config/orch`) and the `.orch` folder of each project. To remove them as well,
delete `~/.config/orch` and, in each project, the `.orch` and `.orch-work` folders, the orch block in `AGENTS.md` and
`CLAUDE.md`, and the skill folders under `.agents` and `.claude`. Do this only when you are sure: without the passphrase
and the recovery code the keys can't be brought back.

The host's program copy (`~/.local/share/orch`) is read-only. To delete it, run `chmod -R u+w ~/.local/share/orch` first.

## How this guide was checked

Every step was run literally on an empty home folder, with a clean environment, on macOS 27 and with the commit named in
step 2. The result per step is in the pull request. The background service itself (`orch host enable`) was run on a
throwaway home once, and in the final run the host was started in the foreground instead. Not walked through: running `claude` itself (it needs a login), the
Approve button in the browser, and click-to-sign.
