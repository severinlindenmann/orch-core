# Dev update, workspace prune, local fast loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `orch update --from/--version [--force]` brings CLI, host, runner and dashboard to one source and proves it; `orch prune workspace` removes one workspace so `orch setup` starts fresh.

**Architecture:** Two new front commands dispatched in `orch/cli/main.py` next to `setup`/`dashboard` (not registry ops). All side effects (uv, host calls, filesystem deletion, git ls-remote) sit behind small injectable interfaces so the order of steps is unit-tested without touching a machine. Human-only via the same `ORCH_GRANT`/`ORCH_SESSION` check as `orch/cli/dashboard.py:agent_refusal`.

**Tech Stack:** Python (stdlib + existing orch modules), uv, pytest. Repo `orch-core`, package `plugins/orch-core`.

**Spec:** `docs/design/dev-update-prune.md` (read §7 "Calls made by the designer" too). Related: `docs/design/install-update.md` §7-8.

## Global Constraints

- Work on a feature branch off `develop` in a worktree; PR into `develop`; never touch `main`.
- Both commands are human-only: refuse with code `human_only`, exit 3, when `ORCH_GRANT` or `ORCH_SESSION` is non-empty.
- Tests: T0 = `uv run pytest <file> -x -q` after every change; T1 = `uv run pytest -q -m "not slow" tests/cli tests/host tests/setup` plus lint. Anything over 2 s is `@pytest.mark.slow`. Tests use `HOME=$(mktemp -d /tmp/hkXXXX)`, never HOME under `~`; they never touch the real `~/.config/orch`, `state/` or a real keychain.
- Deletion is by explicit list only; never glob, never `rm -rf` of a directory the code did not enumerate; never print key material.
- No new network access except `git ls-remote https://github.com/severinlindenmann/orch-core <ref>`.
- Run `tests/cli` and `tests/test_layout_lint.py` after touching `layout.py` or anything under `orch/cli/`. If a new error code enters a catalog, regenerate the CLI goldens and declare it in `tests/cli/test_registry.py`.

## Review Focus

- Prune with a second workspace registered: the other workspace's registration, keys and files are untouched.
- Prune in a git workspace whose `.orch/` has uncommitted changes: refused without the typed folder name, even with `--yes`.
- Update when the install step fails: the old host is enabled again and the exit code is non-zero (never left with the host off).
- Update when the host was off before: it stays off (no service installed or enabled).
- Update with a mismatch (one component still on the old digest): exit non-zero and the component is named; never prints success.
- `--version` with an unresolvable ref fails before anything is stopped.

---

### Task 1: Owned paths and the prune plan

**Files:**
- Create: `plugins/orch-core/src/orch/ownedpaths.py`
- Test: `plugins/orch-core/tests/test_ownedpaths.py`

**Interfaces:**
- Produces: `ORCH_OWNED_PATHS: tuple[str, ...]` (names relative to the workspace root, built from `orch.layout.STORE_DIR` and `WORK_DIR`; `.gitignore` is NOT owned, setup only appends lines to it); `@dataclass(frozen=True) class PruneItem: kind: str  # "registration"|"state"|"path"|"kept"`, `label: str`, `path: Path | None`, `dirty: bool`; `def plan_prune(root: Path, state_dir: Path, workspace_id: str, *, shared_identities: Sequence[str] = ()) -> list[PruneItem]` (pure: reads the filesystem and `git status --porcelain` for `dirty`, deletes nothing).

