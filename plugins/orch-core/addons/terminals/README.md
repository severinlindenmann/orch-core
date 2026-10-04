# Terminals

Run, watch and type into agent sessions in Mission Control. Off until you enable it per workspace in **Workspace & addons**.

While it is enabled (and tmux is installed):

- the Start agent box has **Open in Mission Control**: the agent runs detached in orch's own tmux server (`tmux -L orch`);
- **Terminals** in the menu shows every such session started in this workspace as a live tile, plus **New scratch session**;
- one session opens full size: **Watch** sends nothing, **Type** sends your keys; key buttons, a reply field and **End session…**;
- Today shows a tile with how many run.

The pages and the live screens are part of orch-core, because addons render widgets only; this addon is their switch and counts the sessions. When it is off, the menu item, the button and every `/terminals` page are gone.

## Needs

- `tmux` on PATH (`brew install tmux`).

Setup checks (Workspace & addons, `orch doctor`) report `tmux` and `terminals-cli` while it is enabled: whether tmux and the agent CLI are installed.

## Settings

- **Agent CLI**: what Terminals runs, for scratch sessions and for Open in Mission Control. Claude Code only for now.
- **Start agents in Mission Control by default** (on): Open in Mission Control is the primary button in the Start agent box, and Open in terminal stays as the second one. Off: the other way round.

## Safety

A terminal in the browser is a shell as you. Every Terminals route is behind the dashboard's token cookie, answers only to a request from this machine with a loopback Host (never over `orch serve --lan`), and changes need a same-origin request. Agents may not run `tmux -L orch` (the guard refuses it), and agents started here run without `$TMUX`, so they cannot reach the other sessions through tmux. orch's tmux server counts as an agent harness, so nothing inside a session can make a human decision: those stay in the dashboard's forms or your own terminal.
