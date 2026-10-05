# AI Factory (phases 1 to 6)

One epic in, finished work out: you write an epic and start it as a factory, and agents split, specify,
auto-approve and build its children. You hear from them when they need a permission they do not hold, and at the
end for the verdict. Issue #2 tracks the whole feature; this page describes what phases 1 to 6 ship.

AI Factory is **off by default**. Phase 1 works from the terminal; phase 2 adds the dashboard surface, phase 3 the
Ready report and the Stopped message, phase 4 the runner that keeps the agents going, and phase 5 the core of Dark AI
Factory (no permission prompts while it runs) with its dashboard start, run view and factory list, and phase 6 the
release recipe (merge and dev stages the runner runs by itself for a Dark epic that signs them), all described below.

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
- A missing permission is a request, not a question: `orch permit request "<command>" --ticket <id> --reason ...`.
  It signs nothing and grants nothing.
- The verdict stays yours (D1): children go to testing as usual, and you sign the epic's verdict.

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
  does not prompt for file edits (accept edits, or auto mode).

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
path only when owned by you or root and not writable by group or others. The agent gets `env -i` with a fixed PATH (the
folders of those programs, then the system's) and a short allowlist of variables, nothing else the dashboard holds. A
session starts in the child's worktree only when the child names exactly one, below the workspace's
`.claude/worktrees` folder (where `orch worktree add` puts it) or a git worktree in the workspace whose branch names
the child; otherwise in the workspace root. Residual risk, stated plainly: the operating system does not isolate processes of the same user from each other,
so the guard and these checks are best effort against an agent that tries; they close the obvious routes.

The guard's part is a text check, not a shell. It refuses a command run from inside the orch config dir, a `cd` or
`pushd` that lands in it (resolved with variables, `..` and globs, step by step), a listing of it (`ls`, `find`, `du`,
`stat`, `tree`, also recursive over a folder above it), and any tmux or screen command it cannot show plain: a socket
must be an absolute, literal path with no `..`, outside the config dir (relative ones are refused, because the
working directory is not known to a later command). The same resolution rules cover the file tools (a relative path is
taken from the hook's working directory) and every segment of a command. A path with a symlink component that leads into
the config dir is refused as written, never trusted because of where it points today. The rules are bounded (command
length, glob matches, path depth, time): hitting a bound, or an error inside these rules, is a deny (an unrelated internal error in the guard makes the
hook refuse the command and log, fail-closed). Only a tmux or screen command word and its own arguments are judged: a `grep tmux`,
a heredoc body or quoted text is not. The rule against reading the config dir wholesale (a recursive reader, archiver
or glob next to the config dir or a folder above it) likewise skips text that is only data: a heredoc body fed to
`cat`, `tee`, `gh … --body-file -` or `git commit -F -` when nothing in the line runs code, and the quoted message of
`git commit -m` or title and body of a `gh` create, edit or comment (a double-quoted one with `$` or a backtick stays
judged). A heredoc or string that a shell or interpreter runs is code, its quoted strings included, and stays judged; the
name `remote-humans.json` counts anywhere. A `cd` the guard cannot work out (a substitution, a variable, `CDPATH`) is allowed
(unless its target names orch's own environment or config place, such as `ORCH_STATE_DIR`, `XDG_CONFIG_HOME`, `.config/orch`,
or an obfuscated lookup that also names orch or config (the bare words orch, env and printenv do not count): that is judged as a cd into the config dir). A command that names the config
location and tmux or screen anywhere in its text, interpreter strings included, is refused. Otherwise
the working directory is then unknown for the rest of the line: a relative word that is, or can stand for, a name
in the config dir (permits, sessions, ledger*, tmux, ...) is refused with its own message, and so is a bare `*` handed
to a command that reads or lists; `rm -rf node_modules/*` and `for f in *.md` pass. The socket sits in a random-named
folder whose name is kept in a permits file; that only stops guessing and listing. The path is visible in `ps` to
processes of the same user, and the guard stops an agent naming it, best effort. Known limits of a text guard, not built: a word written without a mention of tmux or screen by
concatenation that uses none of the characters it looks for, a string built in another language (perl, osascript,
python), and a script file written and then run.

**A repository's own files.** A checkout's `.git` (its config, refs, packed-refs, info, objects, HEAD, worktrees and
hooks) and a worktree's `.git` pointer file decide what later git commands run: hooksPath, fsmonitor, aliases, refs.
Agents' file tools (Write, Edit, MultiEdit, NotebookEdit) never write a path with a `.git` component, in any case, as
written (a relative path taken from the hook's working directory) or after symlinks; reading stays open. In the shell
the guard denies a write into those `.git` files (a redirect, `tee`, `cp`, `mv`, `sed -i`, ...) and `git config` of a
key that runs a program (fsmonitor, sshCommand, pager, editor, askpass, aliases, includes, filters, diff and merge
drivers, credential helpers, gpg programs), as it already did for hooksPath; all of these are never grantable. The
same text-check limits apply (a script written and then run is not seen); the release step does not depend on this:
it never uses the workspace's `.git` for anything but fetching objects.

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
Identical means a copy with the same bytes, or a symlink to the workspace's own file itself (what `orch worktree add`
makes); a symlink to any other file is refused, even one with the same bytes.
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
the charter signs: nothing, unless you add `--release merge|dev` (phase 6, below); it closes nothing, the verdict stays
yours. The
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
orch dark profile remove <rule id>
```

Adding and removing are yours: each prints the rule (or the card's command) and needs its id typed (`--baseline`
prints every rule it adds and needs BASELINE typed); agents are refused in orch itself and by the guard.

**The baseline** is a fixed list of prefix rules for the orch commands an unattended planner or worker session runs:
`orch show`, `list`, `search`, `next`, `state`, `check`, `new`, `section set`, `task add|start|done|skip|block|list`,
`claim`, `release`, `log`, `link`, `move`, `wait`, `permit request`, `permit list`, `artifact add`, `epic show` and
`epic auto-approve`. No human-only verb is in it (approve, answer, verdict, request-changes, reopen, close, ledger,
`epic pause`, `permit grant|deny|revoke`, `dark profile add|remove`, `factory dark on`, addon administration, the
dashboard) and no `ask`. Each rule goes through the same checks as any `--prefix` rule; rules already in force are
skipped, so running it again adds nothing. A human-only form of an allowed verb (`orch move X done`) still does not
run: the guard denies it, so no rule matches it. Without the baseline (an empty profile), every command a Dark session
runs stops for a card; the run view and the New ticket page say so. If a rule cannot be signed, the command lists
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
anywhere in the command, even inside quotes (a title or `-m` text holding `(`, `$` or `;`), or a line break, and an
argument shape listed below (`--file` among them, so `orch section set X Plan --file plan.md` and `orch state X
--file f`) match no prefix rule, and the command stops for a card. Multi-line text goes through files the agent writes
with its file tools (`orch new --requirements-file … --acceptance-file …`, `--body-file`), and those tools are denied
in factory sessions unless your permission mode lets file edits through (see "Permissions" above).

- An **exact** rule matches only the identical command text.
- A **prefix** rule matches only a single simple command: no `;`, `&`, `|`, `<`, `>`, `(`, `)`, backtick, `$`,
  backslash or newline anywhere, and its first words (split as the shell would) equal the rule's. Compound commands,
  redirects, pipes and substitutions never match a prefix rule (a redirect defeats prefix rules in Claude's own
  matcher too); they can only match an exact rule. `npm run verify --quiet` matches `npm run verify`;
  `npm run verify > f` and `npm run verify; rm -rf x` do not.
- A prefix rule also never matches a command carrying one of these argument shapes, which make some programs run
  other code, read other configuration or write elsewhere. They are these shapes, not every argument that does so (see
  "A prefix rule trusts the repository" below): `--upload-pack`, `--receive-pack`, `--exec`, `--script-shell`,
  `--shell`, `--prefix`, `--userconfig`, `--globalconfig`, `--node-options`, `--require`, `--config`, `--eval`,
  `--workspace`, `--open-files-in-pager`, `--ext-diff`, `--textconv`, `--output`, `--file`, `--makefile`, `--rootdir`,
  `--confcutdir`, `--manifest-path`, `--to-command`, `--use-compress-program`, `--checkpoint-action` (also as
  `--flag=value`, in any case, with `_` for `-`, or abbreviated to three letters or more, as npm accepts); a word that
  starts with `SHELL=` or `MAKEFLAGS=`; and any single-dash word holding one of the letters `c e x C w f p o I O`
  (`-x`, `-xc`, `-Ofoo`, `-Ipath`, ...). Such a command can only match an exact rule (`pytest -x` needs one).
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
  Nothing is asked in the session.
- A rule that covers an open Dark card hides the card from your lists without signing an answer to it; removing the
  rule brings the card back.

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
  of the run can write a file and the planner cannot create children. The run view and New ticket's factory modes say
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
  command the card showed.

The Dark switch itself stays a terminal command (`orch factory dark on`). Not built: any automatic closing (the
verdict is yours, from the Ready report), and runner-side proof that tests ran or a review happened; the ring has no
steps for those because no record of them exists. Merge and Dev steps appear only for a charter that signs a release
(phase 6, below).

What the test suite covers for the planner and the baseline, and what it does not: the runner, the binding, the hook
and the dashboard states are tested with a stand-in launcher (no tmux, no agent), and the baseline against the CLI's
real commands and the guard. No test runs a real Claude session through a planner or a child end to end.

## Release recipe (phase 6)

A Dark epic can release its own work, up to a stage you sign at its start: **merge** (each child's branch) or **dev**
(merge, then a deploy to your dev environment). Nothing releases to production, and nothing is closed: the verdict
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
orch factory release retry <epic> --stage merge|dev [--child <child>]   # one more attempt, needs the epic id typed
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
             "commands": [["<program>", "..."]], "check": {"argv": ["<program>", "..."]}}]}
```

- `remote` (required): where the base comes from and where the work goes: an `https://`, `ssh://` or `file:///` URL,
  `user@host:path`, an absolute path, or `owner/name` (GitHub over https). Never read from the workspace's
  `.git/config` or any workspace file. Transport helpers such as `ext::` are refused.
- `repo` (optional): `owner/name` for `gh --repo {repo}`; taken from `remote` when that is `owner/name`.
- `base`: the branch the work goes to (default `main`), fetched from `remote`.
- `git_config` (optional): only `credential.helper` and `core.sshCommand`, for the runner's own git calls (see below).
- `stages`: `merge`, then `dev`, each at most once and in that order. `production` or any other name is refused
  ("not built yet"). `merge` runs once per child (`per` may only say `child`); `dev` runs once per epic.
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
- Placeholders, in any word but the program, and in `expect`: `{epic}`, `{workspace}`, `{base}`, `{remote}`,
  `{repo}`, and in the merge stage `{child}`, `{branch}` and `{sha}`. Each value is checked before it is put in:
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
   the agent's working tree), and runs the commands there.

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
touches no sensitive pattern is not judged; and the dev stage runs the base's own scripts (`make`, `package.json`),
which are the merged, classified ones.

**Signing it into the charter.** `orch approve <epic> requirements --dark --release merge|dev`, or the "Release up to"
choice of the dashboard's Dark start. The text you confirm says it "releases up to <stage> by itself using the recipe
on this machine". It is refused without `--dark`, while no valid recipe exists for this workspace, or when the recipe
lacks a stage up to the target (dev needs merge and dev). The charter carries `release` only when you sign one, so
every charter signed before hashes exactly as before. The recipe in force when a stage runs is the one used.

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

**Stopped reasons** (the run view, Today and the Board, as for the earlier reasons):

- *Sensitive path touched*: look at the named paths; merge by hand, or rewrite the branch without the change, then
  Retry release on the merge stage: the branches are checked again.
- *Release stage failed* (with the stage and exit code or reason): read the output on the run view, fix the cause,
  then Retry release for that stage: it runs once more.
- *Release outcome unknown*: check by hand whether the stage's commands ran (did the branch merge, did dev deploy).
  Retry release runs it once more, so retry only when running it again is safe; otherwise finish it by hand.
- *Release out of date*: children changed after the release stage was proven; Retry release on that stage, or
  release the change by hand.

**Retry release** is yours: the run view's button (inline confirm) or `orch factory release retry`. It allows exactly
one more attempt of one failed, unknown or out-of-date stage (or, after a sensitive-path stop, a fresh check of the
branches) and runs nothing itself; the runner's next round does.

**On the dashboard.** A Dark start (New ticket's Dark mode and the epic page's Start) has a "Release up to" choice:
Nothing (the default), Merge or Dev. Merge and Dev are disabled, with a line naming
`orch factory release set --file recipe.json`, while this workspace has no valid recipe with those stages; the server
checks it again before anything is created or signed, and the confirm says what will run. For a charter that signs a
release, the run view's ring gets Merge (and Dev) after Evidence, lit only from proven stage records (a failed,
unknown, out-of-date or merely running stage lights nothing); Done stays your verdict. A Release panel shows each stage
and unit as waiting, running, proven, failed, outcome unknown or out of date, the escaped output tail under a
disclosure, and Retry release where it applies. While a stage runs the state reads "Releasing".

**Example** (an example only: the owner/name, the script paths and what they do are placeholders for your own; no
secrets belong in the recipe). It pushes the checked commit to the remote, opens a pull request against the recipe's
base, merges it only at that commit, and checks that the merged pull request went to that base at that commit; then it
deploys dev through scripts of yours kept outside the repository:

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
   "check": {"argv": ["/Users/you/bin/dev-health"], "expect": "ok"}}]}
```

(`gh pr create` fails when a pull request for the branch exists already; make it a script of yours that tolerates that
if your flow reuses branches.) The recipe's commands get only the runner's minimal environment (the allowlist of the
agent sessions, `HOME` among it, and a PATH of the pinned programs' folders and the system's), the mirror's work tree as
working directory, and no standard input. A program that needs a credential reads it from your own config (for
example `gh` from its config under `HOME`), never from the recipe.

**Not built.** A production stage, any automatic closing (the verdict stays yours), release windows, rollback, and
runner-side proof that tests ran or a review happened. The test suite covers the recipe, the CLI, the guard, the
charter and the release step with real git in temporary repositories (a workspace and a bare remote on disk, no
network) and a stand-in for the recipe's commands: no test runs a real `gh`, push, merge or deploy.

## Coming in later phases

- A production stage; closing children under the charter (after a live test); release windows and rollback;
  runner-side proof of tests and review (and ring steps for them).
- A live end-to-end test of a factory run, per child, with a real agent session.
- The Dark switch on the dashboard.
- A signed `factory.enabled` switch (today a plain config value).
- Phone cards through the signed phone-decision flow.
- Runner status on the epic page, and a runner limit signed into the charter.
- `factory.ask`: actions the harness would allow that you still want asked.
