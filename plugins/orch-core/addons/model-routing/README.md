# Model routing

Start each agent session on a model chosen by its **Start agent mode**, give its subagents a model, and move a ticket one tier up when its verify keeps failing. Off until you enable it per workspace in **Workspace & addons**. While it is off, and while it is on with nothing set, a session starts exactly as it did before (same command, same environment).

orch launches sessions (in your terminal, or in Terminals), but the model is whatever the harness defaults to. Planning and refining want the strongest model; most building does not. This addon moves that split from habit into settings. It only chooses and passes what Claude Code already provides (`--model`, aliases, `opusplan`, `CLAUDE_CODE_SUBAGENT_MODEL`); it never starts anything itself and never changes a ticket.

## Settings (all empty until you set them)

- **Light, Standard, Strong**: three tiers, each a Claude Code model name. Aliases (`opus`, `sonnet`, `haiku`, `opusplan`, `sonnet[1m]`) are recommended: a harness update that brings new models then needs no change in orch. Light is optional; a Light start without a Light model uses Standard. A tier without a model starts on the harness default and the Start agent box says so.
- **A tier per Start agent mode**: Refine, Work on ticket, Fix failing checks, Continue after feedback. `none` leaves that mode on the harness default. **Continue** defaults to `same`, the tier of the last start on that ticket (Standard when there was none). Work may use `opusplan`: plan mode on Opus, execution on Sonnet.
- **Subagent model**: one model for the subagents of every session, passed as `CLAUDE_CODE_SUBAGENT_MODEL` in the launched process's environment, never in the dashboard's.

Settings are saved per user and workspace by a human on Workspace & addons. An agent cannot edit them (they are not in the workspace's `orchestrator/config.json`).

## What you see

- The Start agent box shows one line per mode and harness: `Model · refine runs on strong: Strong (opus); subagents on haiku`, and the command with the model argument and variables in it. The preview and the launch use one resolver, so the command shown is the command run.
- A name that is not a model name (spaces, a leading `-`) stops the launch with a sentence. A name the harness does not know ends the session at once; started in Mission Control (tmux) the session is checked 1.5 seconds after it starts and reported as "ended right after it started on model X", with a hint to run the shown command in a terminal for the harness's own message. A normal terminal window shows the harness error itself.
- Mission Control tiles and the session page show **Started on opus (Strong)**. The requested model is also in the session's environment as `ORCH_MODEL` (and `ORCH_MODEL_TIER`), which orch records in the ticket's `sessions` when the agent claims it, so usage can be compared before any saving is claimed.
- A warning appears while `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` or `ANTHROPIC_DEFAULT_OPUS_MODEL`, `_SONNET_MODEL` or `_HAIKU_MODEL` is set where `orch serve` runs: they silently change what a tier means.

## Escalation (a human's choice, never automatic)

- **Next start on the Strong model**: a yes/no on every ticket (new-ticket form, approve card, ticket page, `orch addon ticket-option set <ticket> model-routing/strong_next on`). It applies to the next session you start on that ticket, then turns itself off. A session that is running keeps its model.
- **A task that failed its verify in two separate sessions** (`orch task done --run` receipts written by two different agent sessions, task still open) gets one decision card under "From addons", once per task. Choosing **Next start on <tier>** makes the next session you start on that ticket run one tier above the last start, and adds one sentence to its prompt naming the failing receipt artifact, so the new session reads the log. **Not now** hides the card until another session fails the task. The card shows the last lines of the log. Nothing starts until you press Start agent.

## Not in this version

Per-task tiers chosen by the planner, rules that force subagent delegation, generated subagent definition files, agent-initiated launches (see the AI Factory issues #31 and #40), other harnesses as routing targets (Codex, Copilot: they start on their own default), and guard enforcement of models.

## How it plugs in

The addon declares the `launch` capability (addon API 2.7, ADDONS.md): `launch(req, ctx)` returns a `LaunchPlan` that core checks and applies when Start agent previews or starts a session. The same hook serves a terminal and Terminals. It needs no binaries and no environment variables.
