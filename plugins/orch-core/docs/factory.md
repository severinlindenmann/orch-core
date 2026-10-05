# AI Factory (phases 1 to 6)

One epic in, finished work out: you write an epic and start it as a factory, and agents split, specify,
auto-approve and build its children. You hear from them when they need a permission they do not hold, and at the
end for the verdict. Issue #2 tracks the whole feature; this page describes what phases 1 to 6 ship.

AI Factory is **off by default**. Phase 1 works from the terminal; phase 2 adds the dashboard surface, phase 3 the
Ready report and the Stopped message, phase 4 the runner that keeps the agents going, and phase 5 the core of Dark AI
Factory (no permission prompts while it runs) with its dashboard start, run view and factory list, and phase 6 the
release recipe (merge, dev and production stages the runner runs by itself for a Dark epic that signs them), all
described below.

## Switching it on

Set `factory.enabled` to `true` in `orchestrator/config.json`. That alone changes nothing: a factory epic needs
your signed start, and switching the factory off again turns its epics back into ordinary delegated epics.

## Starting a factory epic (you only)

```bash
orch approve <epic> requirements --factory
```

This is the epic approval you already know, with delegation, run in your own terminal with the typed
confirmation (owner decision D7). It signs one charter into the ledger with the factory's limits:

- up to **25 children or 72 hours**, whichever comes first (D5; `--max-children` changes the count);
- children of **size m or smaller** only; larger ones wait for you (D6);
- everything the existing delegation already rules: agents auto-approve only children they wrote, requirements and
  plan, once per child, while the epic text is unchanged and the delegation is not paused.

When the time budget is used up, agents stop: no new auto-approvals, no claims or task starts on its children, and
grants answer nothing until you decide again. A used-up budget (time or children) is a card in `orch permit list`;
`orch epic show <epic>` shows the state.

The child count also comes from one marker per auto-approved child, kept beside the ledger outside the repository,
not only from the event log or ticket files, so editing those cannot understate it. The budget is decided by the
signed charter alone: the factory switch does not lift it. At the limit, a child the delegation approved goes on;
any other child without a human approval is refused a claim or task start.

## What changes for agents in a factory epic

- `orch ask` is refused. The agent decides within the epic's text and records why in the ticket log (a note, never
  an answer), or leaves the item out and lists it as not built.
- A missing permission is a request, not a question. A command the hook denies with a request id (P-n) already has
  its request; only a denial without one (an auto-mode classifier denial) needs `orch permit request "<command>"
  --ticket <id> --reason ...`. It signs nothing and grants nothing. A request for a command the Dark profile already
  allows files nothing (see "Requests the profile already covers").
- The verdict stays yours (D1): children go to testing as usual, and you sign the epic's verdict. The one exception
  is a Dark charter you sign with `close` (see "Closing by itself"), which replaces your verdict for that run only.

## Permissions: one system, answered by you

orch-core's plugin registers a Claude Code `PermissionRequest` hook (`orch permit hook`). Outside a factory session,
or with the factory off, it gives no answer and the harness asks you as usual (with the factory off it reads only
the workspace config). A factory session is one the runner launched and bound to
one factory epic (phase 4, below). Claims in tickets, environment variables and session ids an agent chose give no
factory treatment, and a session the runner did not launch gets none: the harness asks you as usual. In a factory
session:

- a **live signed grant** for this exact command answers `allow`;
- otherwise the hook files a request (one per epic and command while it is open) and answers `deny` with "waiting
  for permission P-n", so that child parks and the others go on;
- an error, an unreadable ledger or anything unexpected never answers `allow`;
- prompts for anything other than a shell command are denied, so run factory sessions in a permission mode that
  does not prompt for file edits (accept edits; auto mode only for a model that has it, not Haiku);
- tools that never prompt in that mode never reach the hook at all (see "outward tools" under "Readiness checks").

A request's command text and reason are kept beside the ledger in your orch config dir, not in the repository; the
event log only records that a request with that id and command hash was filed. A once grant's use is recorded there
too, so every checkout of the workspace sees it. The guard keeps agents away from these records as it does from
the ledger.

You answer requests in your own terminal:

```bash
orch permit list                     # open requests and every grant with its state
orch permit grant P-7                # once: used up by the first matching prompt
orch permit grant P-7 --for-epic     # every prompt for this exact command, while the factory is active
orch permit deny P-7
orch permit revoke <grant id>        # an action already running finishes; the next one asks
```

Each answer prints the full command and needs the typed request id. Grants, denials and revocations are signed
ledger entries; agents cannot write them (the commands are human-only in orch itself, and the guard denies them).
A grant binds the epic, the delegation it was given under, the exact command text and its sha256. There are no
wildcards. A grant for the epic ends when the epic is done, paused, changed or approved again, or when the budget
is used up.

Some commands are never grantable, and no request is filed for them. The list is coarse and errs towards refusing:
anything the guard denies, orch's permission commands, starting the dashboard, the harness's own settings, hooks and
plugins, orch's config, state and ledger, the variables that decide where orch keeps its records, elevated rights,
merging pull requests, force pushes, deleting remote branches, sweeping recursive removals, a shell running a
substituted command, permission changes on orch's config dir, and any
command text outside printable ASCII or spanning several lines. Requests are shown with such characters escaped.

## Harness settings and auto mode

orch never writes into the harness's settings (D2 B). There is no *Always*: the hook answers every prompt itself.

A hook cannot overturn an **auto-mode classifier denial**: the harness does not consult a `PermissionRequest` hook
for an action its classifier has already denied. So the factory's agents file `orch permit request` after "Denied
by auto mode". Your grant then answers the next prompt the harness raises through the hook; if the classifier
denies the same action again, that is a new request each time. Making such an action pass the classifier is your
own settings change, never orch's. The test suite covers this path (`tests/test_factory.py`) without touching any
real settings.

## On the dashboard (phase 2)

Only while `factory.enabled` is on; otherwise none of it is drawn and the routes refuse. Everything here is yours:
the dashboard's own cookie and same-origin checks apply, and orch refuses these answers to a process running inside
an agent harness, as it does for the terminal commands. Each is a signed ledger entry written by the same functions
as `orch permit ...`, with the dashboard's inline confirm (no popup), bound to the hash of the command the card shows.

- The human-only check runs in the dashboard process, as for approvals: keep the dashboard link and its cookie
  yours, because a grant lets a command run.
- **Start**: the epic's approval form has a "Start as an AI Factory" choice (25 children or 72 hours, size m or
  smaller); it is the same signed charter approval as `orch approve <epic> requirements --factory`.
- **Epic page**: a factory section with the state, children used out of the limit and hours left, the epic's open
  permission cards, its standing grants with Revoke, and the budget card once the budget is used up.
- **Permission cards** on Today and in the Board's Your move: the exact command (escaped), the reason, the epic and
  the asking ticket, with Grant once, Grant for this epic and Deny. They are not part of the decision count.
- **Board**: Group by "Factory epic" gathers each factory epic and its children; everything else sits under
  "Not in a factory".

## Ready and Stopped (phase 3)

Both are derived, read-only views of an epic whose signed charter is a factory one. Nothing in them approves, grants,
starts or signs anything, and nothing an agent can write turns one on or hides it: they come from the signed charter,
the signed ledger entries, the budget markers beside the ledger and the tickets' own state.

**Ready.** Every child is in testing or done, at least one is in testing, and every acceptance criterion of every
child in testing cites evidence. Status words in a ticket file can be edited, so each must be backed by a record: a
done child by a signed verdict or close, a testing child by orch's own record of its move into testing (by the
session that claimed it, every task closed). A child the records do not back is shown as "not verifiable" and no
report is made. The evidence is what the agents wrote, not a check; the report says so. It lists, per child, how it
was approved, its criteria count, and in full the acceptance criteria and Verification text your verdict covers
(plus the artifacts those show, named); its title, links and Findings are shown apart as context your verdict does
not cover. All as plain text. It has one action,
yours: **Accept the epic and close its children**. That is the epic verdict that already existed (`orch verdict <epic>
done`, or the epic page): it signs the epic's verdict hash, the hash of exactly the children and evidence the report
showed, and is refused if any of it changed since. The report never closes a child by itself, and an agent cannot
give the verdict (D1: the verdict stays whole).

**Stopped.** The factory is at a dead end the agents cannot leave on their own. The message names every reason that
holds, and what you can do about it; it has no action of its own. A reason is one of:

- the budget is used up (25 children or 72 hours, or what you signed);
- a child you sent back with a follow-up verdict three times is still not finished (counted from your signed verdicts);
- a request you denied still holds a child back (counted from your signed denials);
- the approval ledger on this machine was cut, so no approval or grant counts.

Stopped is worked out on its own: a Ready epic can also be Stopped (say, the budget ran out while the work was
done), and an edited status cannot hide a real reason. Under a cut ledger the other reasons still show, from the
entries that still verify. A child blocked in some other way, or one that failed its own checks, is not a
reason yet: those need the runner's records and come with it.

