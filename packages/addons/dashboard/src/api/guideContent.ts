import { SHORTCUT_DEFS, WORKSPACE_SWITCH } from './shortcuts'
import type { HelpRoute } from './guide'

// The guide's pages (markdown), v2 text. Fixed text in the web bundle: the same for everyone, no host side. Written for
// someone who has never seen orch: short, plain, no ids. Only what the dashboard does today; later things are said to be
// "coming later". The Keyboard shortcuts page is generated from the shortcut registry (shortcuts.ts), never typed here.

export interface GuidePage {
  slug: string
  title: string
  markdown: string
}

const shortcutsPage = (): GuidePage => ({
  slug: 'shortcuts',
  title: 'Keyboard shortcuts',
  markdown: `# Keyboard shortcuts

Shortcuts do nothing while you type in a field or while a dialog is open. A key written as two letters, such as \`g b\`, is a sequence: press the first, then the second within one second.

| Keys | What it does |
| --- | --- |
${[...SHORTCUT_DEFS.map((d) => [d.keys, d.label]), [WORKSPACE_SWITCH.keys, WORKSPACE_SWITCH.label]].map(([k, l]) => `| ${k.includes('`') ? `\`\` ${k} \`\`` : `\`${k}\``} | ${l} |`).join('\n')}
| \`Ctrl+K\` or \`Cmd+K\` | Open the search and command palette |

Press \`Esc\` to close the help sheet, a dialog or the palette.
`,
})

