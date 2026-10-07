# Contributing

Thanks for helping. A few rules keep the project healthy.

## Before you push

Run the same steps CI runs, locally:

```bash
cd plugins/orch-core
uv sync --locked --extra dev --extra dashboard
uv run pytest -q -n auto --dist loadfile   # parallel (pytest-xdist); drop -n auto to debug one test
uv run orch addon check addon-template --strict && uv run pytest -q addon-template/tests
for m in addons/*/orch-addon.json; do d=${m%/orch-addon.json}; uv run orch addon check "$d" --strict; [ -d "$d/tests" ] && uv run pytest -q "$d/tests"; done
cd ../..
uv run --project plugins/orch-core python plugins/orch-session/test/make_fixtures.py /tmp/orch-session-fixtures \
  && ORCH_SESSION_FIXTURES=/tmp/orch-session-fixtures node --test plugins/orch-session/test/*.spec.ts
```

While you work, `uv run pytest -q -m "not slow"` skips the tests marked `slow` (wheel and venv builds, the bridge mutants, a performance budget); CI runs them all. Every run lists its 15 slowest tests: mark a new test `@pytest.mark.slow` when it lands there.

CI uses Python 3.11 and Node 24, so avoid syntax that needs a newer Python. CI also runs on every pull request to `main` and on `main` itself, but not on pushes to other branches.

## Pull requests

- One topic per pull request, with tests for the behaviour you change.
- Commit messages and PR descriptions are plain, imperative English.
- Tests never touch the real `~/.config/orch`, real workspaces or `~/.claude`, and make no network calls; the test fixtures isolate the config directories.
- Never weaken the human-only rules: approvals, answers, verdicts and closes come from a human, signed into their ledger.

## Security

Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md), never as a public issue.