**Where you see them.** Today (an "AI Factory" section) and the Board's Your move strip show a card each; the epic
page shows them in its factory section. Both count as items waiting on you: they are in the Today headline, the
menu badge, the tab title and the session-start summary, and `orch wait <epic>` returns when the factory
becomes Ready or Stopped (event kind `factory.ready` or `factory.stopped`, actor `orch:factory`, derived and not
written to the event log; a child's own wait does not). It wakes once per state: the cursor it prints names the
state, and passing it back as `--after` waits for the next change. A Stopped card for a used-up budget replaces the budget card.

## The runner (phase 4)

The dashboard server can keep the agents going for you, on this machine, in a tmux server of its own (D4). When you
start a factory epic **from the dashboard** (the epic page's "Start as an AI Factory"), that signed start also arms the
runner for that delegation. Every few seconds the dashboard then, for each armed and active factory epic:

- **plans**: an epic with no child at all gets one **planner** session instead (below);
- **launches** one agent session (tmux must be installed) for each child that is auto-approved or covered by your
  charter, is size m or smaller, and is open, in progress or waiting: at most **3 at a time**
  (`factory.max_concurrency` in `orchestrator/config.json` can only lower that), at most the charter's **max
  children** distinct children, and a few launches per child. Those counts are markers beside the ledger, so editing
  tickets cannot lower them;
- **wakes** a child whose session ended while it was parked, when something it waits for changed: your grant, denial
  or revocation in that epic, or the child's own text or approvals. Nothing else restarts it;
- **stops** every session of the epic, and ends its binding, when you pause the epic, edit its text, approve it again,
  the epic is done, the time budget is used up, the ledger is cut, the factory is switched off or the user-scope
  settings below stop holding; and a child's session when the child is done. A used-up child budget only stops new
  children: those already running go on. When the dashboard stops, every session stops and every binding ends (each
  child starts again with the next dashboard); when it starts, a binding whose session is not running ends. If tmux does
  not answer, the runner concludes nothing that round and asks again.

**Waking a session that waits (the idle nudge).** An interactive Claude session that waits for you does not end, so
the wake above never reaches it (in the live run each pane had to be told by hand that the cards were answered). So
after you answer something in its epic (a grant, a denial, or for a Dark epic a rule added to the Dark profile;
a revocation or a removed rule takes a permission away and answers nothing), the runner types **one** built-in line
into the session's pane and presses Enter: "The human answered your
permission requests. Retry the blocked commands, then finish your ticket and move it to testing." (a denial alone gets
a line saying to do without the command and record why; a planner one saying to finish splitting the epic). The text
is a constant in orch, never taken from a ticket, the config or an agent, and the tmux launcher refuses any other.
The runner reads every session's pane each round with tmux's `capture-pane`. It types only when the pane shows
Claude Code's footer hint ("? for shortcuts" or "shift+tab to cycle") and an empty input line (the last `>` line,
and only when the input box's border is right above it: a prompt echoed in the transcript never counts), nothing in its last lines looks like a running command ("esc to
interrupt"), a permission, trust or other menu ("Do you want", "1.", "(y/n)"), and the screen stayed exactly the same
for 45 seconds over two rounds; at most 3 times per session and 5 minutes apart. It reads the pane once more right
before typing and types nothing unless the input line is still empty; after typing it reads the pane up to five times
over about a second (tmux redraws asynchronously) and presses Enter only when the line sits on the input line itself
and no menu, permission or trust prompt or running command is on screen (Enter would answer that instead); otherwise
it clears the input line (Ctrl-U), presses nothing more, and counts the attempt.

The same readings tell the run view whether anything runs: when every live session of a run has shown the empty
prompt, unchanged, for 3 minutes and no card is open, the run view says "Sessions are waiting at their prompt: nothing
is running" (chip "Idle at prompt") instead of "Sessions are running on its children" (in the live run's second round
all three sessions sat idle for minutes under "Working"). A session the runner has no reading of (no record, a damaged
one, a pane it cannot read) counts as working: the view never claims more than it read. Anything else, or a record or pane
it cannot read, types nothing. The run view says how often it nudged. What it cannot tell: a session that waits at a
prompt Claude Code draws differently from these markers (a future version) is never nudged, and an idle session that
was not waiting for that answer still gets the line (it is idle anyway). The runner keeps a small record per session
(`permits/nudges/` in your orch config dir, guarded like the rest of the permits folder).

**A session that ends right after it starts.** The runner's tmux server keeps a pane after its process exits
(`remain-on-exit`), so the runner reads its exit status and last screen, then ends it. A session that ended within 90
seconds of its start is recorded (`permits/early-ends/`: the exit status and the last 15 non-empty lines, each cut to
200 characters, everything outside printable ASCII escaped; one record per child, the latest), and the run view says
"A session ended right after it started" with those lines, instead of "Waiting for children", until that child (or the
planner) is started again. Such a session is parked like any that ended: fix the cause, then answer a card in the epic,
change the Dark profile, or approve the epic again.

**The planner.** An armed, active factory epic that has no child at all (in any status) gets one planner session, under
the same gates as a child's session (the factory on, the user-scope settings below, the budget not used up, not
paused, edited or suspended, tmux and the programs found) and taking one of the concurrency slots. It is bound like a
child's session (a random session id, the binding written before the start, trusted only for the recorded process,
the checkout and start folder recorded); its binding names the epic itself as its child, so the permission hook gives
it that epic's grants, Dark profile and refusals. It starts in the workspace root. Its prompt is built in, like the
work prompt: read the epic, split it into children within the charter's limits with `orch new --epic <epic>`
(Requirements and Acceptance criteria from files: `--requirements-file`, `--acceptance-file`), write a one-paragraph
Plan with `orch section set <child> Plan -m "..."` when the size needs one, decide instead of asking (`orch ask` is
refused; reasons go to `orch log`), approve each child with `orch epic auto-approve <child>`, build nothing, leave
the epic's text alone, and stop. A planner starts at most twice per start (its own markers, not counted against the
charter's children), and again only when its session ended, the epic still has no child, and something it waits for
changed (your grant, denial or revocation in that epic, or a Dark profile change), as for a parked child; the runner
never starts a planner for an epic that has a child. An interactive session does not end by itself, so besides every
reason a child's session stops, the runner stops the planner once the epic has children and every one of them is
approved (auto-approved, covered or approved by you: its work is done and its slot goes to the children), and at the
latest 30 minutes after it started, children or not (a planner stopped there without a child counts as one of its
two starts; one stopped with children leaves the unapproved ones to you). A planner the dashboard's own shutdown
stopped does not count: it starts again with the next dashboard. Two dashboards on one config dir start one planner
(the check, the count and the binding happen under the delegation's lock, as for children).

**The worker prompt.** A child's session gets a built-in prompt of its own (never the workspace's work prompt, which
an agent can edit): work on the child, following the `orch-work-on-ticket` skill if the agent has it, with the command
forms written out, because a session that has only orch's hooks at user scope has no orch skills (in the live run the
agent invented `orch work-on`). Both built-in prompts say the same about command shapes: one plain command per tool
call (no `&&`, `;`, pipes, `2>&1`, `|| true`, other redirects or substitutions), titles, `-m` texts and commit
messages as short plain sentences without line breaks, backticks, dollar signs or backslashes, `orch permit request`
only for a command actually denied with a request id, no variants of a denied command, and never `orch instructions
sync` or `orch setup`. The worker's: `orch claim`, `orch show`, one `orch task add <child> "TASK"` per task, `orch
task start`, `orch task done <child> TN` (no `-m`; notes go to `orch log`), its work, Verification from a file with `orch section set <child> Verification --file FILE`, then `orch move <child>
testing`. Both prompts spell out orch's evidence format, because the live run's second round reached testing on every
child and still could never be Ready: the planner wrote plain bullets (no criteria at all to orch) and the workers
wrote prose with check marks (no evidence). The planner writes each criterion as a top-level checkbox line (`- [ ] The
export writes one row per order to out.csv`); a worker proves each with one top-level Verification line in order
(`- AC1: ran the export on the sample orders and saw 3 rows in out.csv`), leaves the criteria unticked (a tick proves
nothing, and editing that section risks its approval), checks `orch show`, and only then moves to testing. The test
suite parses the prompts' own examples with orch's evidence parser. Both prompts also say to create files with the file
tools (a multi-line heredoc never matches a rule), to read files with the Read tool instead of extra commands, and to
quote revisions such as `"HEAD^"`. The test suite checks every orch command and
option either prompt names against the CLI, every git verb against real git, and that the orch and git-basic
baselines match each command it tells the agent to run. A prompt is advice: a model can still ignore it, and then
its chained command stops for a card as before.

**Commits.** One rule (`own_worktree`) decides where a child's session starts in a worktree, whether its prompt
tells it to commit, and whether a commit may run: the one worktree the child names must be a linked git worktree
below the workspace root (its `.git` a file naming a git dir in the common git dir's `worktrees/` folder), not keep its
refs in reftable, have HEAD on a real branch, and that branch must not be a default branch and must name the child
(its id as a word). Default branches, compared without case: `main`, `master`, the release recipe's base, and what
every remote's `HEAD` names; a recipe that exists but cannot be loaded leaves the default unknown, so nothing passes.
Only such a session is told to commit, on that branch: `git add FILES` and a `git commit` in the workspace's own commit
format, rendered at launch from `commit.subject` and the required body lines of `commit.body` (plus `Rollback` when
`commit.rollback` is on), one `-m` each, for example `git commit -m "<child> short summary" -m "What: ..." -m "Why:
..." -m "Risk: ..."`, so the message passes orch's commit-msg check (the test suite runs that check on it). A config
whose subject or labels are not plain words gets no commit instruction. Every other session (a child that names no
such worktree starts in the workspace root, and the baseline cannot create a branch) is told not to commit: it leaves
its changes in the working tree and says so with `orch log`.

Whatever the prompt says, a runner-bound session's command that may make or move a commit (the word `git` and
`commit`, `commit-tree`, `merge`, `cherry-pick`, `revert`, `am`, `rebase`, `pull`, `update-ref` or `stash` anywhere
in its text, quotes and backslashes taken out, so wrappers such as `env`, `sh -c` or an alias count; or a `GIT_DIR` /
`GIT_WORK_TREE` assignment) is refused unless the folder the runner started it in passes the rule above, the session's
folder is that folder or below it with no other git checkout in between, and the command carries nothing that points
git elsewhere (`-C`, `--git-dir`, `--work-tree`, `GIT_DIR`, `GIT_WORK_TREE`, a `cd` or `pushd`). The guard checks this
on every command (PreToolUse runs in every permission mode, so an allow rule, auto mode or bypass does not skip it),
and the permission hook checks it again; a session whose binding exists but does not verify is refused. Anything that
cannot be read is a refusal.

