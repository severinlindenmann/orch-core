# Ticket usage

Claude Code usage per ticket: what a ticket cost, how many output tokens each model wrote for it, and an estimate of how much of your weekly limit it used. Off until you enable it per workspace in **Workspace & addons**. It only reads files; it changes nothing and never edits Claude's settings.

- **Ticket page, Usage panel** (in the code column): the estimate at API list prices (Claude Code's own figure) per model, the weekly limit share, working time, lines added and removed, first and last message, and output tokens per model for the main session and for subagents.
- **Menu entry**: a row per limit, "5h" and "Week", each with a small meter, the percent and its reset ("resets in 4h49", "resets Mon 09:00"). Colour comes from the level (amber from 70 %, red from 90 %) and turns amber earlier when the pace sentence sees the limit fill before its reset. A window that has reset reads "5h — reset" in grey. When the last reading is older than the stale setting, the rows are greyed and say "as of 13:52". In a menu too narrow for the rows, one labelled chip shows the riskier limit ("Week 59 %"). The tooltip lists both limits and their resets. Needs orch API 2.8.
- **Usage page** (menu): limit cards (5-hour and weekly) with reset time and a pace sentence; output tokens by model per day (last 7 / 30 days or calendar weeks, local days, from every transcript on this machine, subagents included); how much of this week's output is tied to a ticket and output per ticket; limits history with real time spacing; the API-price equivalent per calendar week (booked on the day a session ended); then this week's table. Needs orch API 2.6 for the charts. Without the recorder the limit cards give way to the install line.

## How the numbers are made

- Claude's dir is `$CLAUDE_CONFIG_DIR`, else `~/.claude`. Transcripts are `projects/*/<session id>.jsonl`; subagents are `<session id>/subagents/agent-*.jsonl`. A ticket's sessions come from its `sessions` list. A session whose transcript is not on this machine shows as "not on this machine".
- A reply is logged several times while it streams; each message id is counted once, from its last line.
- The estimate is the cost record Claude Code writes when a session ends (so "session still running" until then). It is Claude's list-price estimate, not a bill.
- **Shared sessions:** when one session id is on more than one ticket, its main thread is not split. It shows once as "Shared orchestrator", and a ticket counts only the subagents whose description names its id (the exact ticket id). In a session with one ticket, subagents that name no ticket are shown as "other subagents".
- **Week share** is an estimate from the limits log: each rise of the weekly percentage is spread over the output tokens written in that interval, and each ticket gets the part that is its. A new reset time starts over. Without log data for the ticket's messages it says "unknown", never 0.
- The transcript format is Claude Code's own and changes between versions. Anything unreadable is skipped, and a failed read shows up as a failed fetch, not a broken page.

## Install the recorder (for the limits)

Claude Code only exposes the limit percentages to its status line, so a small script records them. orch never edits your Claude settings: you do it once.

1. Needs `jq`. Copy `recorder/statusline.sh` from this addon to `~/.claude/orch-usage/statusline.sh` and make it executable (`chmod +x`).
2. In `~/.claude/settings.json` add `"statusLine": {"type": "command", "command": "~/.claude/orch-usage/statusline.sh"}`. If you already have a status line, call this script from it instead.
3. The script appends one line to `~/.claude/orch-usage/limits.jsonl` whenever a percentage changes. Nothing else is stored.

## Settings

- **Limits log**: path of that file (default `~/.claude/orch-usage/limits.jsonl`).
- **Show the estimated cost** (on): switch off to hide every dollar figure.
- **Dim the menu limits after** (30 minutes): how old the last reading may be before the menu entry greys its rows.

Binaries: none. Data is refreshed in the background while Mission Control is open (or on Refresh); big transcripts are parsed there, never while a page renders.
