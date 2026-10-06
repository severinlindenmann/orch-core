# Ticket usage

Claude Code usage per ticket: what a ticket cost, how many output tokens each model wrote for it, and an estimate of how much of your weekly limit it used. Off until you enable it per workspace in **Workspace & addons**. The addon itself only reads files and never edits Claude's settings; only the human-run setup command below does, after you confirm.

- **Ticket page, Usage panel** (in the code column): the estimate at API list prices (Claude Code's own figure) per model, the weekly limit share, working time, lines added and removed, first and last message, and output tokens per model for the main session and for subagents.
- **Usage page** (menu): limit cards (5-hour and weekly) with reset time and a pace sentence; output tokens by model per day (last 7 / 30 days or calendar weeks, local days, from every transcript on this machine, subagents included); how much of this week's output is tied to a ticket and output per ticket; limits history with real time spacing; the API-price equivalent per calendar week (booked on the day a session ended); then this week's table. Needs orch API 2.6 for the charts. Without the recorder the limit cards give way to an info card with the setup command; a warning is kept for a Limits log you configured that is not there.

## How the numbers are made

- Claude's dir is `$CLAUDE_CONFIG_DIR`, else `~/.claude`. Transcripts are `projects/*/<session id>.jsonl`; subagents are `<session id>/subagents/agent-*.jsonl`. A ticket's sessions come from its `sessions` list. A session whose transcript is not on this machine shows as "not on this machine".
- A reply is logged several times while it streams; each message id is counted once, from its last line.
- The estimate is the cost record Claude Code writes when a session ends (so "session still running" until then). It is Claude's list-price estimate, not a bill.
- **Shared sessions:** when one session id is on more than one ticket, its main thread is not split. It shows once as "Shared orchestrator", and a ticket counts only the subagents whose description names its id (the exact ticket id). In a session with one ticket, subagents that name no ticket are shown as "other subagents".
- **Week share** is an estimate from the limits log: each rise of the weekly percentage is spread over the output tokens written in that interval, and each ticket gets the part that is its. A new reset time starts over. Without log data for the ticket's messages it says "unknown", never 0.
- The transcript format is Claude Code's own and changes between versions. Anything unreadable is skipped, and a failed read shows up as a failed fetch, not a broken page.

## Install the recorder (for the limits)

Claude Code only exposes the limit percentages to its status line, so a small script records them. `orch doctor` reports it as `usage-recorder` while this addon is enabled.

Run this in your own terminal (agents cannot run it):

```bash
orch addon setup ticket-usage
```

It shows what it will do and asks you to type `ticket-usage`, then copies the script to `~/.claude/orch-usage/statusline.sh` (under `$CLAUDE_CONFIG_DIR` if set), makes it executable and adds the `statusLine` below to your user-global `settings.json`, so it runs in every Claude Code session on this machine. If you already have a status line, it is never replaced: the command prints the one line to add to your script instead (after the script reads stdin into `$input`): `printf '%s' "$input" | ~/.claude/orch-usage/statusline.sh >/dev/null`.

By hand instead:

1. Needs `jq`. Copy `recorder/statusline.sh` from this addon to `~/.claude/orch-usage/statusline.sh` and make it executable (`chmod +x`).
2. In `~/.claude/settings.json` add `"statusLine": {"type": "command", "command": "~/.claude/orch-usage/statusline.sh"}`. If you already have a status line, call this script from it instead.
3. The script appends one line to `~/.claude/orch-usage/limits.jsonl` whenever a percentage changes. Nothing else is stored.

## Settings

- **Limits log**: path of that file (default `~/.claude/orch-usage/limits.jsonl`).
- **Show the estimated cost** (on): switch off to hide every dollar figure.

Binaries: none. Data is refreshed in the background while Mission Control is open (or on Refresh); big transcripts are parsed there, never while a page renders.
