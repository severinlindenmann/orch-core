import { SHORTCUT_DEFS, WORKSPACE_SWITCH } from '@/api/shortcuts'

// The guide's pages (markdown). Written for someone who has never seen orch: short, plain, specific.
// The Keyboard shortcuts page is generated from the shortcut registry (src/api/shortcuts.ts), never typed here.

export interface GuidePage {
  slug: string
  title: string
  markdown: string
}

const shortcutsPage = (): GuidePage => ({
  slug: 'shortcuts',
  title: 'Keyboard shortcuts',
  markdown: `# Keyboard shortcuts

Shortcuts do nothing while you type in a field or while a dialog is open.

A key written as two letters, such as \`g b\`, is a sequence: press the first, then the second within one second.

| Keys | What it does |
| --- | --- |
${[...SHORTCUT_DEFS.map((d) => [d.keys, d.label]), [WORKSPACE_SWITCH.keys, WORKSPACE_SWITCH.label]].map(([k, l]) => `| ${k.includes('`') ? `\`\` ${k} \`\`` : `\`${k}\``} | ${l} |`).join('\n')}
| \`Ctrl+K\` or \`Cmd+K\` | Open the search and command palette |

Press \`Esc\` to close the help sheet, a dialog or the palette.
`,
})

export const GUIDE_PAGES: GuidePage[] = [
  {
    slug: 'getting-around',
    title: 'Getting around',
    markdown: `# Getting around

Mission Control shows the work of one workspace: its tickets, the agents working on them, and what is waiting for you.

## The sidebar

- **Today**: what needs you now, which agents are at work, and what happened recently. Start here.
- **Board**: tickets as cards in columns by status.
- **Tickets**: every ticket in a table, with search and saved views.
- **Agents**: running agent sessions, and the grants that let them work.
- **Addons**: pages that addons add. Each one carries an orange **A**.
- At the top you switch workspace. At the bottom: settings, the grant status and who you are.

## Roles

Each person has one role per workspace. **Owner** and **maintainer** can approve and sign grants. **Member** can create tickets, comment, answer and sign a grant for their own agents. **Viewer** can only read.

## Search and commands

Press \`Ctrl+K\` (\`Cmd+K\` on a Mac) to find a ticket or run a command. Press \`?\` on any page for help about that page.
`,
  },
  {
    slug: 'tickets-and-sections',
    title: 'Tickets and sections',
    markdown: `# Tickets and sections

A ticket is one piece of work: a **feature**, a **bug**, a **chore**, a **spike** (a question to research) or an **epic** (a group of tickets).

## Status

A ticket is in one of six states: Backlog, Open, In progress, Waiting, Testing, Done. Drag a card on the Board to move it. Owners and maintainers can move tickets. Nobody can drag a ticket to **Done**: only a passed verdict does that.

## Sections

The text of a ticket is split into sections: Summary, Context, Requirements, Out of scope, Plan, Decisions, Verification and Current state. Which ones a ticket needs depends on its type. A chore skips Out of scope and Verification, an epic has no Plan, and a spike calls them "Questions to answer" and "Findings".

When you create a ticket, only the title and the Requirements are required. The other sections can follow, but must be filled before the plan can be approved.

## Finding tickets

Use the **Tickets** page to filter by status, type or label and to search the text. "Save view" keeps a filter for later; you can share it with the workspace.
`,
  },
  {
    slug: 'gates-and-approvals',
    title: 'Gates and approvals',
    markdown: `# Gates and approvals

A ticket passes three **gates**. Each gate asks for approval of what the ticket says at that moment.

1. **Requirements**: is it clear what to build?
2. **Plan**: is the way to build it agreed?
3. **Verify**: does the result meet the requirements?

## Who approves

Each workspace sets a policy per gate: who may approve, and how many approvals are needed. By default Requirements and Plan need one owner. Verify needs one reviewer who is not an assignee. An owner changes the policy in Settings.

## Signing

An approval is always signed by you, in a dialog that core draws, never by an addon. The dialog shows what you approve and a fingerprint of the text. Click **Sign with Touch ID** to confirm.

## Changed text

If the text of a gate changes after it was approved, the approval no longer matches and is marked invalid. The ticket shows why. The gate must be approved again.

## Verdict

At Verify the reviewer gives a verdict. The verdict signs the branch's head commit; a new commit after it voids it and the ticket goes back to Testing. A passed verdict is the only way a ticket reaches **Done**. Where the **code review** gate is on (Settings > Gates), a person also approves that same commit before it is done.
`,
  },
  {
    slug: 'agents-and-grants',
    title: 'Agents and grants',
    markdown: `# Agents and grants

An **agent** is a program such as Claude Code or Codex that works on tickets. It can only work while a person has given it a **grant**.

## Grants

A grant says: this person's agents may work in this workspace until a given time. You sign it for yourself with Touch ID: owners and maintainers for all tickets (up to 24 hours), members for the tickets they may work on (up to the workspace default, 8 hours). Owners can revoke any grant. It runs out by itself. **Revoke** ends it at once, and every agent under it stops. The grant status is shown at the bottom of the sidebar.

## What an agent does

- It **claims** a ticket. Only an agent claims or releases a ticket; people do not.
- It takes a **lease** on a task while it works on it, so two sessions do not do the same task.
- It writes log lines, finishes tasks with a receipt (the exit code of the check), and asks questions.

## Limits

An agent can never approve a gate, answer for you, sign a grant, enable an addon or use a terminal. Those stay with people.

## Agents page

The page lists each session, what it holds (claims and leases), what it waits on and when its grant ends. A claim ends when its grant ends.
`,
  },
  {
    slug: 'questions',
    title: 'Questions',
    markdown: `# Questions

A question is how an agent, or a teammate, asks a person for a decision without leaving the ticket.

## What a question has

- **Who it is for.** Only that person can answer it. An owner can answer for anyone.
- **Options**, with one recommended. You pick one, or write your own answer.
- **Blocking or not.** A blocking question stops the agent until it is answered. A question that is not blocking lets the work go on.

## Where you see them

Open questions for you appear on **Today** under "Needs you", and on the ticket under the Questions tab. When you answer, core opens a signing dialog; the answer is recorded with your name.

## Asking

Anyone who is not a viewer can ask a question on a ticket: use the Questions tab, or the command palette (\`Ctrl+K\`, then "Ask a question").
`,
  },
  {
    slug: 'addons',
    title: 'Addons and the orange A',
    markdown: `# Addons and the orange A

Addons add pages, panels and cards to Mission Control. Everything an addon puts on screen carries a small orange **A** and an orange outline. Orange is used for nothing else, so you always see what comes from an addon and what is core.

## What an addon can and cannot do

An addon describes what to show; core draws it. An addon cannot approve a gate, answer a question, sign a grant or give a verdict. When an addon needs a decision from you, core shows it on Today and asks you to sign.

## Capabilities

Some addons need extra powers, called capabilities: for example **pty** (open a terminal) or **spawn_agent** (start an agent). An owner grants them in **Settings > Addons**, signed with Touch ID, for one version. When the addon is updated, the owner must grant again before it works.

## Managing addons

In **Settings > Addons** an owner can install, enable, disable, update and remove addons. A disabled addon disappears from the sidebar and from tickets. Viewers see addon pages read-only.
`,
  },
  shortcutsPage(),
  {
    slug: 'simulated',
    title: 'What is simulated in this mockup',
    markdown: `# What is simulated in this mockup

This is a clickable mockup. Nothing leaves your browser. The buttons really change the demo data, but these parts are pretend:

- **Touch ID.** The sign dialog waits about half a second and then signs. No fingerprint is read.
- **Terminals.** The terminal is a fake shell that understands a few commands. No process runs on your computer.
- **GitHub.** Pull requests, issues and checks come from sample data. Nothing is read from or sent to GitHub.
- **The relay.** The link between this dashboard and your devices is not built. Settings > Relay & devices simulates it: connecting, pairing a phone with a mock QR code, removing a device and the sync queue. The dot beside the workspace name turns green while the simulated link is on. The Drop addon's files and links are simulated too.
- **Agent runs.** When you start an agent it plays a short script: it claims the ticket, takes a task, logs a line, finishes with a receipt and asks you a question. No model is called.
- **Time.** The clock is fixed at 2026-10-09 11:30 UTC and moves forward only while the page is open. Grants and claims run out against that clock.
- **Demo data.** The workspaces, tickets and people are made up. Your changes are kept in this browser. The **Demo data** pill in the top bar resets everything, and switches between the Normal demo and a **Busy day** with a lot of tickets, agents and addon data.
`,
  },
]
