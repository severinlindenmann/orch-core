# Dev update, workspace prune, local fast loop

Status: design draft, 23 Oct 2026. Owner request (23 Oct): while testing workspaces end to end, every fix needs many
manual steps and still often leaves the host or dashboard on the wrong version; and starting again from a clean
workspace is slow. Two commands and one loop fix both: `orch update --version/--from [--force]`, `orch prune
workspace`, and `--from <checkout>` as the local fast loop. Part A decisions (D1-D29) are untouched. The release-based
`orch update` in [install-update.md](install-update.md) §7 is unchanged and stays the path for normal users; this
document adds the **source modes** that work without an install record.

## 1. Why

Today a tester who wants a fix runs, by hand: `orch dashboard stop`, `orch host disable`, a `uv tool install --force
--with orch-dashboard @ … <git+url@sha>` with a pinned commit, `orch host register .`, `orch host enable`, and, when
agents or terminals are running, `launchctl kickstart -k gui/<uid>/io.orch.runner`. Each step can be forgotten or run in
the wrong order, so CLI, host, runner and dashboard end on different versions (the 23 Oct laptop session ended with
the runner "from an older orch version"). A fix also waits for a merged PR, CI and a new sha before it can be installed.
Starting over means removing the registration, keys and the orch files in the workspace by hand.

Goals:
- One command brings CLI, host, runner, dashboard and add-ons to one named source and proves it.
- One command removes one workspace completely, so `orch setup` runs from a fresh state.
- A fix can be tested minutes after it is written, from a local checkout, with no PR, CI or sha.

Non-goals: release signing, channels, the update check (install-update.md); changing how add-ons are versioned;
pruning anything but one workspace; any agent-runnable update or prune.

Anti-goals (failure even if it "works"): touching a workspace, key or config the command was not pointed at; deleting
files that orch did not create; an agent running either command; reporting success while the host, runner or
dashboard still run old code.

## 2. `orch update` source modes

```
orch update --from <checkout> [--force] [--yes]       install the working tree of a local orch-core checkout
orch update --version <sha|tag|branch> [--force] [--yes]   install that git ref of orch-core from GitHub
```

`--from` and `--version` are mutually exclusive, work without an install record (they are the "development install"
path that install-update.md reports as `update.no_record` for the release flow), and never touch the install record.
Both install the core package and the dashboard add-on **as a pair from the same source**: for `--version`, the ref is
resolved to a full sha first and both `#subdirectory=` URLs use it (the current manual recipe); for `--from`, the paths
`<checkout>/plugins/orch-core` and `<checkout>/addons/dashboard/server`, installed non-editable so the host's release
copy matches (editable installs would make the digest depend on files that keep changing).

Flow, as one command (the person sees each step named, as in `orch setup`):
1. Refuse with `human_only` when run by an agent (same shared helper as install-update.md §8). Print the plan: source,
   resolved sha (and `dirty` with the changed-file count for `--from`), the workspaces that will be re-registered.
2. Check running work: count agents and terminals in the runner. If any run and `--force` is absent, ask "N agents and
   M terminals are running; restarting the runner stops them. Continue? [y/N]". `--force` answers yes; `--yes` alone
   does not (it never answers this question, as in install-update.md §7).
3. Stop the dashboard servers started by the person (pidfile), then drain the host (`orch host disable` logic).
4. `uv tool install --force --with <dashboard source> <core source>`. uv installs atomically, so on failure the old
   install is still in place; the command then re-enables the old host (the flow jumps to step 7) and exits non-zero,
   so the person is never left with the host off.
5. Exec the **newly installed** `orch`: `host register <path>` for each workspace that was registered before (read from
   the host state before step 3), then `host enable --restart-runner` (the runner restart is the part that needs step 2).
6. **Prove it.** Compare the release digest (`orch.host.release.code_digest`, D-P2-4) reported by the CLI, the host
   welcome, the runner and the dashboard server. Equal everywhere → print `orch 2.0.0.dev0 (dev, <sha>[+dirty])` and
   exit 0. Any mismatch → name the component still on old code and exit non-zero (`update.version_mismatch`).
7. Re-open the dashboard if it was running before.

`--force` has exactly two meanings: restart the runner although agents/terminals run, and reinstall even when the digest
already matches (otherwise "already current" exits 0 without stopping anything).

Fast loop: `orch update --from ~/orch-dev/orch-core --yes` is the whole cycle after a local fix. The dashboard page must
be built for `--from`: if `static/index.html` is missing and Node >= 24 exists, the hatch hook builds it (existing
behaviour); otherwise the command fails before stopping anything with the exact build command to run.

## 3. `orch prune workspace [path]`

Removes one workspace so `orch setup` starts from nothing. `path` defaults to the current directory's workspace.

What it removes, listed first and asked about (`--yes` skips the question, never the list):
1. The host registration of that workspace (and its served socket state).
2. That workspace's keys and state under the state dir (`run/<ws-id>/`, its keychain entries, person and device
   material that belongs to it only). The **host's own identity**, other workspaces, and anything shared stay.
3. The files and folders orch created inside the workspace folder: `.orch/` and the other orch-owned paths the setup
   wrote, from one list in code (`ORCH_OWNED_PATHS`) that setup also uses. Anything not on the list is left, even next
   to orch files. A path on the list that has uncommitted changes in a git worktree is shown as such in the list.