A session the runner bound works on its own epic only. orch refuses it, whatever the profile or a grant allows:
`orch new --epic` and `orch link --epic` naming another epic, and every change to an existing ticket (claim, release,
move, section set, state, log, link, tasks, artifacts, ask, auto-approve, a follow-up's link back to its source, and
any other change: they all go through one check in orch's single write path) unless the ticket is its epic, one of
that epic's children (a parent named by ticket id; an external key never makes a ticket a child), or a ticket the
session created itself, such as a follow-up (`orch new` without `--epic`, or `--from` one of its tickets). orch notes
each ticket a factory session creates in that session's record beside its binding (`permits/sessions/<id>.created/`
in your orch config dir); a ticket you or another session filed during the run is not the session's. `orch permit
request` from a factory session names a ticket of its own epic. Reading stays open (show, list, search, `orch permit
list`). orch fails closed here: when it cannot tell whether a session is a factory session (a binding record that
does not verify, or an error while looking), it creates and changes nothing. An agent with no binding at all works as
before.

The limits of these checks, stated plainly. The created-tickets record is written by the session's own orch process
(an agent's process): the guard keeps the agent's tools and commands away from the permits folder, and a Dark profile
rule never matches a command naming it, which is the same best effort that protects the bindings. orch tells a
factory session from others by the session id Claude Code exports to the agent's commands (`CLAUDE_CODE_SESSION_ID`;
the runner starts the agent under `env -i` with an allowlist and does not set it itself); a command that drops or
changes that variable needs a card in a Dark run, and elsewhere the harness asks you as usual. And all of this guards
orch's own commands only: ticket files are plain files in the repository, so a profile rule that runs arbitrary code
(`pytest`, `make`, `npm run ...`) can change any ticket file without orch. Prefer exact rules (see "A prefix rule
trusts the repository").

The runner never approves, grants, signs or starts a factory by itself. It does nothing unless `factory.enabled` is on,
the epic's signed charter is a factory one and still active, and you started it from the dashboard (the terminal's
`orch approve --factory` signs the charter but does not arm the runner). It runs only in a process that is not under an
agent harness, like the dashboard's other human actions.

**Turn orch on at user scope first.** The launched session ignores project settings (see below), so orch's guard and
permission hook must come from your user-scope Claude settings (`CLAUDE_CONFIG_DIR`, else `~/.claude`): enable the
orch-core plugin there, or carry the orch guard and permission hooks (commands whose program is `orch` or an absolute
path ending in `/orch`), and `disableAllHooks` must not be on. Until that holds, the runner starts nothing, stops
what runs, and the epic page says why.

**Readiness checks.** A settings file that names the hooks does not prove they run: in the live run orch's hooks failed
with a non-blocking error in every session (the plugin's `bin/orch` needs `uv`, which was not on the session's PATH),
so the guard and the permission hook silently did nothing. Before it starts a session, the runner therefore runs
these checks under the sessions' exact environment (`env -i`, the session PATH, the allowlisted variables) and shows
the failures on the run view (with the last lines of a failing program's output, escaped). While a blocking check
fails it starts nothing. A result is kept for at most 60 seconds, and only while the programs it probed and the hook
commands it ran are still the ones the runner would use (checked again, without running anything, right before a
launch); all the programs of one run share a budget of 20 seconds (a program still running then is killed and counts
as failed), and at most 64 KB of each one's output is kept. An error inside the checks is a blocking failure with its
reason, never a silent pass. The hook programs run with the words Claude Code would run them with, but never through a
shell, in an empty folder of the runner's own, and a plugin's hook with `CLAUDE_PLUGIN_DATA` pointing at the runner's
own data folder (`permits/plugin-data` in your orch config dir, where the plugin's `bin/orch` keeps its venv): nothing
is written in the workspace or the plugin's install folder (the test suite checks both). An absolute hook program is
run only when it passes the same trust rule as the other programs (above). A relative `CLAUDE_CONFIG_DIR` blocks: each session would read another
folder depending on where it starts.

- *programs* (blocks): `claude`, `env`, `orch` and `uv` must not lie inside the workspace (as found or after links):
  agents write there, so an editable or workspace-local install (`<workspace>/.venv/bin/orch`) would put
  agent-written code on every session's PATH. Fix: install them outside the workspace and start the dashboard from
  there. Independently of this check, a program inside the workspace is never put on a session's PATH and the runner
  refuses to launch a `claude` or `env` there, and any program whose folder is not owned by you or root, or is
  writable by everyone, is not used at all (a group-writable folder of yours, such as Apple Silicon Homebrew's
  `/opt/homebrew/bin`, is fine).

- *claude* (blocks): `claude --version` must exit 0 and print a version. A wrapper first on the dashboard's PATH that
  cannot find the real claude fails here. Fix: put the real claude first on the dashboard's PATH.
- *orch on PATH* (blocks): `orch` must resolve on the sessions' PATH. Fix: install orch as a tool of your user (for
  example `uv tool install` of orch-core) so the dashboard's PATH finds it, then restart the dashboard.
- *guard* and *permission hook* (block): the hook commands your user-scope settings name (program `orch` or an
  absolute path ending in `/orch`, outside the workspace), or, with the plugin enabled, the plugin's own `hooks.json`
  commands (program exactly `${CLAUDE_PLUGIN_ROOT}/bin/orch`, then `guard` or `permit hook`), taken only from the
  folder Claude Code installed it to (an `installPath` in `plugins/installed_plugins.json` of the user config dir,
  outside the workspace; never the folder this orch runs from), run once with a harmless payload (`true` from a session
  id nobody bound) and must exit 0 with empty or JSON output. Fix: what the output says; usually `uv` (or `orch`)
  missing from the sessions' PATH.
- *trust* (blocks): Claude Code's record (`$CLAUDE_CONFIG_DIR/.claude.json`, else `~/.claude.json`) must say the trust
  dialog was accepted for the workspace or a folder above it (keys and the workspace compared as real paths);
  otherwise a new session waits at that dialog. A file over 256 MB is not read, and the check says so. Fix: open
  Claude once in the folder and accept it.
- *skills* (warns): the orch skills at user scope (the plugin, or `skills/orch-work-on-ticket` in the user config
  dir). Without them the built-in prompts still name every command a session needs.
- *outward tools* (warns): `permissions.deny` in your user-scope settings should list `Artifact`, `WebFetch` and
  `WebSearch`. Tools that do not prompt (under accept-edits, for example) never reach orch's permission hook, so
  nothing else stops a session from using them: in the live run a child published a Claude artifact on its own.

The session PATH is the folders of the resolved `claude`, `orch` and `uv` (each found on the dashboard's PATH and
trusted as below, none inside the workspace), then the system's. The guard keeps agents from writing what decides how every session is guarded: the user-scope `settings.json`,
`settings.local.json` and `CLAUDE.md` and the `plugins`, `hooks`, `skills` and `agents` folders (of `$CLAUDE_CONFIG_DIR`
and of `~/.claude`), `.claude.json`, every folder `installed_plugins.json` names, and the programs orch runs as: the
folders of the `orch` and `uv` the session finds, the tool venv such an `orch` lives in, and orch's own installed code
(none of these inside the workspace or in a source checkout of orch: those are someone's working copy). With the file
tools, as written or after links; in the shell, per simple command, on its text with quotes and backslashes taken out
and `$HOME` spelled `~` (a write that names one, or any write after a `cd` or `pushd` into one in the same line), so
reading them and writing elsewhere in the same line stay open, and a workspace's own `.claude/settings.json` stays
writable. Best effort for shell text, as for the other guarded files.

**What "agent-writable" means here.** For the readiness checks and the launch, a program is agent-writable when it lies
inside the workspace (agents write there with their file tools and shell). Other folders of your user are writable by
any process of your user, agents included; the guard keeps agents' tools away from the ones listed above, best effort,
and nothing else (other folders on your PATH, for example) is protected.

**Claude Code formats assumed, not verified against Claude Code's own documentation:** `projects[<path>]
.hasTrustDialogAccepted` in `.claude.json`; `{"plugins": {"<id>": [{"installPath": ...}]}}` in
`plugins/installed_plugins.json`; the hook entries of `settings.json` and a plugin's `hooks/hooks.json`; and, for the
idle nudge, the footer hints and empty input line of the pane. If Claude Code changes one, the matching check fails
closed (it blocks, or the nudge types nothing) rather than passing.

**Session binding.** At launch the runner generates the session id, records session -> (epic, delegation, child, and the checkout and folder it launches in)
exclusively in the guarded permits folder of your orch config dir, and only then starts the agent under that id. The
permission hook trusts only this record to decide which epic's grants apply, and only for a process running under the
process the runner recorded for that session (same pid and same start time): a copied session id gets nothing
elsewhere. An ended or stopped session loses the record at once. Only a human process writes it: agent processes are
refused, and the guard keeps agents away from the folder. Session ids are random; the runner itself writes them to
no event, ticket, log line or page. The agent's own orch commands do record its session: a claim writes the full
session id into the claimed ticket (its `claim` and `sessions` entries in the frontmatter), and the event log names the
agent with the first 8 characters of it. That grants nothing: the hook trusts the id only for a process running under
the one the runner recorded, so a copied id gets no factory treatment (and the binding is gone once the session ends).

**Where and how a session runs.** The runner's tmux server sits on a socket inside the guarded permits folder (a
private folder), not on the Terminals' socket, so these sessions are not in Mission Control's Terminals page. The
programs it starts (`tmux`, `env`, `claude`) are looked up on the dashboard's absolute PATH entries and used by absolute
path only when trusted: the file owned by you or root and not writable by group or others, its folder owned by you
or root and not writable by everyone (Homebrew's group-writable `/opt/homebrew/bin` is fine; `/tmp` is not). That keeps
out another user's programs and world-writable folders; it is no defence against code that runs as you, which can
write any folder you own (the defences there are that nothing inside the workspace is used, and the guard). When the
runner cannot use `tmux`, `env` or `claude`, it says so: once on the dashboard's terminal and on every armed epic's
run view and epic page, with the path it saw and why it was refused ("tmux was not found at a trusted path: ..."),
never only "Waiting for children". The runner's own lines (sessions started, refused, stopped, nudged) are printed on
the terminal that runs the dashboard. The agent gets `env -i` with a fixed PATH (the
folders of `claude` and, when found the same way, of `orch` and `uv`, then the system's) and a short allowlist of variables, nothing else the dashboard holds. A
session starts in the child's worktree only when the child names exactly one and it is the child's own (a linked
git worktree in the workspace on a non-default branch that names the child: see "Commits"); otherwise in the workspace
root. Residual risk, stated plainly: the operating system does not isolate processes of the same user from each other,
so the guard and these checks are best effort against an agent that tries; they close the obvious routes.

The guard's part is a text check, not a shell. It refuses a command run from inside the orch config dir, a `cd` or
`pushd` that lands in it (resolved with variables, `..` and globs, step by step), a listing of it (`ls`, `find`, `du`,
`stat`, `tree`, also recursive over a folder above it), and any tmux or screen command it cannot show plain: a socket
must be an absolute, literal path with no `..`, outside the config dir (relative ones are refused, because the
working directory is not known to a later command). The same resolution rules cover the file tools (a relative path is
taken from the hook's working directory) and every segment of a command. A path with a symlink component that leads into
the config dir is refused as written, never trusted because of where it points today. The rules are bounded (command
length, glob matches, path depth, time): hitting a bound, or an error inside these rules, is a deny (an unrelated internal error in the guard still lets the
hook fail open and log, as before). Only a tmux or screen command word and its own arguments are judged: a `grep tmux`,
a heredoc body or quoted text is not. A `cd` the guard cannot work out (a substitution, a variable, `CDPATH`) is allowed,
but the working directory is then unknown for the rest of the line: a relative word that is, or can stand for, a name
in the config dir (permits, sessions, ledger*, tmux, ...) is refused with its own message, and so is a bare `*` handed
to a command that reads or lists; `rm -rf node_modules/*` and `for f in *.md` pass. The socket sits in a random-named
folder whose name is kept in a permits file; that only stops guessing and listing. The path is visible in `ps` to
processes of the same user, and the guard stops an agent naming it, best effort. Known limits of a text guard, not built: a word written without a mention of tmux or screen by
concatenation that uses none of the characters it looks for, a string built in another language (perl, osascript,
python), and a script file written and then run.

**A repository's own files.** A checkout's `.git` (its config, refs, packed-refs, info, objects, HEAD, worktrees and
hooks) and a worktree's `.git` pointer file decide what later git commands run: hooksPath, fsmonitor, aliases, refs.
Agents' file tools (Write, Edit, MultiEdit, NotebookEdit) never write a path with a `.git` component, in any case, as
written (a relative path taken from the hook's working directory, `~` expanded) or after symlinks; reading stays
open. The same holds for your own git config, which every git of your user reads: `~/.gitconfig`, anything in
`~/.config/git` or `$XDG_CONFIG_HOME/git`, and `/etc/gitconfig`. In the shell the guard denies a write into those
`.git` files or onto a bare `.git` (a redirect, `tee`, `cp`, `mv`, `ln`, `install`, `rsync`, `dd`, `sed -i`,
`perl -i`, an interpreter's `-c`/`-e`; paths are matched after `//` and `/./` are collapsed) or into your git config;
`git config --global`/`--system` writes, and `git config -f`/`--file` naming a `.git` path or your git config; and
`git config` or `git -c` of a key that runs a program or redirects git (fsmonitor, sshCommand, pager, editor, askpass,
alternateRefsCommand, attributesFile, hooksPath, aliases, includes, filters, `diff.external`, diff, difftool,
mergetool and merge drivers, credential settings, gpg programs, `remote.*.uploadpack|receivepack`,
`url.*.insteadOf|pushInsteadOf`, `protocol.*.allow`), and `git --config-env`. A `git config` or `git -c` whose
command word, key, scope or file is not a plain literal (ANSI-C `$'…'`, `${…}`, `$(…)`, a backslash, a quote inside a
word) is denied too, because its value is only known when the shell runs it. All of these are never grantable. The
text-check limits stay: a `cd` to the folder followed by a relative name, a path held in a shell variable, a string
built in another language, and a script written and then run are not seen. The release step does not depend on any
of this: it fetches only objects from the workspace's `.git` and runs its git without your git config.

**The launch command** is yours, in `factory-command.json` inside the permits folder of the orch config dir, which the
guard keeps agents from reading and writing (never the workspace config, ticket text or anything else an agent can
edit; the prompt is built in):

```json
{"command": ["claude", "--setting-sources", "user", "--strict-mcp-config", "--session-id", "{session}", "{prompt}"]}
```

That is the default; a damaged file means the default. `{session}` is the id the runner bound (the agent must start
under exactly that id) and `{prompt}` the child's work prompt, last. The command is an allowlist: the program is
`claude`; the arguments are `--setting-sources user`, `--strict-mcp-config`, `--session-id {session}` (all three
required), and optionally `--model`, `--verbose` and `--permission-mode` default or plan, each once. Anything else is
refused and the default used (settings, MCP or plugin sources, extra folders, agents, tool allowances, permission modes
that skip prompts): the hook stays the only gate. Run the agents in a permission mode that does not prompt for file
edits by setting it in your user settings, not in this command.

**Worktrees are written by agents.** The launched session ignores the settings and MCP servers a worktree carries
(user settings, which hold orch's hook, still apply), and the runner refuses to launch a child whose worktree has a
`.claude/settings.json`, `.claude/settings.local.json` or `.mcp.json` that is not identical to the workspace's own.
Residual risk: instruction files such as `CLAUDE.md` or `AGENTS.md` in a worktree still reach the session as text.

**Known gap.** An auto-mode classifier denial still needs a card from you each time (see "Harness settings and auto
mode"); the runner does not change that (D2 B).

## Dark AI Factory (phase 5, core)

A Dark factory is an AI Factory epic whose sessions show no permission prompts. That does not mean nothing reaches
you: a command outside the Dark profile is denied and becomes a card for you, and larger children, budget cards and
the verdict still need you. It is built on everything above: the permission hook stays the only gate, orch still writes no harness settings (D2 B), and the runner's launch
command allowlist is unchanged (no settings file, no skip or bypass flags, no permission mode that stops asking).
Instead of a card for every prompt, a Dark epic's hook answers from your **Dark profile**: a signed list of shell
commands Dark runs in this checkout may run.

**How it starts.** A Dark run starts only from the dashboard (below): its start signs the Dark charter and arms the
runner. A Dark charter you sign with `orch approve --dark` in the terminal is not armed, so no agent session is
launched for it.

**Switching it on (you only).** Set `factory.enabled` to `true` in `orchestrator/config.json`, then in your own
terminal:

```bash
orch factory dark on       # human only: typed confirmation; refused to agents and under an agent harness
orch factory dark status   # anyone
orch factory dark off      # anyone: it only takes power away
```

The Dark switch is not a config value: it is a signed setting in the approval ledger, bound to this checkout (as
`orch widget html on` is), so an agent editing `orchestrator/config.json` cannot turn it on. With Dark off, a Dark epic
behaves as an ordinary factory epic: the profile is ignored and every prompt is a card, as before. With the factory
off, nothing here does anything.

**The brakes.** `orch factory dark off` (anyone, signed) turns every Dark epic of this checkout back into an ordinary
factory epic at once; `orch epic pause <epic>` (yours, signed) stops one epic. Known limitation: `factory.enabled`
itself is still a plain config value an agent can edit (as since phase 1). Switching it off only takes power away;
switching it on still needs your signed Dark setting and a signed Dark charter before the profile answers anything. A
signed factory switch is a follow-up.

**Starting a Dark epic (you only).**

```bash
orch approve <epic> requirements --dark       # implies --factory
```

The charter you sign carries `dark: true` (a charter signed without it hashes exactly as before). The text shown
before the typed confirmation says it plainly: Dark runs without permission prompts in the session, only commands the
profile lists run (anything else is denied and becomes a card for you), and it releases and closes only within what
the charter signs: nothing, unless you add `--release merge|dev|prod` (phase 6, below); it closes nothing and the
verdict stays yours, unless you add `--close` ("Closing by itself", below), which replaces your verdict for this run. The
command is refused while Dark is off, under an agent harness, and without a terminal, like every approval.

**The Dark profile** is per checkout: signed ledger entries (add and remove) that name the checkout they were made in,
written only by a human process. The worktrees of one clone share one profile and one Dark switch (agents work in
worktrees, so they must); another clone of the same workspace (the same customer and id prefix), or a copy, has its
own, empty profile and its own switch. In a Dark session the checkout is the one the runner recorded in the session's
binding when it launched it, never the one the agent's working directory or its `.git` file names: a session whose
checkout no longer matches its binding (it moved into another clone, or its `.git` file was rewritten) is denied. The
rules in force are a replay of the entries, each checked again as when it was added: an entry that fails is ignored.
A cut ledger means no rule counts.

```bash
orch dark profile list                          # anyone may read it
orch dark profile add --prefix "npm run verify"  # a single simple command starting with these words
orch dark profile add --exact "make test > out.txt"
orch dark profile add --from-request P-7         # an open Dark card: its command as an exact rule
orch dark profile add --baseline                 # the orch agent verbs a planner or worker needs
orch dark profile add --baseline git-basic       # the git a worker needs to commit its own work
orch dark profile remove <rule id>
orch dark profile prune                          # remove exact rules for compound commands
```

Adding and removing are yours: each prints the rule (or the card's command) and needs its id typed (`--baseline`
prints every rule it adds and needs BASELINE typed; `prune` lists the rules it removes and needs PRUNE typed);
agents are refused in orch itself and by the guard. `prune` removes exact rules whose command is compound (an
unquoted `;`, `&`, `|`, `<`, `>`, `(`, `)` or line break, or a backtick or `$(` outside single quotes): such a rule
matches only that identical text, which agents rarely repeat, so it is clutter left by adding one chained card. An
exact rule that is merely not simple (`ls ~/x`, `pytest tests/*.py`) is kept. The baselines are named: `orch` (the default) and `git-basic`; an unknown
name is refused with the list.

**The git-basic baseline** adds the git a worker needs to commit its work on its own branch: prefix rules `git
status`, `git diff`, `git log`, `git show`, `git add`, `git commit`, the read-only `git ls-tree`, `git ls-files` and
`git rev-parse`, and the exact rule `git branch --show-current`
(`git branch` is never a prefix rule). Nothing that reaches out, rewrites or configures: `git push`, `fetch`,
`reset`, `clean`, `checkout`, `switch`, `rebase`, `config`, `stash`, `git -c …` and `git -C …` stay out, and the
argument shapes below still refuse (`git diff --output=…`, `--ext-diff`, `git log -p`). For any git command a prefix
rule also refuses what reads files outside the repository or rewrites other commits: `--no-index`,
`--pathspec-from-file`, `--template`, `--orderfile`, `--amend`, `--fixup`, `--squash`, `--file` (and every
abbreviation git accepts), a short option word holding `F` or `t`, and any argument (or the value after `=` or `:`)
that is an absolute path, starts with `~` or has a `..` path component. The text of a `git commit` message (the word
after `-m` or `--message`, or attached to them) is message text, not a path: only its option checks apply, so
`-m "Fix the /api path"` matches; `-m` of other git verbs takes no message and changes nothing. `git commit` passes only
where the workspace lets agents commit (`git.agent_may.commit`); elsewhere the guard denies it, and `--baseline
git-basic` adds the other rules and reports `git commit` as not added, with the reason.

**The baseline** is a fixed list of prefix rules for the orch commands an unattended planner or worker session runs:
`orch show`, `list`, `search`, `next`, `state`, `check`, `new`, `section set`, `task add|start|done|skip|block|list`,
`claim`, `release`, `log`, `link`, `move`, `wait`, `permit request`, `permit list`, `artifact add`, `epic show` and
`epic auto-approve`. No human-only verb is in it (approve, answer, verdict, request-changes, reopen, close, ledger,
`epic pause`, `permit grant|deny|revoke`, `dark profile add|remove`, `factory dark on`, addon administration, the
dashboard) and no `ask`. Each rule goes through the same checks as any `--prefix` rule; rules already in force are
skipped, so running it again adds nothing. A human-only form of an allowed verb (`orch move X done`) still does not
run: the guard denies it, so no rule matches it. Without the baseline (an empty profile), every command a Dark session
runs stops for a card; the run view and the New ticket page say so and name both baselines. If a rule cannot be signed, the command lists
which rules it added and which failed (and why), and exits with an error; run it again once the cause is fixed.

What the baseline lets a session do, besides the scope limits above (residual risks, stated plainly):

- **Files.** In a session the runner bound (the same trusted binding the permission hook uses), orch reads the
  files it is handed (`orch new --requirements-file|--acceptance-file|--body-file|--summary-file|--out-of-scope-file`,
  `--file` of `section set`, `state`, `task add`, `ask`, `widget`, `feedback`, and `orch artifact add <file>`) only
  when the file lies inside the workspace, is reached without a symbolic link, and is not in orch's config dir;
  otherwise orch refuses ("an agent cannot hand orch the file ..."). orch opens such a file once, without following a
  link, and reads only from that open file: it must be a regular file with a single hard link (a hard link could
  make a file from anywhere on the volume look like a workspace file), and the same file the checked path named. This
  fails closed: any agent is refused a path that cannot be resolved (missing, a broken link), and every file while orch
  cannot tell whether its session is a factory session. A directory on the way swapped for a link between the check
  and the open is not caught (the final name is opened without following a link). A human, and an agent outside the factory (its
  harness asks you about each command, and it attaches screenshots from /tmp), pass any file, as before. A file inside
  the workspace (a `.env` there, say) can still be copied into a ticket or an artifact: the agent could read it with
  its own tools anyway.
- `orch new` is not capped: a session can create any number of tickets; only approvals count against the charter's
  children.
- The size of a child is the agent's own label: the size limit holds the label, not the amount of work.
- `orch section set` on the epic's own Requirements or Acceptance criteria suspends the epic (its text changed
  since you signed); a Plan on an epic is refused (an epic has no plan of its own).
- `orch permit list` shows every open request's command and reason to the session.

What a prefix rule matches is narrow on purpose, and that reaches orch's own commands too: a shell metacharacter
outside quotes, a line break, and an argument shape listed below match no prefix rule, and the command stops for a
card. Text inside correct quotes is plain text to the shell, so a title or `-m` text may hold `(`, `;`, `&`, `|`,
`<`, `>` or `#` (`orch task done T-2 T1 -m "Data (WWF, IUCN)"` matches `orch task done`); `$`, a backtick, a
backslash and `!` inside double quotes still refuse (the shell acts on them there), and inside single quotes
everything is plain (`-m 'costs $5'` matches). Multi-line text goes through files the agent writes with its file
tools (`orch new --requirements-file … --acceptance-file …`, `--body-file`), and those tools are denied in factory
sessions unless your permission mode lets file edits through (see "Permissions" above).

- An **exact** rule matches only the identical command text.
- A **prefix** rule matches only a single simple command: outside quotes no `;`, `&`, `|`, `<`, `>`, `(`, `)`,
  backtick, `$`, backslash, glob (`*`, `?`, `[`), brace, tilde, `#`, `^`, `!`, newline, and no `=` starting a word;
  inside double quotes no `$`, backtick, backslash or `!`; quotes that close, and no single-quoted piece right after
  another (`'a''b'`); and its first words (split as the shell would) equal the rule's. Compound commands, redirects, pipes and substitutions never match a prefix rule (a redirect defeats
  prefix rules in Claude's own matcher too); they can only match an exact rule. `npm run verify --quiet` matches
  `npm run verify`; `npm run verify > f` and `npm run verify; rm -rf x` do not. The split is tested against the real
  `sh`, `bash`, `zsh` and `zsh` with `extendedglob` and `rcquotes` on thousands of generated commands
  (`tests/test_dark_quoting.py`): whenever orch reads words from a command, those shells read the same words. That
  guarantee is for a plain shell configuration: a user's shell with other options that change quoting or globbing is
  not covered. Globs and braces outside quotes refuse because they expand (a file
  named `--exec=x` matched by `-*` would reach the program as that option).
- A prefix rule also never matches a command carrying one of these argument shapes, which make some programs run
  other code, read other configuration or write elsewhere. They are these shapes, not every argument that does so (see
  "A prefix rule trusts the repository" below): `--upload-pack`, `--receive-pack`, `--exec`, `--script-shell`,
  `--shell`, `--prefix`, `--userconfig`, `--globalconfig`, `--node-options`, `--require`, `--config`, `--eval`,
  `--workspace`, `--open-files-in-pager`, `--ext-diff`, `--textconv`, `--output`, `--file`, `--makefile`, `--rootdir`,
  `--confcutdir`, `--manifest-path`, `--to-command`, `--use-compress-program`, `--checkpoint-action` (also as
  `--flag=value`, in any case, with `_` for `-`, or abbreviated to three letters or more, as npm accepts); a word that
  starts with `SHELL=` or `MAKEFLAGS=`; and any single-dash word holding one of the letters `c e x C w f p o I O`
  (`-x`, `-xc`, `-Ofoo`, `-Ipath`, ...). Such a command can only match an exact rule (`pytest -x` needs one).
  One exception: `--file` passes when the program is `orch` itself (the bare word `orch`, or exactly the path the
  runner resolves `orch` to; never another path, which could name a script the agent wrote), because orch reads a bound session's files only
  inside the workspace (see "Files" above), so `orch task add X --file orchestrator/temporary/t.yaml` and `orch
  section set X Plan --file plan.md` match their baseline rules. The test suite proves a bound session is still
  refused a file outside the workspace through every file option orch has (`new --*-file`, `section set`, `state`,
  `task add`, `widget add`, `feedback add`, `artifact add`).
- **Broad rules are refused**: a prefix of fewer than two words; one whose program is not a plain name (a variable
  assignment such as `FOO=1`, an option); one whose program (by its last path part, any case) is a shell,
  interpreter, wrapper, editor, network or file-sweeping tool: `sh`, `bash`, `zsh`, `fish`, `dash`, `ksh`, `csh`,
  `tcsh`, `pwsh`, `busybox`, `env`, `sudo`, `su`, `doas`, `eval`, `exec`, `xargs`, `nohup`, `time`, `nice`, `timeout`,
  `watch`, `command`, `builtin`, `arch`, `xcrun`, `caffeinate`, `script`, `tmux`, `screen`, `osascript`, `open`,
  `launchctl`, `crontab`, `at`, `stdbuf`, `ionice`, `setsid`, `flock`, `unbuffer`, `parallel`, `expect`, `gdb`,
  `lldb`, `sqlite3`, `less`, `man`, every shell by name (`bash5`, `tcsh`, ...), `python` (and `python3.12`, `pythonw`,
  `python3.12-intel64`, `py`, `pypy3`, `ipython`), `node` (and `node18`, `nodejs`), `perl`, `ruby`, `php` (and their
  versions), `irb`, `julia`, `Rscript`, `swift`, `jshell`, `lua`, `tclsh`, `deno`, `bun`, `bunx`, `npx`, `uv`, `uvx`,
  `curl`, `wget`, `ssh`, `scp`, `sftp`, `ftp`, `telnet`, `nc`, `socat`, `rsync`, `docker`, `kubectl`, `find`/`gfind`,
  `awk`/`gawk`/`mawk`/`nawk`, `sed`/`gsed`, `tee`, `dd`, `vim`, `vi`, `nano`, `emacs` (a trailing `.exe` is ignored);
  `npm`, `pnpm` or `yarn` with `exec`, `x` or `dlx`; `cargo run`; `go run`; `gh` without a subcommand first, or with
  `api`, `alias`, `extension` or `secret`; `git` with an option before its subcommand, or with `push`, `reset`,
  `clean`, `fetch`, `pull`, `clone`, `rebase`, `bisect`, `submodule`, `ls-remote`, `archive`, `config`, `worktree`,
  `remote`, `grep`, `difftool`, `mergetool`, `filter-branch`, `daemon`, `instaweb`, `send-email`, `credential`, `p4`,
  `svn`, `update-ref`, `replace`, `gc`, `branch` or `checkout`; `rm` and `mv`; any of the argument shapes above;
  anything never grantable; anything outside printable ASCII.

**A prefix rule trusts the repository.** A prefix rule on a project runner (`npm run X`, `make X`, `pytest`, `python
script.py` as an exact rule) lets the agent run any code it can write into the repository: `package.json`, the
`Makefile`, `conftest.py` and the scripts they call are all agent-writable. orch's own commands are the exception
for files: whatever the rule, a factory session hands orch only files inside the workspace (see "The baseline"
above). Trailing arguments are passed through as
written, except the shapes above. Prefer exact rules, and for anything that matters, a wrapper script kept outside
the repository the agent writes to, listed by its exact command.

**In a Dark session** (one the runner bound to a Dark epic, as in phase 4):

- a never-grantable command, or a tool other than the shell, is denied as before; a never-grantable command is never
  allowed, whatever the profile holds;
- a command a rule matches is allowed; rules are standing, nothing is used up;
- a live grant for the exact command still allows, as before;
- anything else is denied with "not in the Dark profile", and a card (source `dark`) is filed for you: grant or deny
  it as usual, or add it to the profile (`--from-request`, only while its epic is an active Dark epic and Dark is on).
  Nothing is asked in the session. The denial tells the agent which request is open, that you can add it, to do other
  work or `orch wait`, and not to retry variants of the command or file another request for it.
- A rule that covers an open Dark card hides the card from your lists without signing an answer to it; removing the
  rule brings the card back. The same holds for a request of any source (one an agent filed itself, or a harness card
  from before Dark was on) while its epic is an active Dark epic, where the profile answers that command; in an
  ordinary factory epic a card stays a card whatever the profile lists.

**Requests the profile already covers.** In the live run agents filed `orch permit request` for commands the profile
already allowed (`orch task done …` after a variant of it was denied), then waited for an answer that never needed
giving. From a session the runner bound to a Dark epic, `orch permit request "<command>"` for a command the Dark profile
of the session's checkout allows files nothing: it prints "already allowed by the Dark profile: just run it" and
exits 0. Everywhere else it files a request as before.

**Waking.** Adding or removing a rule wakes the parked children (and a parked planner) of Dark epics (the runner relaunches them), the same
way your grants do, whether the Dark switch is on or off: a wake only relaunches a child, it allows nothing by itself.
Flipping the Dark switch alone wakes nothing, so switching it off and on does not relaunch every parked child; a child
parked while Dark was off waits for your answer to its card, or a profile change.

## Dark AI Factory on the dashboard (phase 5, dashboard)

Only while `factory.enabled` is on; every start is yours (the dashboard's cookie and same-origin checks, and orch's
refusal of a process under an agent harness, as for every approval).

- **New ticket** has a Mode choice: Ticket (as before), AI Factory, and Dark AI Factory while Dark is on (otherwise a
  line says how to turn it on in a terminal). A factory mode makes an epic whose Requirements are your ask, word for
  word, and whose Acceptance criteria are the "Done when" text; nothing else is written for you. Creating it is also the
  start: the same signed charter approval and runner arming as the epic page's start, behind the same inline confirm,
  which names the limits you sign. Dark needs the word dark typed. The server checks the switches, the typed word, and
  the text for what the start would refuse (a line that reads as a question still open for you, hidden or control
  characters) before anything is created, so a refused start creates nothing; it checks that the stored Requirements
  and Acceptance criteria are byte for byte what you sent before it signs. Each rendered form carries a one-time token,
  so sending the same form twice starts one run (the second send links to the epic the first one created). If the epic
  was created but its start failed, you land on the epic with the reason, and start it there.
- **A planner splits the epic.** An epic started from New ticket has no children; the runner starts one planner session
  for it (see "The planner" above), and the run view says "A planner session is splitting the epic into children"
  while it runs. Without one it says "Waiting for children" and why: the planner starts when a session slot is free;
  it ended without a child and no card of the epic is open (it starts once more only when you answer a card in the
  epic or change the Dark profile); or it ended twice without adding a child (then add the children yourself, or
  approve the epic again for a new start). An open card shows as "Needs you" instead. In a Dark epic the planner's
  orch commands stop for cards unless the Dark profile holds them: while the profile is empty, the run view and New
  ticket's Dark mode say so and name `orch dark profile add --baseline`.
- **Sessions cannot write files under a prompting permission mode.** A runner session's file-edit prompt is denied
  without a card (only shell commands are answered), and the launch command may not set a permission mode that skips
  prompts. So unless your user-scope Claude settings (`$CLAUDE_CONFIG_DIR/settings.json`, else
  `~/.claude/settings.json`) set `permissions.defaultMode` to `acceptEdits`, `auto` or `bypassPermissions`, no agent
  of the run can write a file and the planner cannot create children. `auto` does not count for a Haiku model (the
  launch command's `--model`, else the settings' `model`): Claude Code offers Haiku no auto mode, so its edits prompt
  (in the live run Claude's first-run offer had switched the mode to `auto`). Use `acceptEdits`. The run view and New ticket's factory modes say
  so; orch only reads that file, it never writes it.
- **Epic page**: a Start choice in the epic's approval: None, AI Factory, and Dark AI Factory while Dark is on (radios,
  as on New ticket). Dark shows the field for the word dark, which the server checks; the radio alone decides what is
  signed.
- **No permission prompts in a Dark session** does not mean nothing reaches you: a command outside the Dark profile is
  denied and becomes a card on the dashboard; you still answer cards, larger children and the verdict.
- **Run view** (`/factory/<epic>`): a ring of five steps, each lit only from records orch keeps: Understand (a current
  signed charter and at least one child), Plan (every child covered, auto-approved or approved), Build (every child
  has all its tasks closed, as the agents report it, or is in testing or done with the record behind it), Evidence (the
  Ready report: every criterion of every child in testing cites evidence), Done (the epic's signed verdict). The state,
  said once as a chip (a word or two) and a headline (the reason, never the chip again): working (a session runs on
  a child), planning (the planner runs; Understand still needs a child), waiting for a session slot (a child can start
  but no session runs on one yet: the runner's next round, or every slot of `factory.max_concurrency`, at most 3, is
  taken), needs you, waiting for children, idle, paused, stopped, budget used up, edited, blocked, not running (not
  armed) or finished. Motion only while it works (working or planning); a Dark run's working chip is mint, an AI
  Factory's blue. The Dark core glows stronger only with real build evidence: a task a child closed (a running session
  is not evidence). Then the time: "Running for ... since you signed the start" while it works (there is no estimate),
  "Started ... ago" otherwise, and for a finished run the duration once, in its summary; what waits for you (the same cards as
  elsewhere), a read-only log in plain words (time, ticket, who and a fixed phrase per event kind; no command text,
  hashes or session ids) and "Stop the run…", which is the epic's pause. A finished epic shows a summary from the
  records: children, tasks done, and permission requests answered on a card or added to the Dark profile after they
  were filed (commands the profile allowed directly leave no record and are not counted). A Dark charter while the Dark
  switch is off is shown, and runs, as an ordinary AI Factory.
- **Factories** (`/factory`, in the menu): every factory epic of the workspace; those that need you (cards, stopped,
  budget used up) first, then working, the rest, finished.
- **Add to the Dark profile** on a Dark card, while Dark is on and its epic is an active Dark epic: the card's exact
  command becomes an exact rule (`orch dark profile add --from-request`, the same checks), bound to the hash of the
  command the card showed. On a card whose command is not one plain command (it chains, pipes, redirects or
  substitutes) the button is replaced by a note: no prefix rule ever matches such a command and an exact rule only
  its identical text, so adding it would not help; Grant once (the primary button) or Deny. The terminal's
  `--from-request` still accepts it, for the rare command an agent repeats word for word.

The Dark switch itself stays a terminal command (`orch factory dark on`). Closing by itself happens only for a
charter you sign with `close` (below); otherwise the verdict is yours, from the Ready report. Not built: runner-side
proof that tests ran or a review happened; the ring has no
steps for those because no record of them exists. Merge, Dev and Production steps appear only for a charter that signs a
release up to them (phase 6, below).

What the test suite covers for the planner and the baseline, and what it does not: the runner, the binding, the hook,
the nudge, the readiness checks and the dashboard states are tested with a stand-in launcher and stand-in programs (no
tmux, no agent), the baselines and the built-in prompts against the CLI's real commands, real git and the guard, and
the prefix split against the real shells. No test runs a real Claude session through a planner or a child end to end.

**What the live run showed (5 October), and what it did not.** One real run, with Claude Haiku sessions the runner
started on a scratch workspace, went end to end: an epic from the New ticket page, a planner session that wrote two
children and auto-approved them, a work session per child that wrote files and a commit, and a child that reached
testing. It also showed the friction this round removes: the owner answered about 30 permission cards in 70 minutes,
nearly all for harmless commands. Haiku chained commands (`a && b`), piped and redirected them (`| head`, `2>&1`),
put parentheses in quoted `-m` text, used `--file`, needed `git add` and `git commit`, filed requests for commands the
profile already allowed and then waited forever, and stopped at its prompt after each answer until someone typed into
the pane. Its environment failed silently: no `uv` on the session PATH (so orch's hooks did nothing), a `claude`
wrapper that exited at once, the trust dialog, auto mode that Haiku cannot use, no orch skills at user scope, and a
child that published an artifact through a tool that never prompts. The fixes above (quote-aware matching, `--file`
for orch, the git-basic baseline, the built-in worker prompt, no requests for allowed commands, the idle nudge, the
readiness checks, the early-end notice, compound cards without the profile button, `prune`) are tested with stand-ins;
no second live run has proved them yet. What remains likely: a model that ignores the prompt still chains or pipes
commands, and each such command still stops for a card (by design: the matcher does not accept chains, pipes or
redirects); the nudge depends on Claude Code's current screen markers.

## Release recipe (phase 6)

A Dark epic can release its own work, up to a stage you sign at its start: **merge** (each child's branch), **dev**
(merge, then a deploy to your dev environment) or **prod** (merge, dev, then the recipe's production stage, never
before its release window opens; see "The production stage" below). Nothing is closed by the release: the verdict
stays yours. The runner (the dashboard you started) runs the stages; an agent cannot start, change or skip one.

**The recipe is yours, on this machine.** It lives in `factory-release.json` in the permits folder of your orch config
dir, next to `factory-command.json` and under the same guard: agents can neither read nor write it, it is never
grantable, and orch's `factory release` commands are human-only. It is not workspace config, ticket text or charter
text, because an agent can edit all of those, and these commands merge and deploy. One file holds the recipes of
several workspaces, keyed by workspace id (`{"workspaces": {"<id>": {"recipe": ..., "programs": ...}}}`). A file that
is damaged, not a regular file, not owned by you or writable by group or others counts as no recipe. In your own
terminal (each refused to agents and under an agent harness):

```bash
orch factory release set --file recipe.json   # validates, prints the recipe and the program pins, needs RELEASE typed
orch factory release show
orch factory release clear                    # needs CLEAR typed; no release runs until you set one again
orch factory release retry <epic> --stage merge|dev|production [--child <child>]   # one more attempt, needs the epic id typed
```

**Program pins.** `set` resolves every program of the recipe with your terminal's PATH and the runner's trust checks
(an absolute PATH entry; the file owned by you or root and not writable by group or others; not inside the workspace),
and stores its real path and sha256 with the recipe; the confirmation shows them. At run time each program must still
resolve, on the dashboard's PATH, to the same real path with the same content, or the stage does not start ("not the
one you pinned"): after an upgrade of `gh` or a script of yours, set the recipe again.

**Schema.**

```json
{"remote": "<owner>/<name>", "repo": "<owner>/<name>", "base": "main",
 "sensitive_paths": [".github", "**/*.lock"],
 "git_config": {"credential.helper": "<your helper>"},
 "stages": [{"name": "merge", "timeout": 600,
             "precheck": {"argv": ["<program>", "..."], "expect": "<text>"},
             "commands": [["<program>", "...", "{base}", "{branch}"], ["<program>", "...", "{sha}"]],
             "check": {"argv": ["<program>", "..."], "expect": "<exact trimmed stdout>"}},
            {"name": "dev", "timeout": 900,
             "commands": [["<program>", "..."]], "check": {"argv": ["<program>", "..."]}},
            {"name": "production", "timeout": 900, "window": {"min_hours_since_last": 20},
             "commands": [["<program>", "...", "{sha}"]], "check": {"argv": ["<program>", "..."], "expect": "<text>"},
             "rollback": {"commands": [["<program>", "..."]], "check": {"argv": ["<program>", "..."]}}}]}
