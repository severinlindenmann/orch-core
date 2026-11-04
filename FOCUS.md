# Focus

orch core exists to make day-to-day work with agents easy: customer projects and private projects, on several
devices, without the user having to think about governance. Right now the priority is simple: **the requirements
and scenarios below must work end to end, the way a user experiences them.** Everything else comes after.

## How to work

1. Pick the first scenario in the status table that does not pass yet.
2. Run it end to end like a user would: real host, real dashboard, real CLI, the phone where the scenario uses it,
   two devices where it says so. No shortcuts through internals, no hand-edited state.
3. Where it breaks or behaves differently than a user would expect, fix it, then run the whole scenario again from
   the start.
4. A scenario passes when it runs start to finish without workarounds and you have evidence: screenshots, the
   resulting ticket, logs. Update the status table.
5. Only then continue with the next scenario.

When choosing what to do next:

- A bug that stops a scenario comes first.
- Friction in a scenario comes second: too many steps, confusing screens, approvals or signatures the user has to
  think about. Friction is a bug. The user should do their work, not operate orch.
- Work that does not serve a scenario comes after. If you notice something important, write it down at the end of
  this file under "Noticed" instead of starting it.

## Requirements

- **Workspaces** can run several on one device or on different devices (a customer machine, the user's laptop).
- **Each workspace has its own** skills, connections (MCP) and tools.
- **Central skills, tools and credentials** are configured once and assigned per workspace and per user. A workspace
  can add its own on top. Which credentials are central depends on the workspace and the customer; the vault is per
  user.
- **Model agnostic:** the user brings their own agent CLI (Claude Code today; Codex or OpenCode with local models
  must work too).
- **Publish:** a temporary link (a few days) to share a preview with a colleague, and a permanent link for an app
  that runs for weeks (for example `publish.severin.io/a/<id>`).
- **Workspaces know each other and hand over tasks automatically.** The first request of a kind asks the receiving
  workspace's owner, who accepts it once or permanently (or denies it). A permanent acceptance runs later requests
  without asking.
- **Scheduled tasks and continue later:** a ticket can wait for a date or for an external event (a PR review, a
  release) and continue by itself.
- **Visual representation:** the agent can show things visually while working, and the user sees all tickets and
  what needs them at a glance, also away from the laptop.
- **Ticket with a task list** that both the human and the agent can change at any time.
- **Evidence-based:** evidence is attached to the tasks it proves.
- **Context connected:** the ticket links the Jira issue, Teams messages, the PR, the worktree and its own history.
- **Mobile:** add input by voice to an existing ticket or as a new one. If the target workspace is offline, it
  syncs when it comes back and then acts (creates the ticket, starts an agent).
- **Customer machines run Windows** with Python and uv available. New binaries are hard to get approved there.

## Scenarios

| # | Scenario | Status | Last run | Evidence | Blocker |
|---|---|---|---|---|---|
| 1 | **Understand and plan a Jira ticket (Pilatus).** A Jira ticket arrives. The agent helps understand it, shows something visually, and writes the plan as a task list in the ticket. | not run | | | |
| 2 | **Parallel work and switching back.** While Pilatus waits, work on two tickets in Kistler. Switch back to Pilatus and see within a minute what was done and what is open. | not run | | | |
| 3 | **A change arrives by Teams.** A colleague writes about the Pilatus ticket on Teams. The agent reads it, updates the ticket and the plan, and implementation continues agent-assisted. | not run | | | |
| 4 | **PR to release to close.** A PR is open and the ticket waits for review. After review: merge, deploy, validate with evidence. The ticket waits about two weeks for the release, then wakes up, validates again, updates docs, closes the Jira ticket and cleans up worktrees and branches. | not run | | | |
| 5 | **Weekly report across devices.** On Friday the ALTYCA workspace gets what was worked on in Pilatus and Kistler (running on different devices), adds the calendar and the user's notes, and prepares the Abacus report. The user validates it, then it is uploaded. The first time, Pilatus and Kistler ask whether to accept the request; the user accepts permanently, and the next Friday runs automatically. | not run | | | |
| 6 | **Voice note on the go.** On the phone, dictate input for an existing Kistler ticket, or create a new one. Kistler is offline; when it comes back online it syncs and acts on it. | not run | | | |
| 7 | **Temporary preview link.** Share something under development with a colleague through a link that expires after a few days. | not run | | | |
| 8 | **Permanent app link.** Publish an app that must run for weeks under a permanent public link. | not run | | | |
| 9 | **Central setup.** Configure a skill, an MCP server and a credential centrally, assign them to Pilatus and ALTYCA for one user; each workspace also has its own local additions. Removing the assignment removes access. | not run | | | |
| 10 | **Another agent CLI.** Run scenario 2 with Codex or OpenCode instead of Claude Code. | not run | | | |
| 11 | **Windows customer machine.** Install and run the Pilatus workspace on Windows with Python and uv, without admin rights or new binaries, and pass scenarios 1 and 5 there. | not run | | | |

Status values: `not run`, `fails`, `works with friction`, `passes`.

## Noticed

<!-- Things worth doing that do not serve a scenario right now. One line each, newest first. -->
