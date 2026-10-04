# databricks (orch default addon)

Shows, per environment, the Databricks job runs that failed or are running, pipeline health, deploy drift and the compute you can use for local development. Read-only.

- **Capabilities:** provider (`databricks`, kind `status`, one snapshot per environment), page (menu "Databricks"), panels on Today (`today.summary` tile "Databricks failures", `today.from_addons` card), settings, actions (Create ticket, I logged in).
- **Binaries:** `databricks`, through `ctx.run` only. Commands: `current-user me`, `jobs list-runs`, `pipelines list-pipelines`, `warehouses list`, `clusters list` (allowlist in `orch_databricks/dbcli.py`). Every call passes `--profile <profile> -o json`.
- **Settings (per workspace, Workspace & addons):**
  - Environments: one line per env, `dev = <profile> @ https://<workspace host>`. The host is pinned: before every call the addon checks that the profile in `~/.databrickscfg` still points to it. `DEFAULT` is never used, and the addon never picks a profile for you.
  - `int = simulated:int` serves recorded demo data from `fixtures/simulated/int.json`, only with "Demo workspace" on. Simulated cards and rows are marked "simulated".
  - Show runs and pipelines: `mine` (default: created or run by you), `prefix` (names starting with one of the prefixes), `all`.
- **Deploy drift:** shown only for simulated environments, from their fixtures. Real workspaces show "Deploy drift is not checked for real workspaces yet". The addon never runs any `databricks bundle` command: even `bundle summary` loads `databricks.yml`, which runs the repo's own scripts and Python (preinit/postinit hooks, Python resources), so it is not read-only. Real drift needs a parser that reads the bundle without running it.
- **Login:** when a login expires the env card shows "login needed" with the exact `databricks auth login …` command to copy. The addon stops calling the CLI for that env until you press "I logged in" (or the CLI's token cache file changes).
- **Never:** starts, stops, deploys or edits anything; stores tokens; reads `auth token`.

Tests: `uv run pytest -q addons/databricks/tests` from the orch-core plugin folder. Recorded CLI output lives in `tests/fixtures/` (names of other people replaced).
