# Repos addon — preview proposal

Owner request, 2026-10-10 (U3). A workspace is often a harness root containing several independent repositories. Repos makes that structure visible and lets a person declare, check, clone, fetch, adopt, and remove declarations through core's existing action surfaces. It is installed in the normal and busy demo datasets; all git operations are simulated.

## Declared and observed

The workspace provides a read-only `root_folder`. Each declaration has a stable repo name, a single relative folder, a credential-free remote, a default branch, and an optional primary flag (at most one). Observations are a separate last-check snapshot: folder existence, git presence, actual origin, branch, ahead/behind, changed files, last fetch, size, and worktrees. A mock disk snapshot lets checks discover changed state without changing declarations. Undeclared git folders are untracked; removing a declaration leaves the folder intact.

Structure shows one expandable row per folder. Checks lists missing folders, remote mismatches, behind branches, and uncommitted work. Activity records who requested each operation and when. Today shows readiness and offers core-signed clone decisions only for missing repos. Linked ticket panels are read-only and link to the corresponding expanded row. Settings choose off / 15 min / 1 h / daily and fetch-on-check. This mock advances jobs and automatic checks on state reads while mounted; the real host must schedule them independently of the browser.

Clone jobs run queued → cloning → present in six seconds of the mock clock. Busy DEMO's `private-api` fails its first attempt with “Repository not found or no access”; Retry succeeds to demonstrate recovery. No credentials or network are used. Fetch simulates a new remote commit and refreshes tracking data; it does not pull, merge, or discard local edits.

## Ticket format relationship

`links.repos` names the workspace declarations; the Repos panel uses exact names, not basename guesses. DEMO-0046 links `web-portal`. Existing legacy fixture repo names remain visible as “not declared” rather than silently resolving to another repo.

This list is a proposed UI for host-managed `settings.repos`, not a new ticket source of truth. In orch-v2-ticket-format §5.7 (D58/D59), repo identity is a canonical credential-free HTTPS origin (or `local:<name>`), distinct from a local folder and display name. The source list contains identity, ref, and SHA for each name in `links.repos`; it is projected from host-observed `branch.pushed {repo_name, repo_id, ref, sha}`. The real host must reread git at signing, decision append, and landing, and invalidate stale gates. This preview does not write `branch.pushed` or manufacture source SHAs.

## Host enforcement and real implementation

The host checks manifest roles and core confirmation before actions. Clones, bulk clones, adds, and adoption bind full remote URLs and target folders; a changed target is refused. Removal is owner-only and destructive-confirmed, refuses linked open tickets with a count (409), and has a separate options confirmation with an explicit “Remove anyway” choice. Core's cover says “The folder and its files stay on disk.” No action deletes files.

Both draft preparation and signed add validate inputs. HTTPS and `ssh://` URLs reject all userinfo, including username-only authorities; `git@host:path` is the supported SSH transport spelling. Folder names match `^[a-z0-9][a-z0-9._-]{0,63}$`, contain no `..` or slash, and cannot reuse an existing folder. Duplicate remotes are refused. Credential values never enter a persisted draft. The host must additionally resolve filesystem paths safely, refuse symlink escapes, lock clone destinations, and prevent races with external filesystem changes.

The real host needs persistent declarations; filesystem/git scanning; cancellation and durable clone progress; credential-provider integration; fetch scheduling; disk/worktree measurements; a terminal opened under the proper OS identity; and activity delivery. Provisional events are `repos.added`, `repos.removed`, `repos.checked`, `repos.clone_queued`, `repos.cloned`, `repos.clone_failed`, `repos.fetched`, and `repos.terminal_opened`, with actor `addon:repos` and the requesting person separately recorded. Repos needs `network` and `pty`; opening the dock also checks Terminals' current pty grant. The shell has `cd` typed but not executed.

## Owner decisions

- Which OS user runs clone/fetch, and whose SSH agent, keychain, credential helper, or host connection supplies credentials? Never accept secrets embedded in a remote URL.
- Confirm ownership of declarations in `settings.repos`, repo-name migration rules, and remote canonicalization/alias policy.
- Confirm default auto-check frequency, fetch-on-check policy, resource limits, and behavior while offline.
- Confirm whether remote mismatch blocks all agent starts, how primary influences new tickets, and whether forced declaration removal should also offer ticket relinking.
- Ratify the provisional event names and the `network`/`pty` capability boundary before implementing a real host.
