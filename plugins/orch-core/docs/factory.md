# AI Factory (phases 1 to 4)

One epic in, finished work out: you write an epic and start it as a factory, and agents split, specify,
auto-approve and build its children. You hear from them when they need a permission they do not hold, and at the
end for the verdict. Issue #2 tracks the whole feature; this page describes what phases 1 to 4 ship.

AI Factory is **off by default**. Phase 1 works from the terminal; phase 2 adds the dashboard surface, phase 3 the
Ready report and the Stopped message, and phase 4 the runner that keeps the agents going, all described below.

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

The runner never approves, grants, signs or starts a factory by itself. It does nothing unless `factory.enabled` is on,
the epic's signed charter is a factory one and still active, and you started it from the dashboard (the terminal's
`orch approve --factory` signs the charter but does not arm the runner). It runs only in a process that is not under an
agent harness, like the dashboard's other human actions.

**Turn orch on at user scope first.** The launched session ignores project settings (see below), so orch's guard and
permission hook must come from your user-scope Claude settings (`CLAUDE_CONFIG_DIR`, else `~/.claude`): enable the
orch-core plugin there, or carry the orch guard and permission hooks (commands whose program is `orch` or an absolute
path ending in `/orch`), and `disableAllHooks` must not be on. Until that holds, the runner starts nothing, stops
what runs, and the epic page says why.

**Session binding.** At launch the runner generates the session id, records session -> (epic, delegation, child)
exclusively in the guarded permits folder of your orch config dir, and only then starts the agent under that id. The
permission hook trusts only this record to decide which epic's grants apply, and only for a process running under the
process the runner recorded for that session (same pid and same start time): a copied session id gets nothing
elsewhere. An ended or stopped session loses the record at once. Only a human process writes it: agent processes are
refused, and the guard keeps agents away from the folder. Session ids are random, never written to events, tickets,
logs or pages.

**Where and how a session runs.** The runner's tmux server sits on a socket inside the guarded permits folder (a
private folder), not on the Terminals' socket, so these sessions are not in Mission Control's Terminals page. The
programs it starts (`tmux`, `env`, `claude`) are looked up on the dashboard's absolute PATH entries and used by absolute
path only when owned by you or root and not writable by group or others. The agent gets `env -i` with a fixed PATH (the
folders of those programs, then the system's) and a short allowlist of variables, nothing else the dashboard holds. A
session starts in the child's worktree only when the child names exactly one, below the workspace's
`.claude/worktrees` folder or a git worktree in the workspace whose branch names the child; otherwise in the workspace
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

## Coming in later phases

- The factory switch on the new-epic form, and phone cards through the signed phone-decision flow.
- Runner status on the epic page, and a runner limit signed into the charter.
- `factory.ask`: actions the harness would allow that you still want asked.