Rules:
- Human-only (`human_only`, exit 3 for an agent or `ORCH_GRANT`).
- The runner is stopped for that workspace first; running agents/terminals need `--force`, as in §2.
- On a machine with the dev state dir (`~/orch-dev/state`, workspaces marked `dev`), prune acts only on workspaces
  marked `dev`; it refuses the agents' demo workspaces (`orch-demo-workspace-1`, `-2`) unless the path is given
  explicitly **and** the person confirms by typing the workspace name. Real workspaces are not reachable from a
  dev-state host at all (the existing `dev: true` certificate rule).
- Idempotent: a second run reports "nothing to remove". A failure midway leaves a state `orch setup` can still start
  from (registration removed last, after files and keys).
- After success: prints `run: orch setup` in the workspace folder.

## 4. Safety summary

Both commands are human-only. Neither writes outside the state dir, the workspace folder and the uv tool directory.
Neither reads or prints key material. The prune list is the only deletion list; nothing is deleted by pattern or glob.
No new network access beyond `--version` fetching the named git ref (same URL the manual recipe uses).

## 5. Tests

- Unit: arg parsing and mutual exclusion; the plan/list builders; `ORCH_OWNED_PATHS` equals what setup writes (one
  test creates a workspace via the setup code path and asserts prune would list exactly those paths).
- Prune on a throwaway state dir: removes the registration, keys and listed files; leaves an unrelated file in `.orch/`'s
  parent and a second workspace untouched; idempotent; refuses demo workspaces and agents.
- Update: with `uv` and `host` calls injected, assert the step order, the digest comparison (mismatch → exit code and
  named component), `--force` semantics, and that the host is re-enabled when the install fails.
- CLI goldens for the new catalog codes (`update.version_mismatch`, `prune.refused`), `_exit-codes.txt`, and
  `test_registry.py` declarations (per the catalog-code rule).
- E2E (T2): `e2e/run.sh update-from` on the dev machine: setup a demo workspace, change a marker string in a checkout,
  `orch update --from`, assert the running host reports the new digest; then `prune workspace` and `orch setup` again.

## 6. Tasks (each about 30 min, disjoint files)

1. `ORCH_OWNED_PATHS` + prune plan builder and removal (`orch/cli/prune.py`, unit tests).
2. `orch prune workspace` front command, guard, catalog codes, goldens.
3. Update source resolution and plan (`orch/cli/update_dev.py`): `--from` / `--version`, sha resolve, dirty count.
4. Update apply flow and digest proof; wire into `orch update` beside the release flow.
5. E2E scenario `update-from` + docs (`README`, this file's status → implemented).

## 7. Calls made by the designer (from the builder check; the owner can overrule)

1. **Dashboard proof.** The dashboard server has no digest today. The proof is: core digest equal on CLI, host welcome
   and runner; the dashboard dist-info version/direct-URL equals the install source; and the restarted dashboard
   process's `/api/host` (exists since #586) answers with the host's digest. No new dashboard endpoint.
2. **Add-ons.** Built-in add-ons ship inside the core and dashboard packages (`orch/addons/manifests`, the dashboard's
   `orch-addon.json`), so they follow the pair. Third-party add-ons are not touched. No extra step.
3. **`orch update` does not exist yet in code.** This work creates the `update` front command with only the two source
   modes. The release flow (install-update.md §7) lands later on the same command; bare `orch update` and `update core`
   fail with a clear "not available yet" message until then.
4. **Dirty `--from`.** `+dirty` = `git status --porcelain` on the two installed subdirectories, ignoring the build
   output (`static/`). The proof compares the installed digest with the digest of the source tree, not only the
   components with each other. "Already current" = installed digest equals source digest and all components agree.
   A checkout without a usable `.git` installs fine and reports `unknown` instead of `dirty`.
5. **Prune ownership rule.** Remove only what is keyed by the workspace id (`run/<ws-id>/`, its keychain items, add-on
   and relay registrations for it). A person or device identity used by another workspace is left and named in the
   list as "kept (shared)". When unsure, keep and list.
6. **Prune and uncommitted work.** Paths with uncommitted or untracked-by-git changes make the confirmation stricter:
   the person types the workspace folder name, and `--yes` is not enough. If `.orch/` is tracked in git, the list says
   so and the removal shows as changes in the repo.
7. **Dev-state check.** "Marked dev" is read from `state/orch/dev-workspaces.json` (via `devmode.py`) and the
   certificate flag. Demo workspaces are those two entries by registered path, not a name match. The typed name is the
   folder basename.
8. **Prune and the runner.** Prune only stops the runs of that workspace through the runner's per-run stop; it never
   restarts the whole runner. If a workspace's runs cannot be stopped individually, prune refuses and says why.
9. **Update when the host was off or nothing registered.** Update restores the previous state: it enables the host
   only if it was enabled before, re-registers only workspaces that were registered and whose path still exists (it
   lists the others), and never installs a service that was not installed.
10. **`--version` network.** The ref is resolved with `git ls-remote` against the same `https://github.com/…/orch-core`
    URL the manual recipe uses (the resolver is injectable for tests). Ambiguous names: tag wins over branch; an
    unresolvable ref fails before anything is stopped.

## 8. Left to the builder

Exact step wording and progress output; how the previous host state is read in step 5; whether the digest proof
polls or waits on host readiness (budget: 30 s); the precise `ORCH_OWNED_PATHS` entries (taken from what setup writes
today); the launchd/systemd differences for the runner restart (macOS first, the existing `enable` logic elsewhere).
