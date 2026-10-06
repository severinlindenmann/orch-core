---
name: orch-setup
description: Use when the user directly asks to set up orch or tickets in this repository — "set up orch", "use tickets here", "onboard this repo", or /orch-core:setup — or when the user says yes after you have already offered to set it up once this session. Do not trigger just because a session-start hint is shown or doctor lists open items; that is only ever a reason to offer once, not to start unasked. Guides the user through installing the terminal CLI, answering the customer questions, running orch init, adopting existing agent files and installing the commit check, one question at a time.
---
# Set up the orch ticket workflow

## 0. Only when asked, or after you offered
If the user asked directly (by name, or via `/orch-core:setup`), start. Otherwise you got here from a session-start hint or an open-items list: offer once, in one sentence. If the user says no, run `orch setup --dismiss` and stop — do not bring it up again this session.

You guide the user from "nothing set up" to a working orch workspace. Ask one question at a time, propose a default you detected, and run a command that changes something only after the user says yes. Never commit, never push.

## 1. Check the current state
Run `orch doctor --json` and read every check. In a repository with no `orchestrator/` folder yet, doctor stops at the `workspace` check — there is no `adopt`, `repos` or `hooks` check yet; those only appear once `orch init` has run, so re-check with doctor after init (step 5).

**Existing workspace:** if the `workspace` check is ok (an `orchestrator/` folder already exists), do not run `orch init` again. Do step 2 if `terminal-cli` is not ok, then go straight to step 5 and work through doctor's open items.