```

- `remote` (required): where the base comes from and where the work goes: an `https://`, `ssh://` or `file:///` URL,
  `user@host:path`, an absolute path, or `owner/name` (GitHub over https). Never read from the workspace's
  `.git/config` or any workspace file. Transport helpers such as `ext::` are refused.
- `repo` (optional): `owner/name` for `gh --repo {repo}`; taken from `remote` when that is `owner/name`.
- `base`: the branch the work goes to (default `main`), fetched from `remote`.
- `git_config` (optional): only `credential.helper` and `core.sshCommand`, for the runner's own git calls (see below).
- `stages`: `merge`, then `dev`, then `production`, each at most once and in that order; any other name is refused.
  `production` needs a `dev` stage before it. `merge` runs once per child (`per` may only say `child`); `dev` and
  `production` run once per epic.
- `commands`: 1 to 10 argv lists of 1 to 64 printable ASCII words (at most 512 characters each). A shell string is
  refused, and so is a program that runs a string or another program (`sh`, `bash` and every shell, `env`, `sudo`,
  `xargs`, `nohup`, `timeout`, `nice`, `command`, `script`, `osascript`, `time`, `setsid`, ...), an interpreter given
  code as text (`python -c`, `node -e`, `perl -e`, ...), and git with an alias, `--config-env` or `--exec-path`.
