// The review tour's scenarios: the same list, in the same words, as "Scenarios" in REVIEW.md (a test keeps the two
// in step). Each step may say where "Go" takes you: a page, as which person, in which workspace, and on which demo
// dataset. Without `viewer` / `workspace`, Go puts you back as Severin in DEMO, so every step starts from a known place.

export type TourPerson = 'p_sev' | 'p_mara' | 'p_tom'
export type TourDataset = 'normal' | 'busy'

export interface TourGo {
  /** A route of the app, e.g. `/settings/addons` or `/ticket/DEMO-0043`. */
  path: string
  /** Who you look at it as (default Severin, the owner). */
  viewer?: TourPerson
  /** Which workspace (default DEMO). */
  workspace?: 'DEMO' | 'INT' | 'CLI'
  /** The step needs this demo dataset; Go asks before switching (a switch discards demo changes). */
  dataset?: TourDataset
}

export interface TourStep {
  /** Stable id for the tick ("6.4"). */
  id: string
  title: string
  /** What to do and what to look for. */
  detail?: string
  go?: TourGo
}

export interface TourScenario {
  n: number
  title: string
  summary: string
  steps: TourStep[]
}

export const PERSON_NAME: Record<TourPerson, string> = { p_sev: 'Severin', p_mara: 'Mara', p_tom: 'Tom' }

const S = (n: number, title: string, summary: string, steps: Omit<TourStep, 'id'>[]): TourScenario => ({
  n,
  title,
  summary,
  steps: steps.map((s, i) => ({ id: `${n}.${i + 1}`, ...s })),
})

