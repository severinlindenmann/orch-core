# orch

**Let coding agents do the work. Keep every decision yours.**

orch is a way of working with coding agents: every piece of work is a ticket, every ticket follows a fixed path, and a person signs each step that matters. Agents file tickets, write the requirements and the plan, do the work and attach the evidence. You approve the requirements and the plan and give the final verdict. orch checks the signatures, so an agent can't approve its own work.

It works with Claude Code, Codex and any other agent that can run a shell command. Tickets are plain files in your workspace. Mission Control (desktop) and orch mobile (iPhone) show you what's waiting for you.

<p align="center">
  <img src="docs/images/v2/desktop-today.jpg" alt="Mission Control, Today: connections to fix, a blocking question with a recommended answer, three approvals and a verdict that wait for you, and which agents are working" width="900">
</p>

> **Status: v2 in development.** This branch (`develop`) is the v2 rebuild: core, host, dashboard and relay are merged, and the iPhone app is in TestFlight. v1 is the stable release on [`main`](https://github.com/severinlindenmann/orch-core/tree/main). The full design is in [docs/architecture/orch-v2.md](docs/architecture/orch-v2.md).
> Website: **[severinlindenmann.github.io/orch-core](https://severinlindenmann.github.io/orch-core/)**

## Why

Agents are fast, confident and happy to approve their own plan. After a week on another project, I came back and couldn't tell where things stood, because the agents had done the work, not me. orch came out of four problems I kept running into:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/v2/problems-dark.png">
  <img src="docs/images/v2/problems-light.png" alt="Four problems and what orch does about them. A: I lost control: open tasks, questions and the last status in one place. B: Not what we agreed: signed requirements and plan. C: Done isn't done: a checklist with evidence. D: Same discussion again: ticket history and context.">
</picture>

## How it works

Everything flows into the core. A request comes in, in the requester's own words. The agent gets the keys and the knowledge it needs, follows your skills and rules, and writes every step into the ticket. People answer questions and sign the gates. The results go back to your tracker, docs, code host and data platform, and every finished ticket adds to what the next one can draw on.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/v2/flow-dark.png">
  <img src="docs/images/v2/flow-light.png" alt="Request, keys and knowledge flow into orch core from the left, skills and rules from above. Inside the core: understand, you sign, plan, you sign, build, prove, you sign. People sign every gate. Results are written back to the tracker, docs, code host and data platform, and feed the knowledge base.">
</picture>

Each ticket follows the same path, with a gate before every next step:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/v2/gates-dark.png">
  <img src="docs/images/v2/gates-light.png" alt="Ticket, then gate 1 requirements and acceptance criteria (you sign), plan, gate 2 plan approved (you sign), build and prove, gate 3 verdict (you sign).">
</picture>

- **Gates that hold.** Approvals, answers and verdicts are bound to the exact text you saw and signed with your key. Agents get no signing commands, and orch refuses a gate without a person's signature. On the phone every signature needs Face ID; on the Mac, Touch ID per signature and agents under their own OS user are in progress ([enforcement tiers](docs/architecture/orch-v2-harnesses.md#3-enforcement-tiers)).
- **Agents that ask instead of guessing.** `orch ask` parks a question with options and a recommended answer, and `orch wait` blocks until you reply, at your desk or on your phone.
- **Evidence, not claims.** Acceptance criteria, a task list ticked off with proof, and artifacts (screenshots, test reports, PRs) per criterion, before a ticket reaches your verdict.
- **Memory that survives a holiday.** Every change is an appended, signed event. The current-state note on each ticket tells you where it stands and what's next.

## Mission Control

The desktop dashboard is where you work through what's waiting for you: questions first, then approvals and verdicts. Each prompt shows exactly what you are about to sign.

| | |
|---|---|
| ![A ticket in progress: requirements and plan approved, verify pending, the current-state handoff note and a question widget that compares DATE with TIMESTAMP](docs/images/v2/desktop-ticket.jpg) | ![Approve plan: the prompt shows the task list exactly as signed](docs/images/v2/desktop-approve.jpg) |
| **Ticket page.** Gates at a glance, the handoff note, questions, evidence and history. | **Approve.** The exact text you sign. Only core can show this prompt, not an addon. |
| ![The board: tickets grouped by epic across backlog, open, in progress, waiting and testing, with GitHub issues alongside](docs/images/v2/desktop-board.jpg) | ![Give verdict: acceptance criteria evidenced, the exact commit the verdict signs, pass or send back](docs/images/v2/desktop-verdict.jpg) |
| **Board.** Epics as swimlanes, and addons add their own lanes. | **Verdict.** Signs one commit. A new commit voids it. |

<sub>Screenshots use the simulated "Acme energy data" demo workspace.</sub>

## orch mobile

Answer questions and sign gates from your iPhone. One QR scan joins a workspace, logging out wipes it from the phone, and every signature needs Face ID. The device key never leaves the Secure Enclave.

<table>
  <tr>
    <td><picture><source media="(prefers-color-scheme: dark)" srcset="docs/images/v2/mobile-home-dark.jpg"><img src="docs/images/v2/mobile-home-light.jpg" alt="Home: your workspaces, what needs you, and Drop" width="200"></picture></td>
    <td><picture><source media="(prefers-color-scheme: dark)" srcset="docs/images/v2/mobile-tickets-dark.jpg"><img src="docs/images/v2/mobile-tickets-light.jpg" alt="A workspace's tickets with their status" width="200"></picture></td>
    <td><picture><source media="(prefers-color-scheme: dark)" srcset="docs/images/v2/mobile-question-dark.jpg"><img src="docs/images/v2/mobile-question-light.jpg" alt="A question from an agent with its answer options" width="200"></picture></td>
    <td><picture><source media="(prefers-color-scheme: dark)" srcset="docs/images/v2/mobile-sign-dark.jpg"><img src="docs/images/v2/mobile-sign-light.jpg" alt="The sign sheet: what you are signing, then Sign and send" width="200"></picture></td>
  </tr>
  <tr>
    <td align="center"><sub>Workspaces</sub></td>
    <td align="center"><sub>Tickets</sub></td>
    <td align="center"><sub>A question</sub></td>
    <td align="center"><sub>Sign and send</sub></td>
  </tr>
</table>

## Architecture

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/v2/architecture-dark.png">
  <img src="docs/images/v2/architecture-light.png" alt="On your machine: agents call the orch CLI, which talks over a socket to the host (orch serve), the only process that holds keys; the host writes the workspace files. Mission Control and other addons plug into the host. Through orch-relay, which stores ciphertext only, the host reaches orch mobile and other workspaces. orch-publish serves shares and apps over HTTPS, signed by the workspace.">
</picture>

| Piece | What it does | Where |
|---|---|---|
| **`orch` CLI** | Agent-first commands with terse output: `new`, `claim`, `task`, `ask`, `artifact`, `submit`. Any agent with a shell can use it. | [`plugins/orch-core`](plugins/orch-core) |
| **Workspace** | Plain files: `config.json`, `keys.jsonl` and, per ticket, `ticket.json`, `body.md`, `events.jsonl` and `artifacts/`. Every change is an appended, signed event. | your repo or folder |
| **Host** (`orch serve`, `orch host`) | The only process that loads keys (OS keychain or Secure Enclave). It enforces gates, roles and policies, and agents reach it through a narrow local socket. | [`plugins/orch-core`](plugins/orch-core) |
| **Mission Control** | The desktop dashboard. It's an addon itself. | [`addons/dashboard`](addons/dashboard) |
| **Addons** | GitHub reviews and issues, Databricks, wiki, terminals, schedules, quick tasks, or your own. Each one runs only after you trust that exact version. | [`addons/`](addons) |
| **orch-relay** | Directory, relay, Drop (files) and push. It stores and forwards ciphertext only, and runs as a public instance or self-hosted inside a company. | [orch-relay](https://github.com/severinlindenmann/orch-relay) |
| **orch mobile** | Native iPhone app: questions, approvals and verdicts with Face ID, push notifications and Drop. | [orch-mobile](https://github.com/severinlindenmann/orch-mobile) |
| **orch-publish** | Shares and small apps over HTTPS, in the workspace's own namespace and signed by the workspace. | [orch-publish](https://github.com/severinlindenmann/orch-publish) |

Workspaces can hand tickets to each other: yours, a colleague's or one on a client's VM. An incoming ticket lands in the inbox as untrusted data until you accept it.

## Works with your agent

orch doesn't depend on one harness. Every command is defined once, and gates and signatures are enforced by the CLI and the host, not by a hook in the agent.

| | Claude Code | Codex CLI and others |
|---|---|---|
| Install | plugin marketplace | `uv tool install`, then `orch init` writes `AGENTS.orch.md` |
| Instructions | `AGENTS.orch.md` and skills | `AGENTS.orch.md`, plus skills where the harness supports `SKILL.md` |
| Gates and signatures | enforced by orch | enforced by orch |
| Early refusal of hand edits | guard hook (an optional second layer) | detected by `orch check` and `orch doctor`, or prevented by running agents under their own OS user |

Details: [harnesses and enforcement tiers](docs/architecture/orch-v2-harnesses.md).

## Try the v2 preview

v2 is a developer preview. Expect changes, and use [v1 on `main`](https://github.com/severinlindenmann/orch-core/tree/main#install) for day-to-day work. You need [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
git clone -b develop https://github.com/severinlindenmann/orch-core.git
uv tool install ./orch-core/plugins/orch-core

cd your-project
orch init              # creates the workspace and writes AGENTS.orch.md
orch host enable       # starts the host as a per-user service
orch help work         # what an agent reads first
```

The dashboard mockup with simulated data runs on its own: `cd addons/dashboard && npm ci && npm run dev` (Node 24).

## Documentation

- [orch v2 design](docs/architecture/orch-v2.md): decisions, threat model, keys, relay, mobile, phases
- [The minimal core](docs/architecture/orch-v2-core.md) and the [ticket format](docs/architecture/orch-v2-ticket-format.md)
- [Host process](docs/architecture/orch-v2-host.md): socket API, signing, custody and worker isolation
- [Harnesses and enforcement tiers](docs/architecture/orch-v2-harnesses.md)
- [Dashboard addon](addons/dashboard/README.md)

## Development

From `plugins/orch-core`:

```bash
uv sync
uv run pytest -q -m "not slow"
```

The diagrams are drawn from [`docs/images/v2/src/diagrams.html`](docs/images/v2/src/diagrams.html), and `node render.cjs` re-renders the PNGs. See [CONTRIBUTING.md](CONTRIBUTING.md) for the checks CI runs. Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md), never in a public issue.

## License

orch is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE): free for noncommercial use. Commercial use needs a separate license from the author.
