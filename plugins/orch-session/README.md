# orch-session

Shows the orch tickets **this session** holds, inside Claude Code. Nothing else: no backlog, no queue.

Everything leads with whose move it is, as orch-core says it (`move` in `orch show --json`, the dashboard's own rules) and in its design system's colours: pink ● only for **your move** (answer a question, approve a plan, re-approve a changed gate, do your task, give the verdict), blue ◐ while the agent works, amber ▲ when the ticket is blocked, red ✕ when orch cannot be read. Every status is an icon and a word, never colour alone.

- **Status line**: `◐ L-0001 working  ● L-0002 Answer Q1`, one entry per claimed ticket.
- **Band above the prompt**: one row with the move that matters most, yours first: `● L-0002 0/1 Your move: Answer Q1 · Which one?`.
- **`/orch`**: a pane with one card per ticket: whose move and what, an open question with its options, the recommended one and the `orch answer` command to run in your own terminal, every task (doing and blocked first), the epic it belongs to, the last verdict and the PR.
- **Toasts** on hand-overs only: your move starts or ends, the ticket is blocked, a verdict, a claim gained or lost.

It only reads: `orch list --mine --json` with the session's id and `orch show <id> --json` for each ticket. It refreshes after every `orch` command the agent runs and every 20 seconds, one refresh at a time, without holding up the tool call. A refresh in which any read fails keeps the last complete state and says so in the pane. It never runs a command that writes or that only a human may run; it shows the `orch answer` command, it does not run it.

**What it shows.** `orch list --mine` lists only the claims held by this session's own id, so a fresh `claude` shows nothing until the agent runs `orch claim`. Epics cannot be claimed: an epic appears only as "in epic DEMO-0031" on its claimed children, without progress of its own (open it in Mission Control for that). In a folder without an orch workspace, `/orch` shows a short toast instead of a pane, and the refresh slows to every 5 minutes.

The status line is drawn by Claude Code, which currently styles every plugin status like a warning (`⚠ orch-session: ...`) and ignores plugin colours there. That is the host's behaviour, so the line is kept short.

The plugin has no rules of its own for whose move it is. An orch that predates `move` (orch-core schema below 1.4.0) shows "Update orch-core" on each ticket and an amber note in the pane, never a guess.

## Install

Needs the `orch` CLI on the PATH, orch-core with schema 1.4.0 or newer (see orch-core's README, "The CLI in your own terminal"), and a Claude Code build with function-hook plugins (early access; the API may change between releases).

```bash
claude plugin install orch-session@orch-core
```

It is a separate plugin on purpose: if a Claude Code update breaks the hooks API, orch-core's guard and session-start hooks keep working.

## Develop

```bash
node --test plugins/orch-session/test/*.spec.ts   # the logic, against real orch output; no claude CLI needed
claude plugin validate plugins/orch-session
claude plugin test plugins/orch-session           # the hooks inside the engine
claude --plugin-dir plugins/orch-session          # load it into a session
```

The fixtures in `test/fixtures` are real orch-core output. After a change to what `orch list` or `orch show` print, make them again (CI also runs the tests against freshly made ones):

```bash
uv run --project plugins/orch-core python plugins/orch-session/test/make_fixtures.py plugins/orch-session/test/fixtures
```