export const SCENARIOS: TourScenario[] = [
  S(1, 'Owner morning', 'Severin starts the day: read, answer, approve, decide, move a card.', [
    { title: 'Read what needs you', detail: 'Today: the groups, the oldest and blocking items first, the Agents column.', go: { path: '/' } },
    { title: 'Answer a question on DEMO-0043', detail: 'Questions tab: pick an option, Send answer, sign.', go: { path: '/ticket/DEMO-0043' } },
    { title: 'Approve a plan gate', detail: 'Approvals group on Today: Review, read what you sign, Approve plan.', go: { path: '/' } },
    { title: 'Decide an addon decision', detail: 'From addons group: the failed build of Publish. Core asks you to sign.', go: { path: '/' } },
    { title: 'Move a card on the Board', detail: 'Drag a card or press m on it. Done is refused: only a verdict gets there.', go: { path: '/board' } },
    { title: 'Back to Today: the items are gone', detail: 'What you answered and approved has left the list; the count went down.', go: { path: '/' } },
  ]),
  S(2, 'New work', 'Create a bug, find it, start an agent and answer its question.', [
    { title: 'Create a bug', detail: 'Press c, choose Bug, fill Title and Requirements, Create.', go: { path: '/tickets' } },
    { title: 'Find it with ⌘K', detail: 'Type its key or a word of the title; Enter opens it.' },
    { title: 'Start an agent on it', detail: 'Actions → Start agent… on the new ticket; core shows what starts, then Start agent.' },
    { title: 'Watch it work', detail: 'Within about 15 s it claims the ticket, finishes T1 and asks you a question.', go: { path: '/agents' } },
    { title: 'Answer from Today', detail: 'The question is at the top of Questions; answer it there.', go: { path: '/' } },
  ]),
  S(3, 'Verdict', 'Read the evidence of a ticket in testing and send it back.', [
    { title: 'Open a ticket in testing', detail: 'DEMO-0041 waits for a verdict.', go: { path: '/ticket/DEMO-0041' } },
    { title: 'Read the evidence', detail: 'Acceptance & tasks (evidence per criterion), Artifacts (log, CSV, code, screenshot).', go: { path: '/ticket/DEMO-0041' } },
    { title: 'Read the changes', detail: 'Changes: core\'s diff of the branch against develop. Give verdict says "Pass on <commit> · +A −D": the verdict signs that commit.', go: { path: '/ticket/DEMO-0041' } },
    { title: 'Look at widgets', detail: 'DEMO-0046 shows charts, a flow and an image compare in its text.', go: { path: '/ticket/DEMO-0046' } },
    { title: 'Fail the verdict with a reason', detail: 'Give verdict → Send back, write why. The ticket returns to In progress.', go: { path: '/ticket/DEMO-0041' } },
  ]),
  S(4, 'Maintainer (Mara)', 'The same workspace as Mara: what she may and may not do.', [
    { title: 'Today as Mara', detail: 'Her own queue; items waiting for an owner are named as such.', go: { path: '/', viewer: 'p_mara' } },
    { title: 'Owner-only gates', detail: 'DEMO-0044: "Approve plan (owners only)" is disabled with the reason.', go: { path: '/ticket/DEMO-0044', viewer: 'p_mara' } },
    { title: 'Settings are read-only', detail: 'Every control is off and says "Only owners can …".', go: { path: '/settings/gates', viewer: 'p_mara' } },
    { title: 'Approve what she may', detail: 'Approvals and verdicts that maintainers may sign.', go: { path: '/', viewer: 'p_mara' } },
    { title: 'Decide a factory permit', detail: 'AI Factory → the permission request above the tabs (Busy day: the factory is installed there).', go: { path: '/addon/factory/factory', viewer: 'p_mara', dataset: 'busy' } },
  ]),
  S(5, 'Viewer (Tom)', 'Everything read-only, with reasons, and no dead ends.', [
    { title: 'Today as Tom', detail: 'One line per person who decides; opens into read-only rows.', go: { path: '/', viewer: 'p_tom' } },
    { title: 'A ticket page', detail: 'Disabled controls say why ("Viewers cannot change tickets.").', go: { path: '/ticket/DEMO-0043', viewer: 'p_tom' } },
    { title: 'Board and Tickets', detail: 'No New ticket button; c says viewers cannot create tickets.', go: { path: '/board', viewer: 'p_tom' } },
    { title: 'Wiki and Guide', detail: 'Open pages and search; Edit page is off with the reason.', go: { path: '/addon/wiki/pages', viewer: 'p_tom' } },
    { title: 'Activity filters', detail: 'Filters apply as you choose them (Busy day: Activity is installed there).', go: { path: '/addon/activity/activity', viewer: 'p_tom', dataset: 'busy' } },
    { title: 'No error toasts from normal use', detail: 'Click around; nothing should answer with a red toast.', go: { path: '/settings/members', viewer: 'p_tom' } },
  ]),
  S(6, 'Owner admin', 'Severin runs the workspace: people, gates, addons, grants.', [
    { title: 'Add a member', detail: 'Members → Add member: pick a person (Enter picks, a second Enter adds).', go: { path: '/settings/members' } },
    { title: 'Change a role', detail: 'The Role of a member; the last owner cannot be demoted.', go: { path: '/settings/members' } },
    { title: 'Change a gate policy', detail: 'Gates: count, approvers, "Affects N open tickets"; each change is signed.', go: { path: '/settings/gates' } },
    { title: 'Install quick tasks', detail: 'Addons → Browse addons → Quick tasks → Install → Grant and turn on (one signature).', go: { path: '/settings/addons' } },
    { title: 'Update GitHub', detail: 'The update shows what changes; one signature grants the new version.', go: { path: '/settings/addons' } },
    { title: 'Revoke an agent grant', detail: 'Agents → Grants → Revoke grant; the agent sessions on it stop.', go: { path: '/agents' } },
    { title: 'A member grants themselves', detail: 'In CLI Tom is a member: Agents → Issue grant… covers the tickets he may work on, at most 8 h.', go: { path: '/agents', viewer: 'p_tom', workspace: 'CLI' } },
  ]),
  S(7, 'Addons tour', 'Every addon page, panel and Today card, each marked with the orange A.', [
    { title: 'Publish', detail: 'Start and stop an app (Undo), revoke a share, a show-once link (asks first, then "Copy this link now").', go: { path: '/addon/publish/shares' } },
    { title: 'GitHub', detail: 'Review on GitHub, Approve; the issues lane on the Board imports an issue as a ticket.', go: { path: '/addon/github/reviews' } },
    { title: 'Usage', detail: 'Overview and By model; every number says its period.', go: { path: '/addon/usage/overview' } },
    { title: 'Wiki', detail: 'Open a page, edit it, link it to a ticket.', go: { path: '/addon/wiki/pages' } },
    { title: 'Terminals', detail: 'Run `orch status`, `git log`, arrow-up history; an agent mirror is read-only.', go: { path: '/addon/terminals/sessions' } },
    { title: 'Estimate', detail: 'Estimate a card in its ticket panel; the Board column shows the sum.', go: { path: '/board' } },
    { title: 'Worktrees', detail: 'Add a worktree, Remove is refused on a dirty one, Open terminal here.', go: { path: '/addon/worktrees/worktrees', dataset: 'busy' } },
    { title: 'Quick tasks', detail: 'Add, close with proof, make a ticket, the outgrew decision.', go: { path: '/addon/quick/quick', dataset: 'busy' } },
    { title: 'Records', detail: 'Record changes, push twice (rejected: pull first), pull, push.', go: { path: '/addon/records/records', dataset: 'busy' } },
    { title: 'Activity', detail: 'Filters and Show older.', go: { path: '/addon/activity/activity', dataset: 'busy' } },
    { title: 'Widgets in tickets', detail: 'DEMO-0043, DEMO-0046 and DEMO-0047 (the last one has refused blocks).', go: { path: '/ticket/DEMO-0047' } },
    { title: 'Start agent and model routing', detail: 'The model line in the start dialog; an invalid model name in Model routing blocks starts.', go: { path: '/addon/start-agent/start', dataset: 'busy' } },
    { title: 'Guide', detail: 'Press ? on several pages: the help follows the page.', go: { path: '/addon/guide/guide' } },
    { title: 'AI Factory', detail: 'Run demo activity, a permit, Pause and Resume (signed).', go: { path: '/addon/factory/factory', dataset: 'busy' } },
    { title: 'Schedules', detail: 'Enable a schedule (signed), Run now, file the finding.', go: { path: '/addon/schedules/schedules', dataset: 'busy' } },
  ]),
  S(8, 'Workspace hopping', 'Switching workspaces keeps you on the page where that makes sense.', [
    { title: 'Switcher previews', detail: 'Open the workspace switcher: each workspace shows what needs you there.', go: { path: '/' } },
    { title: '⌘2 on the Board', detail: 'The Board stays; it shows INT now.', go: { path: '/board' } },
    { title: 'Switch on a ticket page', detail: 'You land on Tickets with a toast naming the ticket\'s workspace.', go: { path: '/ticket/DEMO-0043' } },
    { title: 'Switch on an addon page', detail: 'Publish is not in INT: you land on Today with a toast.', go: { path: '/addon/publish/shares' } },
  ]),
  S(9, 'Keyboard only', 'Scenarios 1 and 2 without the mouse.', [
    { title: 'Today and the ticket page', detail: 'Tab, g b / g t / g a, Enter, Esc; the focus is always visible.', go: { path: '/' } },
    { title: 'Tickets with j and k', detail: 'j/k move, Enter opens.', go: { path: '/tickets' } },
    { title: '⌘K and ?', detail: '⌘K finds tickets and commands; ? opens help.' },
    { title: 'New ticket with ⌘↵', detail: 'c, type, ⌘↵ creates.', go: { path: '/tickets' } },
  ]),
  S(10, 'Terminal dock', 'The terminal beside any page, bottom or right.', [
    { title: 'Open the dock', detail: 'Ctrl+` on a ticket page: sessions for this ticket.', go: { path: '/ticket/DEMO-0043' } },
    { title: 'Bottom, then right', detail: 'The menu moves it; the page keeps at least 720 px.', go: { path: '/ticket/DEMO-0043' } },
    { title: 'Watch an agent', detail: 'An agent session is read-only and ends on what it waits for.' },
    { title: 'Start your own Claude Code session', detail: 'New session, with and without the ticket context.' },
    { title: 'Continue an earlier session', detail: 'Sessions → Earlier → Continue from summary.' },
    { title: 'Collapse, reopen, resize', detail: 'Drag the edge or use the arrow keys on it.' },
  ]),
  S(11, 'Epics', 'Board and Tickets grouped by epic on a busy day.', [
    { title: 'Board grouped by epic', detail: 'Epic lanes first (a 40-child epic folded), No epic last, 5 cards per cell then "+N more".', go: { path: '/board', dataset: 'busy' } },
    { title: 'Tickets grouped by epic', detail: 'Epic rows with progress; children indented.', go: { path: '/tickets', dataset: 'busy' } },
    { title: 'Search a child', detail: 'A folded lane opens when its child matches.', go: { path: '/board', dataset: 'busy' } },
    { title: 'Drag within a lane', detail: 'Drop in another column of the same lane; the epic never changes by drag.', go: { path: '/board', dataset: 'busy' } },
    { title: 'Group: None', detail: 'Display → Group: None gives the flat board.', go: { path: '/board', dataset: 'busy' } },
  ]),
  S(12, 'Addon pages with tabs', 'Compact addon pages; what needs you sits above the tabs.', [
    { title: 'Publish', detail: 'Apps | Shares.', go: { path: '/addon/publish/shares' } },
    { title: 'Usage by model', detail: 'The By model total equals the Overview tokens.', go: { path: '/addon/usage/overview' } },
    { title: 'Wiki page view', detail: 'On this page, Linked from, Edit page and the unsaved-changes guard.', go: { path: '/addon/wiki/pages' } },
    { title: 'GitHub', detail: 'Pull requests | Issues; Recently merged folded.', go: { path: '/addon/github/reviews' } },
    { title: 'Records', detail: 'Pending | History; one headline that says the next step.', go: { path: '/addon/records/records', dataset: 'busy' } },
    { title: 'Schedules', detail: 'Schedules | Runs; the finding above the tabs.', go: { path: '/addon/schedules/schedules', dataset: 'busy' } },
    { title: 'AI Factory', detail: 'The permit above the tabs.', go: { path: '/addon/factory/factory', dataset: 'busy' } },
    { title: 'Quick tasks', detail: 'Active | Completed.', go: { path: '/addon/quick/quick', dataset: 'busy' } },
    { title: 'Worktrees', detail: 'Filters, then Add worktree.', go: { path: '/addon/worktrees/worktrees', dataset: 'busy' } },
  ]),
  S(13, 'Widgets', 'Every widget type, and widgets inside tickets.', [
    { title: 'Widgets gallery', detail: 'Core types and templates, each with its source.', go: { path: '/addon/widgets/widgets' } },
    { title: 'DEMO-0043', detail: 'Stats, a series chart and checks.', go: { path: '/ticket/DEMO-0043' } },
    { title: 'DEMO-0045', detail: 'Timeline and diff in Current state.', go: { path: '/ticket/DEMO-0045' } },
    { title: 'DEMO-0046', detail: 'Flow diagram, callout, image compare; Expand one.', go: { path: '/ticket/DEMO-0046' } },
    { title: 'An error widget', detail: 'DEMO-0047: refused blocks say why in plain words.', go: { path: '/ticket/DEMO-0047' } },
  ]),
  S(14, 'New ticket', 'The overlay and the quick ticket.', [
    { title: 'Open the overlay', detail: 'Press c on any page.', go: { path: '/tickets' } },
    { title: 'Quick ticket', detail: 'Type one line, Enter: a backlog ticket; the toast has Open and Undo.' },
    { title: 'Dictate (simulated)', detail: 'The mic fills a sample text; nothing is recorded.' },
    { title: 'Open full page', detail: 'The draft goes with you.' },
    { title: 'Unsaved prompt', detail: 'Type, then Esc: "Discard unsaved changes?".' },
  ]),
  S(15, 'Settings', 'The other settings tabs.', [
    { title: 'Addon settings', detail: 'Addons → Settings on a row expands its form beneath the row; Save closes it.', go: { path: '/settings/addons' } },
    { title: 'Members', detail: 'The combobox; an unknown email is refused inline.', go: { path: '/settings/members' } },
    { title: 'Gates', detail: 'Policy and "Affects N open tickets".', go: { path: '/settings/gates' } },
    { title: 'Relay & devices', detail: 'Pair a device (simulated QR and code).', go: { path: '/settings/relay' } },
    { title: 'Skills', detail: 'Grant credentials to a skill (signed).', go: { path: '/settings/skills' } },
    { title: 'Connections', detail: 'Run check, Run doctor, the secrets file (names only).', go: { path: '/settings/connections' } },
  ]),
  S(16, 'Landing (D53)', 'The merge lane: approval, checks and merge bind to one candidate.', [
    { title: 'Landing page', detail: 'Queues | Needs | History.', go: { path: '/addon/land/landing' } },
    { title: 'A voided approval', detail: 'DEMO-0053: a conflict resolution voided the verify approval; back to review.', go: { path: '/ticket/DEMO-0053' } },
    { title: 'New commits void the verdict', detail: 'DEMO-0042 → Changes → "Simulate: the agent pushes a commit". Core voids the verdict; the ticket is back in testing.', go: { path: '/ticket/DEMO-0042' } },
    { title: 'The code review gate', detail: 'Gates → Code review: off by default; on for every ticket or by type. It follows the verdict on the same commit, never by an assignee.', go: { path: '/settings/gates' } },
    { title: 'A charter verdict', detail: 'DEMO-0051: "Verdict: via the factory charter — no person reviewed this". The AI Factory\'s Children tab names each verdict (Busy day).', go: { path: '/ticket/DEMO-0051', dataset: 'busy' } },
    { title: 'Resolve it yourself', detail: 'Today → From addons → "I will resolve it", then Mark resolved on the Landing page (Busy day).', go: { path: '/', dataset: 'busy' } },
    { title: 'Board chips', detail: 'Landing state on cards (landing, checking, conflict); no extra column (Busy day).', go: { path: '/board', dataset: 'busy' } },
  ]),
  S(17, 'Artifacts and Drop', 'All artifacts in one place; files shared through the relay.', [
    { title: 'Artifacts', detail: 'List and grid, filters, preview in the drawer.', go: { path: '/artifacts' } },
    { title: 'Install Drop', detail: 'Drop starts in the catalog: Browse addons → Drop → Install.', go: { path: '/settings/addons' } },
    { title: 'Claim a file', detail: 'Drop → Inbox → Claim.', go: { path: '/addon/drop/drop' } },
    { title: 'Share a file', detail: 'Share a file; a link is shown once.', go: { path: '/addon/drop/drop' } },
  ]),
  S(18, 'Blocked by a connection', 'A ticket that needs a login that expired.', [
    { title: 'Blocked tickets', detail: 'DEMO-0053 (databricks-prod auth expired) and DEMO-0054 (gcloud-billing wrong identity).', go: { path: '/ticket/DEMO-0054' } },
    { title: 'Start agent is refused', detail: 'Actions → Start agent… is off with the reason.', go: { path: '/ticket/DEMO-0053' } },
    { title: 'Re-login from Today', detail: 'Connections group: copy the login hint, Run check again (Demo).', go: { path: '/' } },
    { title: 'Unblocked', detail: 'After Run check again for gcloud-billing, DEMO-0054 no longer says Blocked and Start agent works.', go: { path: '/ticket/DEMO-0054' } },
  ]),
  S(19, 'Mandates (preview)', 'How the wide mandate (owner decision 10 Oct evening) would look. A non-functional preview: nothing signs.', [
    { title: 'Preflight', detail: 'Agents → Mandates: the four prerequisites are "not available in this build", so issuing is blocked.', go: { path: '/agents?tab=mandates' } },
    { title: 'Issue a mandate', detail: 'Show the mandate anyway (preview), then Issue a mandate…: "Steers the whole workspace in your name", what it may do (approve waiting agents and gates, unblock tickets, enable factories and start their runs including Deliver, issue grants), what stays yours (settings, addons, members and roles, devices, relay pairing, secrets and connections, protected paths, another mandate), a length up to 30 days; "Sign mandate (preview — nothing is signed)".', go: { path: '/agents?tab=mandates' } },
    { title: 'Renew', detail: 'Renew… in the mandate: pick a new length (up to 30 days) from now; revision 2, same scope.', go: { path: '/agents?tab=mandates' } },
    { title: 'The banner', detail: 'One calm line on every page: Mandate md_3 · for Severin · whole workspace · 10 decisions · until Tue. Details opens the tab; × hides it for this session.', go: { path: '/board' } },
    { title: 'Decided for you', detail: 'Today, below the real queue: one folded row. Open it for up to 3 decisions (a grant, a factory run, a permit … with Looks right, Veto), the refused items (protected path, your veto, an addon install and a second mandate, both refused) and Show all; "Revoke mandate and void…" sits in its header.', go: { path: '/' } },
    { title: 'Stop', detail: 'Stop in the banner (optionally also stop agents): "Stopping…", then "Stopped at #1842".', go: { path: '/agents?tab=mandates' } },
    { title: 'Revoke and void', detail: 'Lists the 8 decisions it would void and the 2 on landed work it only lists for review.', go: { path: '/agents?tab=mandates' } },
    { title: 'Off again', detail: 'Demo data → Preview: mandates (or Reset demo): Today and the shell look exactly as before.', go: { path: '/' } },
  ]),
  S(20, 'Factory full run', 'Owner decision 10 Oct evening (D61 option): one signed request, the factory goes to Preview or all the way to Deliver, with a hold window and Stop.', [
    { title: 'A run on hold', detail: 'Demo data → Busy day, then AI Factory: "Delivering in 28 min · Publish campaign to the newsletter list" above the tabs with Stop delivery, the same line in the shell, and a needs-you item on Today.', go: { path: '/addon/factory/factory' } },
    { title: 'Stop the delivery', detail: 'Stop… in the shell line (or Stop delivery on the page): core\'s prompt names the run, the destination and the end of the window; Send answer. "Stopped by Severin …: nothing went out."', go: { path: '/addon/factory/factory' } },
    { title: 'Request a full run', detail: 'Full runs tab: Goal, "How far may the factory go on its own?" → All the way to Deliver, What Deliver means (required), Hold window → Review request → Sign and start (or Fill in a demo request). The prompt lists every value in core lines.', go: { path: '/addon/factory/factory' } },
    { title: 'Watch the steps', detail: 'Plan, then Requirements / Build and test / Validate / Evidence per child, then Preview: each reads "via the factory full run you signed on … — no person reviewed this step". About 30 s later it holds before Deliver.', go: { path: '/addon/factory/factory' } },
    { title: 'Delivered', detail: 'Skip the wait (demo) ends the hold now: "Delivered: Deploy to production at …". The epic\'s History has the run\'s milestones.', go: { path: '/addon/factory/factory' } },
  ]),
]

export const STEP_COUNT = SCENARIOS.reduce((n, s) => n + s.steps.length, 0)