- [ ] **Step 1: Write failing tests** in `tests/test_ownedpaths.py`: `test_owned_paths_match_setup` (run the setup create code path from `tests/setup/helpers.py` into a tmp workspace; assert every top-level entry setup created that is not `.git`/`.gitignore`/user files is in `ORCH_OWNED_PATHS`, and nothing in it is missing from disk after setup); `test_plan_lists_only_owned` (an unrelated `notes.txt` and a sibling file next to `.orch/` are absent from the plan); `test_plan_marks_dirty` (git workspace, modify a file under `.orch/` → item `dirty is True`); `test_plan_lists_shared_as_kept` (identity passed in `shared_identities` appears with `kind == "kept"`).
- [ ] **Step 2: Run** `uv run pytest tests/test_ownedpaths.py -x -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `ORCH_OWNED_PATHS`, `PruneItem`, `plan_prune` as above in `src/orch/ownedpaths.py`. The state items come from `state_dir/run/<workspace_id>/` and the workspace's keychain item names as the setup/custody code names them (find them in `orch/custody` and `orch/setup/steps.py`; keyed by workspace id only).
- [ ] **Step 4: Run** the same command → PASS; run `uv run pytest tests/test_layout_lint.py -q` → PASS.
- [ ] **Step 5: Commit** `feat: owned paths and prune plan`.

### Task 2: `orch prune workspace`

**Files:**
- Create: `plugins/orch-core/src/orch/cli/prune.py`
- Modify: `plugins/orch-core/src/orch/cli/main.py` (add `if args[:1] == ["prune"]:` dispatch beside `setup`, ~line 275)
- Test: `plugins/orch-core/tests/cli/test_prune.py`

**Interfaces:**
- Consumes: `plan_prune`, `PruneItem` (Task 1); `orch.cli.dashboard.AGENT_ENV` for the agent check; host calls `host.unregister` (see `orch/host/cli.py:329`) and the runner's per-run stop (`orch/runner/supervise.py: stop(run, grace=...)` via the runner CLI client).
- Produces: `def main(args: Sequence[str], *, env: Mapping[str, str], out: TextIO, err: TextIO, stdin: TextIO = sys.stdin, host: PruneHost | None = None) -> int`; `class PruneHost(Protocol): def runs(self, workspace_id: str) -> list[str]; def stop_run(self, run: str) -> None; def unregister(self, workspace_id: str) -> None; def is_dev(self, root: Path) -> bool; def demo_roots(self) -> list[Path]`. Usage: `orch prune workspace [path] [--yes] [--force]`.

- [ ] **Step 1: Write failing tests** (fake `PruneHost`, tmp state dir and workspace): `test_agent_env_refused` (`ORCH_GRANT=x` → exit 3, stderr has `human_only`, nothing deleted); `test_lists_then_asks` (no `--yes`, stdin `n` → exit 0, nothing deleted, list printed); `test_removes_in_order` (`--yes`: files and state removed, `unregister` called last; assert via the fake's call log); `test_second_workspace_untouched`; `test_idempotent` (second run prints `nothing to remove`, exit 0); `test_dirty_needs_typed_name` (dirty item + `--yes` alone → exit 2 and nothing deleted; stdin = folder basename → proceeds); `test_demo_workspace_refused_unless_typed` (path in `demo_roots()`: without explicit path argument refused; with it, requires typed basename); `test_non_dev_refused_on_dev_state` (`is_dev` False while the state dir is the dev sandbox → refused); `test_running_runs_need_force` (`runs()` non-empty: refused without `--force`; with it `stop_run` called for each, never a runner restart); `test_unstoppable_run_refuses` (`stop_run` raises → exit non-zero, nothing deleted); `test_partial_failure_keeps_setup_possible` (deletion of one path raises → registration is still present).
- [ ] **Step 2: Run** `uv run pytest tests/cli/test_prune.py -x -q` → FAIL.
- [ ] **Step 3: Implement** `main` and the real `PruneHost` (default when `host is None`; `is_dev`/`demo_roots` read `state/orch/dev-workspaces.json` through `orch/host/devmode.py`, demo roots are the registered paths of `orch-demo-workspace-1/-2`). Order: guard → resolve workspace → plan → print → confirm → stop runs → delete paths → delete state → unregister last. After success print `run: orch setup`.
- [ ] **Step 4: Run** `uv run pytest tests/cli/test_prune.py tests/cli -x -q` → PASS (the registry/human-only table tests must still pass; if they enumerate front commands, add `prune`).
- [ ] **Step 5: Commit** `feat: orch prune workspace`.

### Task 3: Update source resolution and plan

**Files:**
- Create: `plugins/orch-core/src/orch/cli/update_dev.py`
- Test: `plugins/orch-core/tests/cli/test_update_dev.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class Source: kind: str  # "local"|"git"`, `sha: str | None`, `dirty: bool | None  # None = unknown`, `core: str  # uv requirement for the core package`, `dashboard: str  # uv requirement for the dashboard`, `digest: str | None  # source-tree digest for local`; `def resolve_source(from_path: Path | None, version: str | None, *, ls_remote: Callable[[str, str], str] = _git_ls_remote) -> Source` (exactly one of the two given; unresolvable ref → `UpdateError("update.bad_source", ...)`; tag wins over branch); `def source_digest(checkout: Path) -> str` (same algorithm as `orch.host.release.code_digest` applied to `<checkout>/plugins/orch-core/src/orch`); `def uv_install_argv(src: Source) -> list[str]` (`uv tool install --force --with "<dashboard>" "<core>"`, non-editable; git form uses `git+https://github.com/severinlindenmann/orch-core@<sha>#subdirectory=…`); `class UpdateError(Exception): code: str`.

