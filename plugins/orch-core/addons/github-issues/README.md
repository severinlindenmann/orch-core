# github-issues (default orch addon)

GitHub issues of every tracker in `external_trackers` whose URL is a github.com issue URL, e.g.
`{"prefix": "GH", "pattern": "GH-(?P<id>\\d+)", "url": "https://github.com/<owner>/<repo>/issues/{id}"}`.

- **Board · External:** a filter (Mine, Current sprint, All open; the default is a per-workspace setting), with a fallback to "Current sprint · not imported" when nothing is assigned to you. It lists issues with key, title, status, priority, assignee, sprint and the linked local ticket, or **Import** (a backlog ticket with the external key). It shows sprint progress (milestone = sprint, due date = end, "expected by today" when start and end are known) and an **Out of sync** list with **Close local**, **Reopen local** and **Ignore**.
- **Ticket:** the linked GitHub issue on the ticket page, with the same out-of-sync fixes.
- **Binary:** `gh` (signed in with `gh auth login`), only through `ctx.run`. Read-only towards GitHub. **Env passed through:** `GH_HOST`, `GH_TOKEN`, `GH_CONFIG_DIR`.
- **Setting:** Default filter on Board · External (`mine`, `sprint`, `all`).

Enable it per workspace in Mission Control → Workspace & addons.
