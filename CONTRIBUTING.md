# Contributing

Thanks for helping. A few rules keep the project healthy.

## orch v2 core

The v2 core lives in `plugins/orch-core` (layout: `docs/architecture/orch-v2-core.md`, test tiers: `docs/architecture/orch-v2.md` Part B).

```bash
cd plugins/orch-core
uv sync --locked
uv run pytest tests/model/test_gates.py -x -q      # T0: one module, under 30 s, after every small change
uv run pytest -q -m "not slow" tests/store && uv run ruff check   # T1: the touched package plus lint
uv run pytest -q -n auto                           # T2: the whole core with slow tests (CI skips slow)
```

The shared protocol vectors and HTTP fixtures in `plugins/orch-core/tests/wsproto/vectors/` come from orch-relay. The vector file is stored xz-compressed (`vectors_v2.json.xz`; `SOURCE` pins the sha256 of the decompressed bytes); never commit the raw `vectors_v2.json`. Refresh them (and `SOURCE`) with `scripts/sync-vectors.sh [ref]`; the default ref is `origin/develop` and the clone is `~/orch-dev/orch-relay` (override with `ORCH_RELAY_DIR`). Then bump `PINNED_RELAY_COMMIT` in `tests/wsproto/test_corpus.py` and commit the result as one PR.

## Before you push

Run the same steps CI runs, locally:

```bash
cd plugins/orch-core
uv sync --locked
uv run ruff check && uv run ruff format --check
uv run pytest -q -m "not slow" -n auto     # parallel (pytest-xdist); drop -n auto to debug one test
uv run pytest -q -m slow -n auto           # the slow tests, which CI runs as a second job
```

The tests marked `slow` (wheel and venv builds, the bridge mutants, a performance budget) run in their own CI job. Every run lists its 15 slowest tests: mark a new test `@pytest.mark.slow` when it lands there.

CI uses Python 3.14, the only supported version (`requires-python >=3.14`). The workflows are in `.github/workflows` (`orch-core`, `dashboard`).

## Pull requests

- One topic per pull request, with tests for the behaviour you change.
- Commit messages and PR descriptions are plain, imperative English.
- Tests never touch the real `~/.config/orch`, real workspaces or `~/.claude`, and make no network calls; the test fixtures isolate the config directories.
- `develop` requires two checks, `core gate` and `dashboard gate` (each reports on every PR; the dashboard jobs are skipped when nothing they cover changed). A draft PR is not tested until it is marked ready. Turn on auto-merge after opening a PR (`gh pr merge --auto --merge`) and it merges as soon as both gates are green; do not merge by hand before then.
- CI runs the core tests as 3 slices (`tests/_shard.py`, weights in `tests/_shard_weights.json`); after adding many tests, rebuild the weights with `scripts/shard_weights.py` (usage in its docstring). A stale file only costs balance, never coverage.
- Never weaken the human-only rules: approvals, answers, verdicts and closes come from a human, signed into their ledger.

## Security

Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md), never as a public issue.