- The **merge stage** must name `{sha}` in a command (so the merge is pinned to the commit the runner checked, for
  example `gh pr merge … --match-head-commit {sha}`), and `{base}` in a command (the one that opens the pull request:
  the recipe, not the agent, chooses where the work goes) or in its `precheck`.
- `precheck` (merge only, optional): runs before the stage's commands; the stage fails unless it exits 0 and, with
  `expect`, prints exactly that. Use it to refuse a branch whose open pull request targets another base.
- `check`: the stage is **proven** only when this command exits 0 and, with `expect`, its trimmed standard output is
  exactly that text (at most 1024 characters; placeholders are filled in). A stage without a check is refused.
- `timeout`: seconds per command, 1 to 1800 (default 600).
- `window` (production only, optional): `{"min_hours_since_last": N}`, a whole number of hours from 1 to 720
  (default 20). `rollback` (production only, optional): `{"commands": [...], "check": {"argv": [...], "expect":
  "..."}}`, both required, with the same rules as a stage's commands and check; its programs are pinned with the rest.
- Placeholders, in any word but the program, and in `expect`: `{epic}`, `{workspace}`, `{base}`, `{remote}`,
  `{repo}`, `{sha}` (in the merge stage the child's checked commit; in dev and production the base commit the stage
  runs on), and in the merge stage `{child}` and `{branch}`. Each value is checked before it is put in:
  ticket ids by their form, the workspace id and the commit as hex, branch and base by a strict git branch form (a
  letter or digit first, so never an option; no `..`, `//`, `/.`, `@{`, trailing `/`, `.` or `.lock`), remote and
  repo as the recipe validated them. Any other brace is refused.
