# Live test: Dark AI Factory releasing to production and closing by itself

A safe, local way to watch the whole Dark run end to end on your own machine: merge, dev, production (after its
release window), the signed rollback, and the opt-in close. Nothing reaches the network or GitHub: the "remote" is a
bare git repository on disk, and "dev" and "production" are files that a small example script writes. The test suite
runs the same recipe and script for real against temporary repositories (`tests/test_factory_live_rehearsal.py`).

What you need: orch with the dashboard installed as a tool of your user, Claude Code with orch on at user scope (see
"Turn orch on at user scope first" in [factory.md](factory.md)), git and tmux.

## The pieces (examples only)

- [`examples/release-step`](examples/release-step): a POSIX sh script. Every call is appended to
  `state/release.log` beside the folder it lives in. `merge` merges the checked commit into the base and pushes it to
  the bare remote (it refuses a commit that is on the base already) and notes it in `state/merged`; `merged` is the
  merge check (the commit is noted there and is on the base); `deploy dev|production <sha>` writes the commit to
  `state/<env>.version`; `version` prints it (or `broken` while `state/fail-<env>-check` exists); `rollback` writes
  `rolled-back`; `health` prints `ok` (or `down` while `state/fail-rollback` exists).
- [`examples/factory-release-live-test.json`](examples/factory-release-live-test.json): the recipe. Merge, dev and
  production stages that call the script, a production window of **1 hour** (so you can watch the window without
  waiting 20), and a rollback. No secrets, no hosts. Its `remote` is a placeholder: you change it below.

## Set up (once, in your own terminal)

```bash
mkdir -p ~/orch-live-test/bin ~/orch-live-test/state
cp <orch-core>/plugins/orch-core/docs/examples/release-step ~/orch-live-test/bin/
chmod 755 ~/orch-live-test/bin/release-step
export PATH="$HOME/orch-live-test/bin:$PATH"     # in the terminal that starts the dashboard, too

# a scratch workspace (never a real project) and its bare "remote"
mkdir ~/orch-live-test/ws && cd ~/orch-live-test/ws
git init -b main && orch init          # then commit what orch init wrote
git add -A && git commit -m "start"
git clone --bare . ~/orch-live-test/remote.git
```

Copy the example recipe, set its `remote` to the absolute path of `~/orch-live-test/remote.git` (written out, for
example `/Users/<you>/orch-live-test/remote.git`), then:

```bash
orch factory release set --file recipe.json   # shows the recipe and the pinned script; type RELEASE
```

Turn the factory on, and let the workers commit in their clones: in `orchestrator/config.json` set
`factory.enabled` to `true` and `git.agent_may.commit` to `true` (`"git": {"agent_may": {"commit": true}}`). Commit
that change and push it (`git push ~/orch-live-test/remote.git main`): each child's clone starts from `main` as the
bare remote has it, never from your local `main`, so a commit you have not pushed is not in any child (and a local
`main` that shares no history with the remote's gets no clone at all, with "share no history" in the run view). Then

```bash
orch factory dark on                            # type the confirmation
orch dark profile add --baseline                # orch's agent verbs
orch dark profile add --baseline git-basic      # so workers can commit on their branches
```

Each child runs in a clone of its own outside the workspace, below `<your orch config dir>-clones` (for
`~/.config/orch` that is `~/.config/orch-clones`). Claude Code asks "Is this a project you trust?" once per clone
folder: trusting `~/.config/orch-clones` itself does not carry over (the first live run showed it). A child's session
waits at that question, and the run view says so ("<child> waits at Claude's folder-trust question ..."); the runner
never answers it. Answer it in the session's pane, or, once the runner made the clones, run `orch factory clones
trust` in your terminal and add the entries it prints under `projects` in Claude's `.claude.json` (with Claude
closed): that helps a session started after it, while a session already at the question still needs the answer in
its pane.

Start the dashboard from the terminal whose PATH holds `~/orch-live-test/bin` (`orch serve`).

## Run it

1. New ticket, Mode **Dark AI Factory**. Ask for something tiny that names its files (for example `a.txt` and
   `b.txt` with a line each: an epic that names no file is never closed by itself), type **dark**, choose Release
   up to **Production**, tick **Roll back production by itself if its check fails**, type **production**, and tick
   **Close the epic by itself when everything is proven**. Read the confirm: it names the stages, the window, the
   rollback and that the close replaces your verdict for this run.
2. The planner splits the epic; each worker builds its child in its own clone, on the branch `fx/<child>`, commits
   there and moves the child to testing. A child whose branch has no commit of its own is not merged ("the child's
   branch has no commits of its own").
3. Once the epic is Ready, the runner merges each child into `main` of the bare remote, deploys dev, then production
   (the window is open the first time), and then closes the epic by itself.

What to look at:

- the run view (`/factory/<epic>`): the ring's Merge, Dev and Production steps light one by one, then "Closed by
  itself under your charter" with the time, the summary and a Reopen button;
- `cat ~/orch-live-test/state/release.log`: every step the runner ran, with its arguments;
- `git --git-dir ~/orch-live-test/remote.git log --oneline main`: the merged children;
- `cat ~/orch-live-test/state/dev.version ~/orch-live-test/state/production.version`: both the commit dev was proven on;
- `orch check`: an info finding "charter-verdict" for the epic and each child (a decision you delegated in the
  charter);
- `orch show <epic>`: the verdict, logged as closed by itself under the Dark charter.

## Rehearse the stops

- **The window.** Start a second Dark epic with Production within an hour of the first release: after dev it shows
  "Production waits for its release window" with the time it opens. It is not Stopped; nothing runs until then.
- **A failed live check, rolled back.** Before production runs, `touch ~/orch-live-test/state/fail-production-check`.
  On a first run the file must exist before the epic becomes Ready: the runner goes from merge through production in
  one round, so creating it afterwards is too late.
  The production check fails, the rollback runs (`production.version` reads `rolled-back`), and the epic is Stopped
  with "Production rolled back". It does not close by itself. Remove the file, then Retry release on production (after
  the window).
- **A failed rollback.** Also `touch ~/orch-live-test/state/fail-rollback`: Stopped with "Rollback failed". While it
  is unresolved, no other epic's production runs in this workspace: a second Dark epic shows "Production is held".
- **Without the rollback signed**, a failed check stops with "Production check failed" and nothing is rolled back.
- **Reopen.** On a closed epic, Reopen (give a reason). It stops the run (the delegation is paused), the epic is open
  again and never closes by itself again under that charter. Its children stay done, so there is no verdict to give:
  the run view offers "Close the epic" with a reason, or add a child and approve the epic again.

## Clean up

Stop the dashboard, then:

```bash
orch factory clones list               # the clones the runner made for this workspace
orch factory clones clean <child>      # once per child: removes its clone and record (type the id to confirm)
orch factory release clear
orch factory dark off
```

and remove `~/orch-live-test`.