- [ ] **Step 1: Write failing tests**: `test_from_local_paths` (argv has the two absolute subdirectory paths, no `-e`); `test_dirty_counts_only_installed_subdirs` (change under `docs/` → not dirty; change under `plugins/orch-core/` → dirty; change only under `addons/dashboard/server/src/orch_dashboard/static/` → not dirty); `test_no_git_is_unknown`; `test_version_resolves_sha` (injected `ls_remote` returns a 40-hex sha; argv pins it in both URLs); `test_tag_wins_over_branch`; `test_unresolvable_fails_before_anything` (injected resolver returns nothing → `UpdateError` with code `update.bad_source`); `test_both_or_neither_flag_rejected`; `test_source_digest_matches_code_digest` (digest of this repo's own tree equals `code_digest()` run from the same tree).
- [ ] **Step 2: Run** `uv run pytest tests/cli/test_update_dev.py -x -q` → FAIL.
- [ ] **Step 3: Implement** as above. `_git_ls_remote(url, ref) -> str` calls `git ls-remote` with a 15 s timeout, returns a sha or `""`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: update source resolution`.

### Task 4: Update apply flow and proof

**Files:**
- Modify: `plugins/orch-core/src/orch/cli/update_dev.py` (add apply), `plugins/orch-core/src/orch/cli/main.py` (dispatch `["update"]` beside `prune`)
- Test: `plugins/orch-core/tests/cli/test_update_dev.py` (extend)

**Interfaces:**
- Consumes: `Source`, `resolve_source`, `uv_install_argv`, `UpdateError` (Task 3).
- Produces: `def main(args, *, env, out, err, stdin=sys.stdin, steps: UpdateSteps | None = None) -> int`; `class UpdateSteps(Protocol)` with `previous_state() -> PreviousState` (`host_enabled: bool`, `workspaces: list[Path]` that were registered, `dashboard_running: bool`), `running_work() -> tuple[int, int]` (agents, terminals), `stop_dashboard()`, `disable_host()`, `install(argv: list[str]) -> bool`, `new_orch(*argv) -> int` (exec the NEWLY installed `orch`), `digests() -> dict[str, str]` (keys `cli`, `host`, `runner`, `dashboard`), `reopen_dashboard()`; new error code `update.version_mismatch` (exit 5) and `update.bad_source` (exit 2).

- [ ] **Step 1: Write failing tests** (fake `UpdateSteps` recording calls): `test_step_order` (stop dashboard → disable host → install → register each workspace → enable with runner restart → digests → reopen dashboard); `test_agent_env_refused`; `test_running_work_needs_force` (3 agents, no `--force`, stdin `n` → exit 0, nothing stopped; `--yes` alone still asks; `--force` proceeds); `test_already_current_exits_zero_without_stopping` (digest equal and all components agree; `--force` reinstalls anyway); `test_mismatch_names_component` (runner digest differs → exit 5, stderr names `runner`, no success line); `test_install_failure_reenables_old_host` (install returns False and host was enabled → `enable` called, exit non-zero); `test_host_off_stays_off` (`host_enabled=False` → no enable, no register); `test_missing_workspace_path_listed_not_registered`; `test_success_line` (`orch 2.0.0.dev0 (dev, <sha>[+dirty])`).
- [ ] **Step 2: Run** `uv run pytest tests/cli/test_update_dev.py -x -q` → FAIL.
- [ ] **Step 3: Implement** the flow in the order the spec gives (§2 steps 1-7) and the real `UpdateSteps` (shells out to `uv`, `orch host …`, the runner CLI; digests via the control socket welcome, `orch.host.release.code_digest`, and `/api/host` for the dashboard). Proof polls up to 30 s. Add the two error codes to `orch/ops/errors.py` if front commands use the catalog; regenerate goldens (`tests/cli/golden/errors/`, `_exit-codes.txt`) and declare in `tests/cli/test_registry.py` as the rule requires.
- [ ] **Step 4: Run** `uv run pytest tests/cli -x -q` → PASS.
- [ ] **Step 5: Commit** `feat: orch update --from/--version`.

### Task 5: E2E scenario and docs

**Files:**
- Create: `e2e/scenarios/update-from.sh` (follow the existing scenario layout in `e2e/`; runs via `e2e/run.sh update-from`)
- Modify: `docs/design/dev-update-prune.md` (status line → implemented), `README.md` (a short "Test a fix locally" section with the two commands)

**Interfaces:** Consumes the two commands from Tasks 2 and 4.

- [ ] **Step 1: Write the scenario**: set up a demo workspace (dev sandbox), change a marker string in a checkout, `orch update --from <checkout> --yes`, assert the host digest equals `source_digest`; `orch prune workspace <path> --yes`, then `orch setup` succeeds again on the same folder.
- [ ] **Step 2: Run** `e2e/run.sh update-from` on the dev machine → PASS (run alone; this is T2, not part of the implementer's T1).
- [ ] **Step 3: Update docs**, commit `docs+e2e: update-from scenario`.

---

## Self-review

- **Spec coverage:** §2 → Tasks 3-4; §3 → Tasks 1-2; §4 safety → Global Constraints and Task 2 tests; §5 tests → every task; §6 tasks 1-5 map 1:1; §7 calls 1-10: 1 (proof) Task 4, 3 (`update` is new) Task 4 dispatch (bare `orch update` prints "not available yet": add this one-line case in Task 4 `main`), 4 (dirty) Task 3, 5-8 Task 1-2, 9 Task 4, 10 Task 3.
- **Type consistency:** `Source`, `UpdateError`, `PruneItem`, `PruneHost`, `UpdateSteps` used with the same names across tasks.
- **Ordering:** Tasks 1→2 and 3→4 are sequential; the pairs (1,2) and (3,4) touch disjoint files, so two implementers can run them in parallel. Both pairs touch `cli/main.py` in different lines: merge `develop` between them. Task 5 last.
- **Open for the builder:** the exact keychain item names and the runner client call for stopping one run (found in `orch/custody`, `orch/runner/cli.py`).