export const GUIDE_PAGES: GuidePage[] = [
  {
    slug: 'start-here',
    title: 'Start here',
    markdown: `# Start here

orch helps you and your coding agents work on the same list of tickets, with you in charge. An agent can do the work. Only you can approve it.

This dashboard is where you see the tickets, watch the agents and approve what needs a person.

## Your first time

Setting up happens once per folder. In a terminal in your project folder, run \`orch setup\`. It opens a page in your browser and walks you through a few steps: who you are, what agents may do, how you want to approve things, your secret words, and a last look before it creates your workspace.

## Your secret words

The setup shows you a set of **secret words** and a **recovery code**. They protect your approval key. Write them down and keep them somewhere safe, away from this computer. You type the words each time you approve something in the terminal. If you forget them, the recovery code is the only way back in. Nobody else can reset them for you.

If setup stops half way, run \`orch setup\` again in the same folder. It continues where it left off, and shows you new secret words if the old ones were not saved.

## Where to go next

Open **Today** first. Press \`?\` on any page to see help for that page.
`,
  },
  {
    slug: 'today-board-tickets',
    title: 'Today, Board and Tickets',
    markdown: `# Today, Board and Tickets

The sidebar on the left takes you between the main pages.

## Today

Start here each day. **Needs you** lists what is waiting for a person: a ticket to approve, a question an agent asked. Next to it you see which agents are at work. If nothing needs you, you can leave.

## Board

Every ticket is a card, in columns by status: from backlog, through open and in progress, to done. Click a card to open the ticket. Moving a card between backlog and open is a change, so you approve it like any other.

## Tickets

The same tickets as a table. Use the search box to find one, and the filters to narrow it down. Saved views keep a filter you use often.

## New ticket

Press \`c\` or choose **New ticket**. Give it a clear title and say what done looks like. A ticket with clear checks is much easier for an agent to finish.
`,
  },
  {
    slug: 'a-ticket',
    title: 'A ticket and quick tasks',
    markdown: `# A ticket and quick tasks

Click any ticket to open its page.

## What you see

- **Overview**: the title, what it is for, who works on it and what is left to do.
- **Questions**: things an agent asked you. Answer them here and the agent carries on.
- **Changes**: the work the agent has done so far.
- **History**: everything that happened, in order, with who did it and when.
- **Actions** menu: what you can do next, such as approve, comment, move, or start an agent.

## Gates

A ticket passes through gates. A gate is a point where a person must say yes before the work goes on, for example approving the plan or accepting the result. An agent cannot pass a gate for you. A gate that is waiting for you also shows on **Today**.

## Quick tasks

Quick tasks (in the sidebar) are for small jobs you want done fast, without planning. Add one with a title and, if you like, a few simple checks. You sign once at the start. A quick task stays small: it has limits on how many files and changes it may touch, which you can set on the page. Ones that grow past the limits are not closed for you automatically yet. Automatic closing is coming later.
`,
  },
  {
    slug: 'approving',
    title: 'Approving things',
    markdown: `# Approving things

Anything that changes a ticket or gives an agent power needs your approval, called a signature. A signature proves that you, and not an agent, said yes.

## What you review

Before you approve, the dashboard shows exactly what you are approving, in plain words. Read it. If it does not look like what you meant, cancel. Nothing changes until you approve.

## One click

You approve in the dashboard with one click. Read the review, press Approve, then press the button again after a second (it waits that long on purpose). If One click is off, turn it on in Settings.

## If it will not sign

The dashboard explains what needs attention. Cancelling ends that attempt; pressing the action again creates a fresh request.
`,
  },
  {
    slug: 'agents',
    title: 'Agents and their access',
    markdown: `# Agents and their access

An agent works on a ticket for you, but only while you have given it access, and only for a time you chose.

## The access pill

At the top of the page, a small pill shows when agent access ends, for example "Agent access until 17:30". Three buttons:

- **Extend**: give agents more time. You choose how long, then approve.
- **Stop**: end all your agents' access right away. You approve each stop.
- **Start again**: appears after a stop, to give access back.

Access ends by itself at the time shown. A reminder appears shortly before. Choose the length that fits the job. An owner sets the longest time allowed under Settings, General.

## Start agent

On a ticket, choose **Start agent** in the Actions menu. You see what will run before it starts. If agents have no access yet, you approve that first, then the agent starts in a terminal at the bottom of the page.

## Command window

The terminal area shows each running agent. Pick one to watch its work, and write to it if it needs help. **Stop** ends that one agent.

## Agents page

The **Agents** page lists agent sessions and the access you have given. A person with the role Viewer can read it but not change anything.
`,
  },
  {
    slug: 'more-tools',
    title: 'Work copies, Activity and add-ons',
    markdown: `# Work copies, Activity and add-ons

Some pages are added by add-ons, small extras that an owner can switch on. You see a page only if it is on in your workspace and your host has it.

- **Work copies**: each agent works in its own copy of your project, so agents do not get in each other's way. This page lists those copies and says which ticket each belongs to.
- **Activity**: a time line of what happened across the workspace: who did what, and when. Times are in your local time.
- **Repos**: the project folders this workspace works on (see Repositories).
- **Shares**: links to ticket files, from the Publish add-on (see Shares and Publish).
- **Usage**: how long agents worked on each ticket (time and runs; tokens and cost say "Not reported").
- **Files**: look through the workspace's files and their history, read only, for owners.
- **Add-ons**: in **Settings, Add-ons**, an owner switches add-ons on or off. Turning one on shows what it may do and asks for approval first. An orange **A** marks anything an add-on added.

If you do not see one of these yet, it is not available on your host. Do not worry, nothing is missing from the core tool.
`,
  },
  {
    slug: 'settings',
    title: 'Settings',
    markdown: `# Settings

Open **Settings** at the bottom of the sidebar.

- **General**: the workspace name and the rules agents follow, such as the longest time an agent may work for you.
- **Members**: who is in the workspace and their role. **Owner** and **maintainer** can approve and manage things. **Member** can create tickets, comment and answer. **Viewer** can only read.
- **Gates**: which steps need a person, and the allowed ways to approve (see Approving things).
- **Grants**: the access that agents currently have, and when it ends.
- **Records**: where the history of your tickets is kept, when your host offers it.
- **Devices**: phones connect through your company hub. With a hub, **Add a phone** opens the hub's Phones page; without one, there is nothing to set up here.

Changing a setting is a change like any other: you review it and approve it.
`,
  },
  {
    slug: 'repos',
    title: 'Repositories',
    markdown: `# Repositories

A repository is a project folder on this computer that your tickets work in. The workspace keeps a list of them.

## Add one

Open **Settings, General**, find **Repositories** and press **Add repository**. Give it a short name, then the full path of its folder, for example \`/Users/you/code/billing\`. orch checks the folder right away: it must exist, be a git folder, and not be your home folder or orch's own data. If something is wrong the form tells you before you sign anything. Only an owner can add, change or remove a repository.

## Use it on a ticket

On a ticket, change the ticket and link the repository. A linked ticket can get its own worktree (see Work copies).

## Remove one

Removing a repository only takes it off the list. Its folder and files stay on your disk. If tickets still link it, you are told which ones before you sign.

orch does not download repositories yet: clone the project yourself first, then add its folder.
`,
  },
  {
    slug: 'worktrees',
    title: 'Work copies',
    markdown: `# Work copies

A worktree is a separate copy of a repository for one ticket. An agent works in it, so the agent never touches your own copy and agents on other tickets do not get in its way.

## Make one

The ticket must link a repository first. Open the ticket and press **Add worktree**. It appears under **Work copies** in the sidebar with the ticket it belongs to. When you start an agent on the ticket, it works there.

## What you see

The **Work copies** page lists every copy, the ticket, and whether there are changes that are not saved yet. The ticket page shows the same state.

## Remove one

Press **Remove worktree** on the page or the ticket and confirm. You do not need to sign for this. The folder is deleted, but the branch and everything saved on it stay. A folder with changes that are not saved in git is not removed.

Work copies come from an add-on: you see them only when it is switched on in **Settings, Add-ons**.
`,
  },
  {
    slug: 'shares',
    title: 'Shares and Publish',
    markdown: `# Shares and Publish

With the **Publish** add-on you can give someone a link to files of a ticket. Nothing leaves this computer until you review and sign.

## Set up once

In **Settings, Add-ons** switch Publish on, then press **Set up** and enter the two addresses of your publish server: where files are sent, and where links open. Press **Check now** to see whether the server knows this workspace. If not, its admin must allow it once.

## Share files

On a ticket, in the **Shares** box, press **Share files**. Tick the files, choose a **secret link** (works until it ends) or a **show-once link** (works for one or a few opens), and how long it lasts. Read the list, then **Review and sign**. You see the link **once**: copy it before you press Done. orch keeps no copy.

## The Shares page

**Shares** lists every link you made: its kind, when it ends, how often it was opened and its state (Live, Used up, Expired or Revoked). **Revoke** stops a link at once.

An agent can only prepare a share. A person always approves it in the dashboard.
`,
  },
  {
    slug: 'later',
    title: 'Coming later',
    markdown: `# Coming later

Some things you may have heard of are not ready yet. They are not hidden: they simply do not work today.

- **Relay and phone**: approving from your phone, and reaching the dashboard away from this computer.
- **Touch ID and security keys** for approving.
- **Quick tasks that close themselves** when they finish cleanly.

Anything on a page marked "coming" is a plan, not a feature. This guide only lists what works now.
`,
  },
  shortcutsPage(),
]

/** Which page the `?` sheet shows on which route (first match wins). */
export const GUIDE_ROUTES: HelpRoute[] = [
  { match: '/', page: 'today-board-tickets' },
  { match: '/board', page: 'today-board-tickets' },
  { match: '/tickets*', page: 'today-board-tickets' },
  { match: '/ticket/*', page: 'a-ticket' },
  { match: '/quick', page: 'a-ticket' },
  { match: '/agents', page: 'agents' },
  { match: '/settings*', page: 'settings' },
  { match: '/addon/*', page: 'more-tools' },
  { match: '/worktrees*', page: 'worktrees' },
  { match: '/shares*', page: 'shares' },
  { match: '/activity*', page: 'more-tools' },
  { match: '/usage*', page: 'more-tools' },
  { match: '/explorer*', page: 'more-tools' },
  { match: '/repos*', page: 'repos' },
]