- `sensitive_paths`: up to 100 patterns, matched case-insensitively (after Unicode normalisation). A pattern without a
  glob character is that file or folder and everything below it (`.github`, `deploy`); one with `*`, `?` or `[` is a
  glob over the whole path (`*` also crosses `/`, so `*.lock` matches `a/b/c.lock`); a leading `**/` matches at any
  depth, the top level included (`**/migrations/*` matches `migrations/1.sql` and `app/migrations/1.sql`, while
  `*/migrations/*` misses the top level). No leading or trailing `/`.

**What runs where.** The workspace checkout is agent-written, its `.git` included (config, hooks, refs, objects), so
the runner neither classifies nor runs anything there. It keeps a repository of its own in
`permits/release-repos/<workspace id>/repo` of your orch config dir (guarded like the rest of the permits folder),
created with `git init` without templates; its `.git/config` is written by the runner every time it is used: no
includes, aliases, hooks, fsmonitor, filters or remotes. Each release round:

1. fetches the base from the recipe's `remote` into a ref only the runner writes (`refs/remotes/release/<base>`);
2. fetches each child branch from the workspace checkout's path into the mirror (`+refs/heads/<branch>` only: just
   objects cross, and the commit is what counts from then on, never the workspace's refs or replace objects);
3. classifies there (below);
4. for the merge stage, checks out exactly the classified commit in the mirror's work tree (forced, cleaned) and runs
   the precheck, the commands and the check there, with the working directory in the mirror; before every one of
   those commands it fetches the branch again and stops if it no longer points at the classified commit;