If `orch` itself cannot run (for example under GitHub Copilot, where the plugin's `bin/orch` is not on the path):
- `uv` is installed (`uv --version` works): offer the terminal install first — `uv tool install "<plugin folder>[dashboard]"`, with the real plugin folder — and run it only after the user says yes. Then continue with `orch doctor --json`.
- Neither `orch` nor `uv` works: tell the user to install uv (https://docs.astral.sh/uv/getting-started/installation/) and stop.

## 2. CLI for the user's own terminal (`terminal-cli`)
Explain: approvals, answers and the dashboard (`orch serve`) must come from the human's own terminal, so orch needs to be installed there once. Offer the exact `fix` command from doctor (it looks like `uv tool install "<plugin folder>[dashboard]"`); run it only after the user says yes. If the user declines: say that approvals and `orch serve` still need it, give them the command to run later themselves, and don't promise the dashboard works yet.

**Version first:** do this before `orch init` and before any `orch hooks install`. If an `orch` is on the PATH but doctor says its version differs from the plugin's (or that it is too old to report its version), `orch init` and `orch hooks install` would run that older CLI and its options may be missing. Tell the user both versions, offer doctor's `fix` (`uv tool install --force "<plugin folder>[dashboard]"`), run it only after the user says yes, and run `orch doctor --json` again until `terminal-cli` is ok. If the user declines, stop before `orch init`.

## 3. Before `orch init`: existing agent files
Check whether `AGENTS.md` or `CLAUDE.md` already exists at the repo root (doctor cannot tell you this yet — there is no workspace). If one does, explain that orch keeps everything already there and only appends a marked block at the end, which it can update later without touching the rest; ask the user. On a yes, add `--adopt` to the `orch init` command in step 4.

## 4. Workspace answers and `orch init`
Detect defaults first: the folder name for the customer, sub-folders with a `.git` directory for repos, the git remote host for the git type. Then ask, one at a time:
1. Customer name (default: folder name).
2. Ticket prefix for local tickets (default `L`; for example `ACM` for a customer called acme).
3. External ticket system: key prefix, key pattern and browse URL with `{key}` — e.g. `ABC`, `ABC-\d+`, `https://example.atlassian.net/browse/{key}` — or none. For GitHub issues use the group form, e.g. GH, GH-(?P<id>\d+), https://github.com/<owner>/<repo>/issues/{id}, so keys read GH-12 and never collide with plain numbers. Use a prefix different from the local one, so the two never collide.
4. Git host: github, gitlab, gitlab-selfhosted, bitbucket-server, bitbucket-cloud, azure-devops; and its base URL if self-hosted.
5. Pull request or merge request (`PR` or `MR`).
6. What agents may do on their own: any subset of the exact tokens `commit`, `push`, `review` — or `none` (the safe default).
7. Which repos belong to this workspace (name, and path if it differs from the name).

Pick the harness for where you are running:
- In Claude Code, running from this plugin: always pass `--harness claude-plugin`. Without it, `orch instructions sync` copies the skills into `.claude/skills` instead, duplicating the skills this plugin already provides.
- Under GitHub Copilot: pass `--harness copilot` (it writes `.github/copilot-instructions.md` and the skill copies Copilot reads from `.agents/skills`). Do not pass `--harness claude-plugin` there.

Show the full command you are about to run, for example:
`orch init --customer acme --prefix ACM --harness claude-plugin --tracker "ABC=ABC-\d+=https://jira.example/browse/{key}" --git-type gitlab-selfhosted --git-base-url https://git.example --review-term MR --agent-may none --repo dbt-models --adopt`
In Claude Code, add `--harness copilot` too if teammates use GitHub Copilot, and `--adopt` only if step 3 said yes. Run it only after the user says yes. If the user declines `orch init` itself, offer `orch setup --dismiss` and stop — there is nothing further to set up without a workspace.

## 5. After `orch init`: re-check with doctor
Run `orch doctor --json` again — this is the first point where `adopt`, `repos` and `hooks` can appear.
- `adopt`: if it is still not ok (for example a harness added later, or `--adopt` was not passed in step 4), run `orch instructions sync --adopt` only after the user says yes.
- `repos`: if no repos are configured, either re-run `orch init` with `--repo NAME[=PATH]` added for each one, or follow doctor's `fix` hint.
- `hooks`: optionally offer `orch hooks install --stage-records`, which adds a pre-commit hook that stages the gate snapshots, events.jsonl and counter.json (never caches) whenever a commit stages a ticket; it only runs `git add`, orch still never commits, and `orch hook pre-commit --list` shows what it would stage. Under a repo-owned `core.hooksPath` the pre-commit hook is delegated the same way as commit-msg (its tracked file belongs to the repository and has to be committed; a foreign one is only extended, never replaced), and a skipped install is always reported. A commit that names paths (`git commit <paths>`) uses a temporary index, so it may not carry the staged records; run `orch hook pre-commit` and `git add` them, or commit without paths. Explain the check rejects commits without a ticket key or with AI attribution lines, and it runs after any commit hook the repo already has. Run `orch hooks install` only after the user says yes. In a repo whose own config sets `core.hooksPath` to a folder inside the repo, it also writes (or extends) that folder's `commit-msg` so it runs this clone's check (for husky's `.husky/_` it uses `.husky/commit-msg`); tell the user that file belongs to the repository and has to be committed. If that checkout must stay clean, offer `orch hooks install --untracked` instead: it changes no tracked file, points only this clone's `core.hooksPath` at `<git dir>/orch-hooks/` and runs the repo's own hooks after orch's check (`orch hooks uninstall`, run by the user, undoes it). A `core.hooksPath` from the global config, outside the repo or ignored by git is never written to. If a repo is reported as skipped (it already has its own commit-msg hook, or one that is not a shell script), tell the user what the message says and leave it.
- `gitignore`: `orchestrator/.gitignore` has no (or an outdated) orch block, so caches and locks could be committed. Run `orch doctor --fix` (it writes only that block) after the user says yes. If it names local files git already tracks, show the `git rm --cached` command from `fix` and run it only after the user says yes.
- `git` reads "local only": the workspace root is a plain folder of the configured repos, so the records have no history. That is fine as it is. Optionally offer `orch doctor --init-git` (a local-only repo at the root, no remote, nothing committed, the repos and `.claude/worktrees/` ignored); run it only after the user says yes.
- `hooks-path`: a repo's `core.hooksPath` points to a missing folder, so none of its own hooks run. Tell the user; never change `core.hooksPath` yourself.
- `records`: orch records (tickets, gates, events, synced instructions) are not committed. orch never commits them on its own: tell the user which files doctor names and that they are shared records. `orch records commit` commits exactly those (other staged files stay staged), with an `orch: records …` subject the commit check accepts before plan approval; `--dry-run` shows what it would commit. Run it only if the workspace lets you commit and the user says yes; otherwise the user runs it.
- `unclassified`: files in `orchestrator/` that orch did not write. Show them to the user; never delete or ignore them yourself.
- `plugin`: the plugin is not enabled in `.claude/settings.json`; run `orch instructions sync` only after the user says yes.
- `harness`: the workspace still uses harness `claude` while this plugin is active, so the guard, hooks and skills run twice. Explain that, then — only after the user says yes — follow doctor's `fix`: change `"harnesses"` in `orchestrator/config.json` from `"claude"` to `"claude-plugin"`, run `orch instructions sync` (it removes the old orch hooks from `.claude/settings.json` and enables the plugin), then re-run doctor.
- `skill-copies`: leftover skill copies in `.claude/skills` (from an earlier `claude`-mode sync, including the old names `tickets`, `refine-ticket`, `work-on-ticket`). Show the folders doctor lists and delete them only after the user says yes; never delete anything doctor did not list.
- `legacy-plugin`: an earlier orch plugin id (such as `orch-ticket-workflow@ai-convenience-store`) is still enabled, so the guard and session hooks run twice. For `.claude/settings.json`, run `orch instructions sync` only after the user says yes; it removes only the earlier orch ids and leaves every other plugin alone. For any other file doctor names, show the entry and let the user remove it.

## 6. Finish
Offer to create a first ticket from what the user is working on right now (`orch new --title "..."`, then the orch-refine-ticket skill). Tell the user to run `orch serve` in their own terminal to open the dashboard — never run `orch serve` yourself, the guard blocks it for agents.
