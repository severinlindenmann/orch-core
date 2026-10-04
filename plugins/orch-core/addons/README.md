# Default addons

Addons shipped with orch-core. Each folder holds one addon with an `orch-addon.json` manifest (see `../ADDONS.md`). They are trusted with the plugin version and stay off until you enable them per workspace in Mission Control → Workspace & addons. CI runs `orch addon check` on every folder here.

| Addon | What it adds |
|---|---|
| `github-reviews` | Code reviews page across the harness repo and `git.repos`, Today items, the ticket's PRs, Rerun failed, Mark ready |
| `github-issues` | Board · External for GitHub trackers (Mine, sprints from milestones), the ticket's issue, Import, Close/Reopen local, Ignore |
| `databricks` | Databricks page: prod/int/dev workspaces and login state, failed and running job runs (Create ticket), pipelines, compute for local development; read-only CLI, profiles mapped by you, simulated environments for demos |
| `wiki` | Related wiki pages on tickets, "may need an update" hints on Today, a Wiki page with search; GitHub wiki or local Markdown folder provider, page from a ticket, Confluence later |
