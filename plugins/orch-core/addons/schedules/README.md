# Schedules

Run workspace skills on a clock or when an orch event happens, and put recurring tickets on Today. Off until you enable it per workspace in **Workspace & addons**. The full guide is [docs/schedules.md](../../docs/schedules.md).

While it is enabled (and tmux is installed):

- **Schedules** in the menu lists every schedule in `orchestrator/schedules/*.yaml` with its state, trigger, next run and its last runs, and arms, pauses or runs one now;
- Mission Control's runner starts the runs that are due, one headless agent session each, and ends them at their time cap;
- what a run found shows on Today under **From schedules**: you file the proposed ticket in backlog or dismiss it;
- Today shows a tile with how many schedules are armed.

The scheduler, the page and the findings are part of orch-core, because addons render widgets only and cannot start processes; this addon is their switch. When it is off, nothing runs, the menu item is gone and `/schedules` answers 404.

## Needs

- `tmux` on PATH.
- Claude Code (`claude`) and `env` at a trusted path, and orch-core enabled in your user-scope Claude settings (runs ignore project settings), the same as the AI Factory runner.

## Settings

None. What runs is in the schedule files; what may run is what you armed.

## Safety

A schedule runs only after you arm it (`orch schedule arm <id>` in your terminal, or Arm on the Schedules page). Arming signs the definition and every file of its skill into your ledger; any later change pauses it until you look again. Runs act as agents: they can read and report, never approve, answer, give a verdict or file a ticket. Each finding's proposed ticket is filed only when you press File.