5. for the dev stage, fetches the base again and checks out the remote base as it is then (the merged work, never
   the agent's working tree), and runs the commands there;
6. for the production stage, checks out exactly the commit dev was proven on (recorded in dev's outcome), not the
   remote base as it is later, and runs the commands there.

Every git call the runner makes is an argv list with `--no-replace-objects`, `-c core.hooksPath=/dev/null`,
`-c core.fsmonitor=false`, `-c core.attributesFile=/dev/null`, `-c core.sshCommand=ssh` and `-c credential.helper=`
(an empty helper list, then the recipe's `git_config` values when it names them).

**Your own git config is deliberately not used by the release.** Your global git config (`~/.gitconfig`,
`~/.config/git/*`) is a file of your user, so code an agent gets run as you could write into it, and a `-c` flag
cannot undo what it adds (a `url.*.insteadOf` that swaps the remote the base comes from, a smudge filter or an
attributes file that runs a program on checkout). So every executor git runs with `GIT_CONFIG_GLOBAL` pointing at an
empty file the runner writes before each use (`permits/release-repos/<workspace id>/git-global`), `GIT_CONFIG_NOSYSTEM=1`,
`GIT_ATTR_NOSYSTEM=1`, `HOME` set to an empty folder the runner owns there (`git-home`), no `XDG_CONFIG_HOME`, and no
`GIT_CONFIG_COUNT`/`KEY`/`VALUE` of yours, plus `GIT_NO_REPLACE_OBJECTS=1` and `GIT_TERMINAL_PROMPT=0` (no `GIT_DIR`,
`GIT_WORK_TREE` or the like). The recipe's commands get the same `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_NOSYSTEM` and
`GIT_ATTR_NOSYSTEM`, and the runner's own `GIT_CONFIG_COUNT=1` setting `core.attributesFile=/dev/null`, but keep the
allowlist's `HOME` and `XDG_CONFIG_HOME` (so `gh` finds its own login); git's default ignore file under them still
applies to a git they run. The consequence: your credential helper and ssh command reach the release only through the
recipe's `git_config` (`credential.helper`, `core.sshCommand`), for the runner's git; a `git` command in the recipe
names its own (`git -c credential.helper=<yours> push …`). A private https remote therefore needs them in the recipe.
`SSH_AUTH_SOCK` is not passed, so an ssh remote works only with a key ssh can use without an agent (an unencrypted
key, or one named in your ssh config); https with a credential helper is the simpler choice. Any mirror or git failure
stops the round (fail closed): nothing runs.

**The diff classification.** Before the first merge command of an epic, every child branch not yet merged is compared
with the remote base in the mirror: the net diff (`git diff <base>...<commit>`) and every commit the branch brings in
(`git log -m <base>..<commit>`, merges against each parent), both with `--no-renames` (a rename is both paths),
`--ignore-submodules=none` (a gitlink counts, whatever `.gitmodules` says), `--no-ext-diff` and `--no-textconv`. Any
match of `sensitive_paths` stops the release with "Sensitive path touched", naming the paths (escaped): nothing is
merged. Because every commit counts, a later commit that removes the change does not clear it: merge by hand, or
rewrite the branch without it, then Retry release on the merge stage. The runner takes the one branch a child names
(`orch link --branch`), else the branch of its one worktree; the name must be a valid branch name that names the child
(its id as a word) and is not the base. A child the runner cannot fetch or check fails its merge stage without a
command run.

What this guarantees: the paths a branch changes are judged from objects the runner fetched, against the base on the
remote, with no workspace config, hook, ref, replace object or rename detection in between; the merge stage runs on
exactly that commit and is pinned to it. What it cannot: the recipe is yours and runs whatever you wrote (a command
that merges something else merges something else); the check proves what it checks; the content of a commit that
touches no sensitive pattern is not judged; classification covers the child branches only, not commits someone else
pushed to the base; and the dev stage runs the base's own scripts (`make`, `package.json`) as they are on the remote
then: the children's merged work as classified, plus whatever else reached the base.

**Signing it into the charter.** `orch approve <epic> requirements --dark --release merge|dev|prod [--rollback]`, or
the "Release up to" choice of the dashboard's Dark start. The text you confirm says it "releases up to <stage> by itself
using the recipe on this machine", and for prod that it releases to production by itself using the recipe on this
machine, waits for the release window, and (with `--rollback`) runs the recipe's rollback when the production check
fails. It is refused without `--dark`, while no valid recipe exists for this workspace, when the recipe lacks a stage
up to the target (dev needs merge and dev; prod needs all three), and `--rollback` without `--release prod` or while
the recipe's production stage has no rollback, and prod when the recipe's production window is as long as or longer
than the charter's time budget (72 hours for a dashboard start; waiting for the window would use it up, so production
might never run; `orch factory release set` warns about such a window). The charter carries `release` and `rollback`
only when you sign them, so every charter signed before hashes exactly as before. The recipe in force when a stage runs
is the one used. On the dashboard, choosing Production also needs the word production typed (as well as dark); in the
terminal the typed confirmation of `orch approve` (the epic's id) covers the whole charter text, which names
production, the window and the rollback.

**When it runs.** For each armed Dark epic whose charter signs a release, when all of this holds, read fresh before
every command: the factory and Dark switched on, the ledger whole, the charter active (not paused, not edited, the
budget not used up), the epic Ready (every child in testing or done, every criterion cited, nothing unverifiable), no
open permission request of the epic, and no Stopped reason other than the release's own. A process under an agent
harness is refused. Releases run in their own round of the dashboard (every 15 seconds), apart from the session round.
Known limit: an epic that used exactly its child budget counts as Stopped ("Budget used up", as since phase 3), so it
does not release.

**The lock.** One release at a time per workspace: an exclusive file in the guarded folder names the dashboard process
(pid and start time), an expiry (the command's timeout plus two minutes, renewed before each command) and the process
group of the command that runs. It is held across all stages of one epic, and it holds while that dashboard lives and
its expiry has not passed, or while the recorded command's process group still runs: a dashboard that died does not
let a new one start another epic's release beside a running command. When the dashboard stops, each running release
command's process group gets SIGTERM and, five seconds later, SIGKILL; its attempt is recorded as failed ("the
dashboard stopped while it ran").

**Records and crash safety.** Each attempt of a stage for one unit (a child, or the epic) writes, in
`permits/release-records/`, an intent record (stage, unit, attempt, a hash of the commands, start time, the commit)
exclusively before the first command, and an outcome record (exit codes, the check's exit code, end time, proven or
not, the commit merged, for dev the children and commits it was proven for, and the last 4 KB of output, escaped)
after. Temporary files get random names, are created exclusively and never through a link. The output is kept only
there: never in events, tickets or logs. Each outcome adds an event `release.stage` with the stage, the unit, proven
and the exit code only. A proven stage never runs again by itself (records are kept per epic, so approving it again
does not merge twice). An intent without an outcome, and no live lock holder, means the runner stopped while a command
ran: the outcome is **unknown**, and that stage is never run again by itself. Each stage and unit gets one automatic
attempt; a failure stops the release there and leaves the others as they are. A pause, an edit, a used-up budget, the
factory or Dark switched off or a cut ledger stops the release before its next command (a running command finishes
first, and the attempt is recorded as failed).

These records, the outcomes and the lock rest on same-user trust, stated plainly: the guard keeps agents' tools and
commands away from the permits folder, and a Dark profile rule never matches a command naming it, but code an agent
can get run as you (a project runner allowed by a prefix rule, such as `npm run …` or `pytest`) can write there
anyway: forge a proven outcome, delete a failed intent so a stage runs again, or plant a lock.

**Out of date.** A merge stage records the commit it merged for each child, and dev the children and commits it was
proven for. If a child's branch in the workspace moves after its merge was proven (it came back with more work), or a
child is added after dev was proven, the stage shows as out of date, not proven, and the epic is Stopped with
"Release out of date". Retry release on that stage runs it again for the children as they are now (the merge first,
then dev).

**The production stage.** Production runs only for a charter that signs `--release prod`, after dev is proven and not
out of date (a changed merge or a new child makes dev, and with it production, out of date; production never runs
while dev is), on the commit dev was proven on, under the same lock, gate, intent-before-commands records and pinned
programs as the other stages. It gets one automatic attempt: a failed or unknown production attempt stops the release,
and only your Retry release allows one more. It checks out the base commit dev was proven on and fills `{sha}` with it;
that commit is what production releases only when your recipe's commands use `{sha}` (a script that deploys "the
latest main" deploys whatever main is then).

- *One unresolved production holds every other.* While any epic of the workspace has a production attempt that
  failed, whose outcome is unknown, or that is rolling back (a failed or unknown rollback included), no other epic's
  production starts: its run view says "Production is held: another epic's production is unresolved" and names that
  epic. Retry release on that epic's production (after checking production by hand) resolves it; its own production then
  waits for its window and its gate like any other.

- *The release window.* Production never starts before `min_hours_since_last` hours (default 20) have passed since
  the last production attempt of this workspace began or ended, whatever came of it (a failed or rolled-back attempt
  touched production too). The runner trusts only its own records of that time: a file in the guarded release records
  (`permits/release-records/production-last-<key>.json`), written right after the attempt's intent record and before
  its first command, and every epic's production intent and outcome records, so deleting that file does not open the
  window while the attempts' records remain. Never an agent, a ticket or the recipe. No record means the window is open;
  a record it cannot read, or whose time lies in the future, keeps the window shut until you look at it. While the
  window is shut the epic is **waiting**, not Stopped: nothing runs, the run view says when the window opens and when
  the charter's time budget ends (a wait uses it; if the window opens after the budget ends, production does not run
  under that charter), and the runner checks again every round. A retry waits for the window too: it is never
  skipped.
- *The rollback.* With `rollback` signed in the charter (`--rollback`, or the dashboard's "Roll back production by
  itself if its check fails") and a rollback in the recipe, the runner runs the recipe's rollback commands and then
  their check once, right after a production attempt whose commands all exited 0 and whose live check did not pass
  (exit code, output or timeout). It never runs after a failed production command, a stop, a pause or the dashboard
  stopping, never for another stage, and never again by itself. It has its own intent and outcome records beside the
  production attempt's; the outcome says "rolled back" when its check passed, else "rollback failed", and a rollback
  that started and has no outcome counts as failed. A charter that signs a rollback while the recipe no longer has one
  does not start production at all. What a rollback does is what your recipe says; its check proves only what it
  checks.

**Stopped reasons** (the run view, Today and the Board, as for the earlier reasons):

- *Sensitive path touched*: look at the named paths; merge by hand, or rewrite the branch without the change, then
  Retry release on the merge stage: the branches are checked again.
- *Release stage failed* (with the stage and exit code or reason): read the output on the run view, fix the cause,
  then Retry release for that stage: it runs once more.
- *Release outcome unknown*: check by hand whether the stage's commands ran (did the branch merge, did dev deploy).
  Retry release runs it once more, so retry only when running it again is safe; otherwise finish it by hand.
- *Release out of date*: children changed after the release stage was proven; Retry release on that stage, or
  release the change by hand.
- *Production check failed*: production's commands ran and its live check did not pass, and nothing was rolled back
  (none signed, or none in the recipe). Look at production now; roll back or fix it by hand.
- *Production rolled back*: the live check failed and the signed rollback ran and its check passed. Fix the cause,
  then Retry release on production (after its window).
- *Rollback failed*: the rollback's check did not pass, or its outcome is unknown: production may be broken. Fix it by
  hand.
- *Release could not start*: the runner could not start a stage for a reason it cannot get past by itself (a program
  that is not the one you pinned, the recipe cleared or lacking a stage the charter signs, the base not fetchable, the
  commit not checkable out); nothing ran. Fix the cause, then Retry release: the runner tries again in its next round.
- *Signed rollback missing*: the charter signs a rollback but the recipe's production stage has none now; production
  did not run. Put the rollback back into the recipe and Retry release, or approve the epic again without it.

**Retry release** is yours: the run view's button (inline confirm) or `orch factory release retry`. It allows exactly
one more attempt of one failed, unknown or out-of-date stage (or, after a sensitive-path stop, a fresh check of the
branches) and runs nothing itself; the runner's next round does.

**On the dashboard.** A Dark start (New ticket's Dark mode and the epic page's Start) has a "Release up to" choice:
Nothing (the default), Merge, Dev or Production. Each is disabled, with a line naming
`orch factory release set --file recipe.json`, while this workspace has no valid recipe with the stages it needs
(Production: merge, dev and production). Production shows a checkbox "Roll back production by itself if its check
fails" (disabled while the recipe's production stage has no rollback) and a field for the word production, which the
server requires in addition to dark. The server checks all of it again before anything is created or signed, and the
confirm says what will run. For a charter that signs a release, the run view's ring gets Merge (and Dev, and
Production, whose step is lit by its live check) after Evidence, lit only from proven stage records (a failed, unknown,
out-of-date or merely running stage lights nothing); Done stays your verdict. A Release panel shows each stage and unit
as waiting, running, proven, failed, outcome unknown or out of date, the rollback's state, the escaped output tails
under a disclosure, and Retry release where it applies. While a stage runs the state reads "Releasing"; while
production waits for its window it reads "Release window", with the time it opens.

**Example** (an example only: the owner/name, the script paths and what they do are placeholders for your own; no
secrets belong in the recipe). It pushes the checked commit to the remote, opens a pull request against the recipe's
base, merges it only at that commit, and checks that the merged pull request went to that base at that commit; then it
deploys dev through scripts of yours kept outside the repository, and (for a charter that signs prod) production at the
commit dev was proven on, checked by the version production reports, with a rollback script for a failed check:

```json
{"remote": "your-org/your-app", "base": "main",
 "git_config": {"credential.helper": "<the helper your global git config names>"},
 "sensitive_paths": [".github", "deploy", "**/*.lock", "**/migrations/*"],
 "stages": [
  {"name": "merge", "timeout": 600,
   "precheck": {"argv": ["gh", "pr", "list", "--repo", "{repo}", "--head", "{branch}", "--state", "open",
                         "--json", "baseRefName", "--jq", "map(select(.baseRefName != \"{base}\")) | length"],
                "expect": "0"},
   "commands": [["git", "push", "--force", "{remote}", "{sha}:refs/heads/{branch}"],
                ["gh", "pr", "create", "--repo", "{repo}", "--base", "{base}", "--head", "{branch}", "--fill"],
                ["gh", "pr", "merge", "{branch}", "--repo", "{repo}", "--squash", "--match-head-commit", "{sha}"]],
   "check": {"argv": ["gh", "pr", "view", "{branch}", "--repo", "{repo}", "--json", "baseRefName,headRefOid,state",
                      "--jq", "[.baseRefName, .headRefOid, .state] | join(\" \")"],
             "expect": "{base} {sha} MERGED"}},
  {"name": "dev", "timeout": 1200,
   "commands": [["/Users/you/bin/deploy-dev", "--epic", "{epic}"]],
   "check": {"argv": ["/Users/you/bin/dev-health"], "expect": "ok"}},
  {"name": "production", "timeout": 1200, "window": {"min_hours_since_last": 20},
   "commands": [["/Users/you/bin/deploy-prod", "--commit", "{sha}"]],
   "check": {"argv": ["/Users/you/bin/prod-version"], "expect": "{sha}"},
   "rollback": {"commands": [["/Users/you/bin/rollback-prod"]],
                "check": {"argv": ["/Users/you/bin/prod-health"], "expect": "ok"}}}]}
```

(`gh pr create` fails when a pull request for the branch exists already; make it a script of yours that tolerates that
if your flow reuses branches.) The recipe's commands get only the runner's minimal environment (the allowlist of the
agent sessions, `HOME` among it, and a PATH of the pinned programs' folders and the system's), the mirror's work tree as
working directory, and no standard input. A program that needs a credential reads it from your own config (for
example `gh` from its config under `HOME`), never from the recipe.

**Not built.** A notification when a release stops, a rollback
for anything but a failed production check, and runner-side proof that tests ran or a review happened. The test suite covers the recipe, the CLI, the guard, the
charter and the release step with real git in temporary repositories (a workspace and a bare remote on disk, no
network) and a stand-in for the recipe's commands: no test runs a real `gh`, push, merge or deploy.

## Closing by itself (opt-in)

By default the verdict on a factory epic is yours (D1). A **Dark** charter may sign `close`: then the runner gives the
epic's done verdict by itself, and closes its children, once everything is proven. **This replaces your verdict for
that run.** Reopen stays yours. An AI Factory that is not Dark never closes by itself.

```bash
orch approve <epic> requirements --dark --close [--release merge|dev|prod [--rollback]]
```

On the dashboard, a Dark start has a checkbox "Close the epic by itself when everything is proven", off by default and
shown only with Dark; the word dark is still required, and the confirm says that it replaces your verdict for this run
and that Reopen is available. `close` is refused without `--dark`, and the charter carries it only when you sign it
(every charter signed before hashes as before).

**When it closes.** The runner (the dashboard you started, in its release round, never a process under an agent
harness) closes the epic only when all of this holds, read again under the workspace's release lock right before it
acts: the factory and Dark switched on, the ledger whole, the epic open, its charter live (not paused, not edited, the
budget not used up), Dark, started from the dashboard and signed with `close`; the Ready report holds (every child in
testing or done, every criterion of every child in testing cites evidence, every status backed by orch's records);
every release stage the charter signs is proven by its check and not out of date, and no sensitive path stopped it;
no permission card of the epic is open; the epic has no Stopped reason; and the coverage check holds (when this orch
has one: until then there is no coverage condition). If any condition fails it does not close, and the run view and the
Ready card say which: "Not closed by itself: ..." for a condition only you can change (then Accept is offered as for
any epic), or "It closes by itself, in place of your verdict, when: ..." with the conditions the runner still gets
past by itself (a release stage to run), without an Accept button.

**How it closes.** Through the same verdict the Ready report's Accept gives (`orch verdict <epic> done`): it closes
the children and the epic, bound to the hash of exactly the evidence the Ready report showed, and is refused if any of
it changed. It is signed into the ledger as your dashboard's human actor with `via` "dark-charter", and the events say
the same (plus an event `verdict.auto` naming the children and the hash), so the ledger, `orch check` (an info
finding "charter-verdict": a decision you delegated in that charter, not one you gave) and the run view ("Closed by
itself under your charter", with the time, a summary of what was proven and a Reopen button) all show it was the
charter's. An entry marked so under a charter that does not sign `close` is a warning in `orch check`.

**Once.** Before it acts the runner creates an intent record for that charter exclusively (in the guarded release
records, `close.<delegation>.intent`), then an outcome record. So it closes at most once per charter: never again after
you reopen it, and a crash between the intent and the verdict leaves the verdict to you. Approving the epic again
signs a new charter.

**What it cannot tell.** The evidence is what the agents wrote, and a release check proves what it checks; closing by
itself trusts both, as your Accept would have. Same-user trust holds as for the other release records: code an agent
gets run as you could write the records.

A safe local live test of the release and the close, with a bare repository as the remote and example scripts that
record what they were told: [factory-release-live-test.md](factory-release-live-test.md).

## Coming in later phases

- A notification when a release stops; runner-side proof of tests and review (and ring steps for them).
- A second live end-to-end run after this round's fixes, and a repeatable one per release.
- The Dark switch on the dashboard.
- A signed `factory.enabled` switch (today a plain config value).
- Phone cards through the signed phone-decision flow.
- Runner status on the epic page, and a runner limit signed into the charter.
- `factory.ask`: actions the harness would allow that you still want asked.
