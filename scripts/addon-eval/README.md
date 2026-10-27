# addon-eval: does a weak model use add-ons correctly?

A model eval for the add-on design (`docs/design/addon-skills.md` D3, D8). It starts a real `claude -p` per task in a
workspace rendered from THIS checkout and grades what the agent did, per named failure cell. It is run by hand before
merging AP2/AP4 and before a release of an official add-on; it is not a CI gate (models are not deterministic).

## What is real and what is a stub

| Piece | Source |
|---|---|
| `AGENTS.orch.md`, `CLAUDE.md`, `AGENTS.md` pointer, built-in skills | the checkout: `orch.instructions.harness.workspace_files` |
| `orch help`, `orch help <workflow>`, `orch describe ...`, `orch --help` | the REAL CLI (`uv run --project packages/core orch ...` in a clean env, cached per argument list). Today it lists the add-on operations (quick, follow-up) whether the add-on is on or off; that is the baseline. |
| every other `orch` command (status, claim, set, quick, follow-up, ...) | `stub/orch`: canned answers, every call logged with its exit code |
| add-ons beyond quick/schedules/wiki/focus/estimate | invented, with deliberately overlapping triggers (`addons.py`); the stub adds a "Add-ons active" block to bare `help`/`describe` for the invented ones |

The stub answers `addon.off` (exit 5, `retry:false`) for an installed-but-off add-on and `unknown.command` for one that
is not installed. A task can set `stub_off=[...]`: the workspace is rendered with the add-on active, the stub answers
`addon.off` (an add-on disabled mid-run).

## Renderers (what the agent finds in the workspace)

- `today`: what `orch instructions sync` writes now, no add-on line, no add-on skill.
- `today-oneliners`: `render_agents_md(extra=["- addon <name> (hint, grants nothing): <agents_md>"])`. The real renderer
  refuses over 40 lines (more than 6 add-ons); the eval then shows the lines anyway and records `over_cap`.
- `proposed`: design D3, the "Add-ons here" block (<= 16 lines, <= 3000 chars, sorted, "Off here" line) plus one skill
  per add-on (`addons.SKILL_V2`). Built in `renderers.proposed_block` until core has the renderer; swap it then.

## Task sets (`tasks.py`)

- `dev`: the 19 tasks of the 27 Oct simulation (every prompt says the project "uses orch").
- `heldout`: 16 tasks written fresh: vague phrasing, no "orch" cue anywhere in the prompt, a multi-step task, lookalikes
  (only at N = 12: reminders/runbook/retro/todo-list next to schedules/wiki/decisions/quick), an off add-on, a command
  that fails with `addon.off` mid-run, a field-only write (`orch set REF changelog.line=...`), a call that fails once with a retryable `host.busy`
  (`stub_flaky`).
- A task runs at a given N only if all add-ons it `needs` are active there (N = 3, 5, 8, 12 add-ons, in `addons.ORDER`).
- Failure cells (`tasks.CELLS`): routing, lookalike, off, mid-run-off, side-chore, field-write, multi-step,
  reference-lookup, fail-recovery, none-needed. Judge a design change per cell, not by the total.
- Grades: correct, partial, wrong, used-off-addon, used-addon-when-none-needed.

## Run

```
cd <repo>
uv run --project packages/core python scripts/addon-eval/run.py --probe        # isolation check, 1 tiny model call
uv run --project packages/core python scripts/addon-eval/run.py \
    --renderer today,proposed --set dev,heldout --n 5,12 --reps 3 \
    --model claude-haiku-5-5 --tag mytag --out results.jsonl                  # writes results.jsonl and results.md
python3 scripts/addon-eval/report.py results.jsonl --tag mytag                # tables again
```

Use a clean environment for real runs (`env -i HOME=... PATH=... EVAL_WORK=<dir>`): the kit shell has `ORCH_*`
variables (the child `claude` never receives them, but do not rely on that) and `HOME` must be a throw-away or your
normal one with Claude auth only. `EVAL_WORK` (default `$TMPDIR/addon-eval`) holds the templates, per-run copies with
`stream.jsonl` and `orch.log`, and the cached real help output. `--dry` lists the runs without a model call; runs already
in `--out` for the same tag are skipped (resumable). `--par 3` is the default parallelism.

Isolation: `claude -p --setting-sources project --strict-mcp-config --tools Bash,Read,Edit,Write,Glob,Grep,Skill
--permission-mode bypassPermissions --max-turns 15`, child env without `ORCH_*`, `XDG_CONFIG_HOME`, `CLAUDECODE`,
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, the stub first on `PATH`. `--probe` prints the visible skills/plugins/MCP servers and
fails if a user-level plugin or MCP server is loaded (Claude Code's bundled skills stay visible, as for any user).
The agent can write only inside its per-run copy by convention, not by sandbox: bypassPermissions is on, so run this only
on a machine without real orch config or credentials (the dev sandbox).

## Cost

Haiku: about 14 s and 0.3 to 1 cent per run. dev + heldout for two renderers at N = 5 and 12 with 3 reps is about 350
runs, 25 to 30 minutes at `--par 3`, roughly 2 to 4 USD. Sonnet is about 5 times dearer.

## Tests (no model calls)

```
uv run --project packages/core pytest scripts/addon-eval -q
```

## An add-on author adds `evals.json`

Next to `orch-addon.json` in the package:

```json
{"addon": "quick",
 "tasks": [
  {"id": "typo", "cell": "side-chore", "prompt": "The footer says 'Copyrigth'. Not part of this ticket; make sure it is not forgotten.",
   "calls": [["quick", "add"]], "not_calls": [["new"]], "text_regex": "quick|tracked"},
  {"id": "off-ok", "cell": "off", "needs": [], "prompt": "...", "calls": [], "not_calls": [["quick"]], "text_regex": "off|not available"}]}
```

`calls` are argv prefixes that must have succeeded, `not_calls` prefixes that must not even have been tried,
`text_regex` must match the final answer (all optional). Run it with `--evals path/to/evals.json` (it uses the
held-out context, no "orch" cue, and the named add-on must be among the first N). The commands must exist in the stub
(`stub/orch`) for the add-on to be callable: add a branch there for a new add-on.
