# Contributing

Thanks for helping. A few rules keep the project healthy.

## orch v2 core

The v2 core lives in `packages/core` (layout: `docs/architecture/orch-v2-core.md`, test tiers: `docs/architecture/orch-v2.md` Part B).

```bash
cd packages/core
uv sync --locked
uv run pytest tests/model/test_gates.py -x -q      # T0: one module, under 30 s, after every small change
uv run pytest -q -m "not slow" tests/store && uv run ruff check   # T1: the touched package plus lint
uv run pytest -q -n auto                           # T2: the whole core with slow tests (CI skips slow)
```

The shared protocol vectors (relay corpus and HTTP fixtures, hub vectors, publish vectors, F1 and R2) live once, in `contracts/` at the repository root; read `contracts/README.md` before you touch one. A change to a vector is one commit in this repository, and its PR lists every consumer (core, relay, publish, hub, mobile) it affects; run the `contracts` workflow's tests for them locally first.

## Before you push

Run the same steps CI runs, locally:

```bash
cd packages/core
uv sync --locked
uv run ruff check && uv run ruff format --check
uv run pytest -q -m "not slow" -n auto     # parallel (pytest-xdist); drop -n auto to debug one test
uv run pytest -q -m slow -n auto           # the slow tests, which CI runs as a second job
```

The tests marked `slow` (wheel and venv builds, the bridge mutants, a performance budget) run in their own CI job. Every run lists its 15 slowest tests: mark a new test `@pytest.mark.slow` when it lands there.

CI uses Python 3.14, the only supported version (`requires-python >=3.14`). The workflows are in `.github/workflows` (`orch-core`, `dashboard`, `relay`, `publish`, `hub`, `mobile`, `contracts`); each runs only when a path it covers changed.

## Pull requests

- One topic per pull request, with tests for the behaviour you change.
- Commit messages and PR descriptions are plain, imperative English.
- Tests never touch the real `~/.config/orch`, real workspaces or `~/.claude`, and make no network calls; the test fixtures isolate the config directories.
- `develop` requires the gate checks (`core gate`, `dashboard gate`, and per package `relay gate`, `publish gate`, `hub gate`, `mobile gate`, `contracts gate`); each gate reports on every PR and its jobs are skipped when nothing they cover changed. A draft PR is not tested until it is marked ready. Turn on auto-merge after opening a PR (`gh pr merge --auto --merge`) and it merges as soon as both gates are green; do not merge by hand before then.
- CI runs the core tests as 3 slices (`tests/_shard.py`, weights in `tests/_shard_weights.json`); after adding many tests, rebuild the weights with `scripts/shard_weights.py` (usage in its docstring). A stale file only costs balance, never coverage.
- Never weaken the human-only rules: approvals, answers, verdicts and closes come from a human, signed into their ledger.

## Security

Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md), never as a public issue.
