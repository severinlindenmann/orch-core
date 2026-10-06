# Quick tasks

Switches on **quick tasks**: one-line jobs too small for a ticket (a typo, a version bump, a dead import), with no
requirements, plan or verdict, closed with one line of proof and kept small by a size limit. Off until you enable it
per workspace in Workspace & addons (or `orch addon enable quick-tasks` in your own terminal). Once on, **Quick
tasks** appears in the menu under **Addons**.

Quick tasks are part of core; this addon is only their switch. While it is off, `orch quick` lists nothing and
refuses changes, `orch next` offers none, the commit-msg hook refuses `Q-…` keys, and `/quick` answers 404.

Settings (Workspace & addons):

| Setting | Default | Meaning |
|---|---|---|
| Agents may add quick tasks | off | Whether agents may run `orch quick add` themselves |
| Most commits | 1 | Commits naming the task before `orch quick done` marks it "outgrew it" |
| Most files | 3 | Files those commits and the working tree change (orch's own records left out) |

The settings live in your orch config dir, outside the repository, where agents cannot write. The key prefix, how
`orch next` offers quick tasks, artifacts per task and the claim timeout are in `orchestrator/config.json` under
`quick`. Full reference: `docs/quick-tasks.md`.

No binaries, no background fetches.
