# Default addons

Addons shipped with orch-core. Each folder holds one addon with an `orch-addon.json` manifest (see `../ADDONS.md`). They are trusted with the plugin version and stay off until you enable them per workspace in Mission Control → Workspace & addons. CI runs `orch addon check` on every folder here.

| Addon | What it adds |
|---|---|
| `github-reviews` | Code reviews page across the harness repo and `git.repos`, Today items, the ticket's PRs, Rerun failed, Mark ready |
| `github-issues` | Board · External for GitHub trackers (Mine, sprints from milestones), the ticket's issue, Import, Close/Reopen local, Ignore |
| `databricks` | Databricks page: prod/int/dev workspaces and login state, failed and running job runs (Create ticket), pipelines, compute for local development; read-only CLI, profiles mapped by you, simulated environments for demos |
| `wiki` | Related wiki pages on tickets, "may need an update" hints on Today, a Wiki page with search; GitHub wiki or local Markdown folder provider, page from a ticket, Confluence later |
| `model-routing` | A model per Start agent mode (Light, Standard, Strong tiers), a subagent model, "next start on Strong" per ticket and an escalation card after a task failed its verify twice; off by default, a launch is unchanged while it is off |
| `quick-tasks` | The switch and settings for quick tasks (`orch quick`, the Quick tasks page): whether agents may add them, the size limit; the feature itself is core's |
| `ticket-usage` | Claude Code usage per ticket: estimate at list prices, output tokens per model, an estimated share of the weekly limit, a Usage page; status line recorder script included |
| `schedules` | The switch for Schedules: workspace skills on a clock or an orch event, recurring tickets, findings on Today, a Today tile; the runner and the page are core's (docs/schedules.md) |

## More addons (`external.json`)

`external.json` lists addons that live in other repositories. Mission Control → Workspace & addons shows each one under **More addons** until it is installed, with what it adds, what it needs and the commands to install, trust and enable it (installing stays a human terminal step). To list another addon, add an entry: `name`, `title`, `description`, `repo` (https), optionally `path` (the addon's folder inside the repository), `needs` (short lines) and `adds` (`capabilities`, `slots`, `menu`, `remote_humans`, copied from its manifest).
