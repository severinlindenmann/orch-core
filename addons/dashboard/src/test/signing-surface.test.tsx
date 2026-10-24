import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SignPopover } from '@/components/sign/SignPopover'
import userEvent from '@testing-library/user-event'
// The signing and core-confirm paths of the mock, in one table (the list is exactly the CASES below):
//  (a) the dialog's title and its Covers hold core's words only: nothing the addon wrote, except its name as
//      "Title (package id)"; what the addon wrote is shown, in full, in a labelled "From the addon" region instead;
//  (b) the body that is posted equals what the dialog showed, as key + value pairs: an addon-picked arg is core's
//      line for that exact key with that exact value (in the covers or core's "Sends" list, never inside the addon's
//      region); a core field is the line core renders for it.
import { render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { wordsAndId } from '@/addon-ui/SignConfirm'
import { api } from '@/api/client'
import { approversText } from '@/api/gates'
import { getAddon } from '@/mocks/addons'
import { seedRuns, DEMO_REQUEST } from '@/mocks/addons/factory-runs'
import type { MockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'
import { nowMs } from '@/lib/time'

const T = { timeout: 8000 }
type User = ReturnType<typeof renderApp>['user']
type Method = 'postAction' | 'runAddonAction' | 'issueGrant' | 'revokeGrant' | 'grantSkillCredentials' | 'postAddonOp' | 'postRelay' | 'postSettings'
/** How core renders one posted field: a text that must appear, or a test on one element's text (a cover line). */
type Shown = (v: string) => string | ((line: string) => boolean)

interface Case {
  name: string
  path: string
  viewer?: string
  wide?: boolean
  setup?: (s: MockStore) => void
  /** Opens the dialog and returns it. */
  open: (user: User) => Promise<HTMLElement>
  /** Clicks the dialog's confirm button. */
  confirm: (user: User, dialog: HTMLElement) => Promise<void>
  method: Method
  /** Index of the body among the call's arguments. */
  arg: number
  /** Only the call whose body has this `op` (the relay flow posts other ops first). */
  op?: string
  /** Strings the addon wrote that this dialog shows: never in the title or covers, always inside a labelled region. */
  addon?: string[]
  /** Body keys that are core's protocol (the verb the title already says) or plumbing (`confirmed`). */
  skip?: string[]
  /** Addon-picked args: each must be core's line for that key (data-arg-key/value), outside the addon region; `ticket` is "About <ticket>". */
  args?: boolean
  /** Args that must be among the posted (and shown) ones: what this signature has to cover. */
  expectArgs?: string[]
  /** Core's rendering of each other posted field. Every posted field needs one. */
  shown?: Record<string, Shown>
  /** The body is only the verb (relay connect): nothing else to compare. */
  verbOnly?: boolean
  /** A second request the same signature leads to (starting the agent after the grant). */
  then?: { method: Method; arg: number; skip?: string[]; shown: Record<string, Shown> }
}

const wsOf = (s: MockStore) => s.workspaces.find((w) => w.prefix === 'DEMO')!.id
const dialogNamed = (name: string | RegExp) => screen.findByRole('dialog', { name }, T)
const press = (name: string | RegExp) => async (user: User, dialog: HTMLElement) => user.click(within(dialog).getByRole('button', { name }))
/** A typed signed value's line (security review #8): a string quoted, a number or boolean bare. */
const typed = (label: string) => (v: string) => (l: string) => l === `${label}: "${v}"` || (/^(-?\d+(\.\d+)?|true|false)$/.test(v) && l === `${label}: ${v}`)
const line = (prefix: string, has: (v: string) => string) => (v: string) => (l: string) => l.startsWith(prefix) && l.includes(has(v))

async function ticketAction(user: User, name: RegExp) {
  const header = await screen.findByTestId('ticket-header', {}, T)
  const primary = within(header).queryByRole('button', { name })
  if (primary) await user.click(primary)
  else {
    await user.click(await screen.findByRole('button', { name: /Actions/ }, T))
    await user.click(await screen.findByRole('menuitem', { name }, T))
  }
  return screen.findByRole('dialog', {}, T)
}

const grantShown: Record<string, Shown> = { hours: (v) => `${v} h`, scope: (v) => `${v} tickets` }
const addonOpShown = (name: string): Record<string, Shown> => ({
  version: (v) => `Addon: ${name} ${v}`,
  package_sha256: (v) => `sha256:${v}`,
  capabilities: line('Capabilities:', (v) => v),
  viewer_actions: line('Viewers can:', (v) => wordsAndId(v)),
})
const LINE = 'Routing picked this model for you, LINE-MARK'

const CASES: Case[] = [
  {
    name: 'owner landing on main', path: '/ticket/DEMO-0052', wide: true,
    setup: (s) => {
      const state = s.addonState(wsOf(s), 'land') as unknown as { settings: { targets: string[] }; queues: { entries: unknown[] }[]; attempts: unknown[]; worker: { current: number | null } }
      state.settings.targets = ['main', 'develop']
      state.queues.forEach((q) => { q.entries = [] })
      state.attempts = []
      state.worker.current = null
    },
    open: async (user) => {
      const panel = await openTicketPanel(user, 'Landing')
      await user.click(await within(panel).findByRole('button', { name: 'Land on main' }, T))
      return screen.findByRole('dialog', {}, T)
    },
    confirm: press('Sign and run'), method: 'runAddonAction', arg: 3,
    skip: ['confirmed'], args: true, expectArgs: ['ticket', 'target', 'commit'],
  },

  ...([0, 60] as const).map((hold): Case => ({
    name: `factory re-delivery with hold ${hold}`, path: '/addon/factory/factory',
    setup: s => {
      const ws = wsOf(s)
      installAndGrant(s, ws, 'factory')
      const st = s.addonState(ws, 'factory')
      Object.assign(st, seedRuns(s, 'p_sev', ws))
      const runs = st.runs as import('@/mocks/addons/factory-runs').Run[]
      runs[0].stage = 'stopped'
      runs[0].stopped = { at: s.now(), by: 'p_sev' }
      s.runAddon(ws, 'factory', 'prepare_redelivery', { formData: { run: 'R-1', hold_minutes: hold } })
    },
    open: async user => {
      await user.click(await screen.findByRole('tab', { name: /^Full runs/ }, T))
      await user.click(await screen.findByRole('button', { name: 'Sign delivery' }, T))
      const dialog = await dialogNamed(/^Sign: Redeliver/)
      if (!hold) expect(dialog).toHaveTextContent('Delivers as soon as you sign')
      else expect(dialog).not.toHaveTextContent('Delivers as soon as you sign')
      return dialog
    },
    confirm: press('Sign and run'), method: 'runAddonAction', arg: 3,
    skip: ['confirmed'], args: true,
    expectArgs: ['request', 'run', 'deliver_means', 'hold_minutes', 'revision'],
  })),
  {
    name: 'repos clone attention decision', path: '/',
    open: async user => {
      const row = await screen.findByTestId('card-addon:repos.clone.billing-api', {}, T)
      const decide = within(row).queryByRole('button', { name: 'Decide' })
      if (decide) await user.click(decide)
      await user.click(await within(row).findByRole('button', { name: 'Clone' }, T))
      return dialogNamed('Decide for Repos (repos)')
    },
    confirm: press('Send answer'), method: 'runAddonAction', arg: 3, skip: ['confirmed'],
    addon: ['Clone billing-api?'],
    shown: {
      id: v => `Decision ${v}`, option: v => `Answer: option ${v}`, digest: v => v,
      name: typed('Name (name)'), remote: typed('Remote (remote)'),
      target_folder: typed('Target folder (target_folder)'),
      clone_as: typed('Clone as (clone_as)'),
      default_branch: typed('Default branch (default_branch)'),
    },
  },
  ...(['clone', 'clone_all', 'add', 'adopt', 'remove', 'remove_anyway'] as const).map((action): Case => ({
    name: `repos ${action}`,
    path: '/addon/repos/repos',
    setup: s => {
      if (action === 'add') s.addonState(wsOf(s), 'repos').drafts = { p_sev: { name: 'new-repo', path: 'new-repo', remote: 'https://git.example.test/acme/new-repo.git', branch: 'main' } }
    },
    open: async user => {
      await screen.findByRole('tab', { name: 'Structure' }, T)
      if (action === 'clone_all') await user.click(screen.getByRole('button', { name: 'Clone all missing' }))
      else if (action === 'add') {
        await user.click(screen.getByRole('button', { name: /Declare a repo/ }))
        await user.click(await screen.findByRole('button', { name: 'Sign and declare' }, T))
      } else {
        const folder = action === 'clone' ? 'billing-api' : action === 'adopt' ? 'sandbox' : action === 'remove_anyway' ? 'web-portal' : 'shared-lib'
        await user.click(screen.getByRole('button', { name: `Details for ${folder}` }))
        await user.click(screen.getByRole('button', { name: action === 'clone' ? 'Clone' : action === 'adopt' ? 'Declare untracked repo' : action === 'remove_anyway' ? 'Remove anyway…' : 'Remove from declared repos' }))
      }
      if (action === 'remove') {
        const alert = await screen.findByRole('alertdialog', {}, T)
        // Core's own sentence (core carries out the settings.repos change), outside the addon's region.
        expect(within(alert).getByTestId('core-note')).toHaveTextContent('The folder and its files stay on disk.')
        return alert
      }
      const dialog = await screen.findByRole('dialog', {}, T)
      if (action === 'remove_anyway') expect(within(dialog).getByTestId('core-note')).toHaveTextContent('The folder and its files stay on disk.')
      if (action === 'remove_anyway') await user.selectOptions(within(dialog).getByRole('combobox'), 'remove')
      return dialog
    },
    confirm: press(action === 'remove' ? 'Confirm: Remove (remove)' : action === 'remove_anyway' ? 'Continue: Remove anyway (remove_anyway)' : 'Sign and run'),
    method: 'runAddonAction', arg: 3, skip: ['confirmed'], args: true,
    expectArgs: action === 'clone_all' ? ['targets', 'clone_as'] : action === 'remove' || action === 'remove_anyway' ? ['name', 'target_folder'] : action === 'clone' ? ['remote', 'default_branch', 'target_folder', 'clone_as'] : ['name', 'path', 'remote', 'target_folder'],
  })),

  {
    name: 'gate approve',
    path: '/ticket/DEMO-0044',
    open: (user) => ticketAction(user, /Approve plan/),
    confirm: press('Approve plan'),
    method: 'postAction',
    arg: 1,
    skip: ['action'],
    // The content hash it binds (security review #2) is the one in the dialog's Details.
    shown: { gate: (v) => `Approve ${v}`, hash: (v) => v },
  },
  {
    name: 'answer',
    path: '/ticket/DEMO-0043',
    open: async (user) => {
      await user.click(await screen.findByRole('tab', { name: /Questions/ }, T))
      await user.click(await screen.findByRole('radio', { name: /DATE \(local midnight\)/ }, T))
      await user.click(within(document.getElementById('question-Q2')!).getByRole('button', { name: 'Send answer' }))
      return screen.findByRole('dialog', {}, T)
    },
    confirm: press('Send answer'),
    method: 'postAction',
    arg: 1,
    skip: ['action'],
    shown: { question: (v) => `Question ${v}:`, option: (v) => `(option ${v})`, hash: (v) => v },
  },
  {
    name: 'verdict',
    path: '/ticket/DEMO-0041',
    open: async (user) => {
      const dialog = await ticketAction(user, /Give verdict/)
      expect(dialog).toHaveAccessibleName('Give verdict (Verify)')
      await user.click(within(dialog).getByRole('radio', { name: /^Pass on .*: the evidence is enough$/ }))
      return dialog
    },
    confirm: press('Pass'),
    method: 'postAction',
    arg: 1,
    skip: ['action'],
    // The verdict signs the commit (owner decision 2026-10-10): the head is in the choice and the covers, in full.
    shown: { result: (v) => (v === 'pass' ? 'Pass on ' : 'Send back ·'), source_sha: (v) => `Commit ${v} on ` },
  },
  {
    // Owner decision 2026-10-10 (1): the opt-in code review gate signs exactly the commit the verdict signed.
    name: 'code review gate approval',
    path: '/ticket/DEMO-0041',
    setup: (s) => {
      s.appendWs(wsOf(s), { type: 'gate.policy_set', gate: 'code', approvers: 'maintainer', count: 1, not: 'assignees', applies: 'all' })
      const head = s.ticket('DEMO-0041')!.branch.head
      s.append('DEMO-0041', { type: 'verdict.given', actor: 'p_mara', result: 'pass', source_sha: head })
      s.append('DEMO-0041', { type: 'gate.approved', actor: 'p_mara', gate: 'verify', source_sha: head, presence: 'touchid' })
    },
    open: (user) => ticketAction(user, /Approve code review/),
    confirm: press('Approve code review'),
    method: 'postAction',
    arg: 1,
    skip: ['action'],
    shown: { gate: (v) => `Approve ${v}`, source_sha: (v) => `Commit ${v} on ` },
  },
  {
    name: 'addon decision (Today)',
    path: '/',
    open: async (user) => {
      const row = await screen.findByTestId('card-addon:dec_publish_failed_build', {}, T)
      const decide = within(row).queryByRole('button', { name: 'Decide' })
      if (decide?.getAttribute('aria-expanded') === 'false') await user.click(decide)
      await user.click(within(row).getByRole('button', { name: 'Retry last good version' }))
      return dialogNamed('Decide for Publish (publish)')
    },
    confirm: press('Send answer'),
    method: 'runAddonAction',
    arg: 3,
    addon: ['Retry failed build', 'Ops notebook failed to build', 'Retry last good version'],
    skip: ['confirmed'],
    // The digest of the decision as shown (security review #3) sits in the prompt's Details.
    shown: { id: (v) => `Decision ${v}`, option: (v) => `Answer: option ${v}`, ticket: (v) => `About ${v}`, digest: (v) => v },
  },
  {
    name: 'addon sign (confirm: sign)',
    path: '/addon/schedules/schedules',
    setup: (s) => installAndGrant(s, wsOf(s), 'schedules'),
    open: async (user) => {
      const row = (await screen.findByText('Smoke test on testing', {}, T)).closest('tr')!
      await user.click(within(row).getByRole('button', { name: 'Enable' }))
      return dialogNamed(/^Sign: .* · Schedules \(schedules\)$/)
    },
    confirm: press('Sign and run'),
    method: 'runAddonAction',
    arg: 3,
    addon: ['Smoke test on testing', 'Enable a schedule'],
    skip: ['confirmed'],
    args: true,
  },
  {
    name: 'addon options (confirm: options)',
    path: '/ticket/DEMO-0041',
    wide: true,
    open: async (user) => {
      const panel = await openTicketPanel(user, 'Shares')
      await user.click(await within(panel).findByRole('button', { name: 'New show-once link' }, T))
      return dialogNamed('Choose: Share once (share_once) · Publish (publish)')
    },
    confirm: press('Continue: Share once (share_once)'),
    method: 'runAddonAction',
    arg: 3,
    addon: ['Create show-once link', 'The link is shown once', 'What to share', 'The ticket page (read-only)', 'Works for', 'At most 3 times'],
    skip: ['confirmed'],
    args: true,
  },
  {
    name: 'addon destructive (confirm: destructive)',
    path: '/addon/publish/shares',
    open: async (user) => {
      const row = (await screen.findByText('Billing explorer', {}, T)).closest('tr')!
      await user.click(within(row).getByRole('button', { name: 'Stop' }))
      return screen.findByRole('alertdialog', {}, T)
    },
    confirm: press('Confirm: Stop (stop)'),
    method: 'runAddonAction',
    arg: 3,
    addon: ['Stop app', 'Anyone using the app loses it', 'Billing explorer'],
    skip: ['confirmed'],
    args: true,
  },
  {
    // Workspace links (owner feedback 2026-10-10 B): an incoming pairing request is an owner's decision; its id carries the comparison code.
    name: 'links pairing request (decision node)',
    path: '/addon/links/links',
    setup: (s) => installAndGrant(s, wsOf(s), 'links'),
    open: async (user) => {
      await user.click(await screen.findByRole('tab', { name: /^Requests/ }, T))
      await user.click(await screen.findByRole('button', { name: 'Codes match: link on these terms' }, T))
      return dialogNamed('Decide for Workspace links (links)')
    },
    confirm: press('Send answer'),
    method: 'runAddonAction',
    arg: 3,
    addon: ['Link request from Fabrikam Energy', 'Codes match: link on these terms', 'Jonas Weber'],
    skip: ['confirmed'],
    // The id names the signed terms: comparison code, carrier, what each side may send and the expiry.
    // The id binds; the terms are core's own labelled lines (posted with the answer, checked and recorded by the host).
    shown: {
      id: (v) => (v.endsWith('.relay.recv-question+drop.send-handoff+question.90d') ? `Decision ${v}` : `unexpected id ${v}`),
      option: (v) => `Answer: option ${v}`,
      digest: (v) => v,
      peer: typed('Peer (peer)'),
      comparison_code: typed('Comparison code (comparison_code)'),
      carrier: typed('Carrier (carrier)'),
      they_may_send_us: typed('They may send us (they_may_send_us)'),
      we_may_send_them: typed('We may send them (we_may_send_them)'),
      expires_after: typed('Expires after (expires_after)'),
    },
  },
  {
    name: 'links confirm pairing (confirm: sign)',
    path: '/addon/links/links',
    setup: (s) => {
      installAndGrant(s, wsOf(s), 'links')
      s.addonState(wsOf(s), 'links').pending = {
        id: 'pr_9', carrier: 'local', peer: { name: 'CLI · Client VM', ws: 'CLI', owner: 'Severin' }, accepts: ['status'], days: 30, pairing_code: 'ABC-DEF',
        started_at: '2026-10-09T11:29:00Z', started_by: 'p_sev', joined: { at: '2026-10-09T11:29:30Z', fingerprint: 'K7M2QX', theyAccept: ['status'] },
      }
    },
    open: async (user) => {
      await user.click(await screen.findByRole('tab', { name: /^Set up/ }, T))
      await user.click(await screen.findByRole('button', { name: 'Codes match: sign' }, T))
      return dialogNamed(/^Sign: .* · Workspace links \(links\)$/)
    },
    confirm: press('Sign and run'),
    method: 'runAddonAction',
    arg: 3,
    addon: ['Link the two workspaces'],
    skip: ['confirmed'],
    args: true,
    // The covers name the peer, carrier, scopes both ways and the expiry (each one checked by the host).
    expectArgs: ['pairing', 'code', 'peer', 'carrier', 'they_may_send', 'we_may_send', 'expires_days'],
  },
  {
    name: 'links hand off a ticket (confirm: sign)',
    path: '/addon/links/links',
    setup: (s) => {
      installAndGrant(s, wsOf(s), 'links')
      s.addonState(wsOf(s), 'links').nav = { p_sev: { draft: { link: 'ln_int', ticket: 'DEMO-0045' } } }
    },
    open: async (user) => {
      await user.click(await screen.findByRole('tab', { name: /^Requests/ }, T))
      await user.click(await screen.findByRole('button', { name: 'Sign and send' }, T))
      return dialogNamed(/^Sign: .* · Workspace links \(links\)$/)
    },
    confirm: press('Sign and run'),
    method: 'runAddonAction',
    arg: 3,
    addon: ['Hand off a ticket'],
    skip: ['confirmed'],
    args: true,
    // The covers name the peer, carrier and exactly what crosses: title, sections, attachments and the deadline.
    expectArgs: ['link', 'to', 'carrier', 'ticket_key', 'title', 'sends', 'deadline'],
  },
  {
    name: 'links revoke (confirm: sign)',
    path: '/addon/links/links',
    setup: (s) => installAndGrant(s, wsOf(s), 'links'),
    open: async (user) => {
      const row = (await screen.findByText('INT · Internal', {}, T)).closest('tr')!
      await user.click(within(row).getByRole('button', { name: /More actions/ }))
      await user.click(await screen.findByRole('menuitem', { name: 'Revoke' }, T))
      return dialogNamed(/^Sign: Revoke .* · Workspace links \(links\)$/)
    },
    confirm: press('Sign and run'),
    method: 'runAddonAction',
    arg: 3,
    addon: ['Revoke a link: nothing crosses it from now on'],
    skip: ['confirmed'],
    args: true,
    expectArgs: ['id', 'peer'],
  },
  ...([0, 60] as const).map((hold): Case => ({
    // Owner decision 2026-10-10 evening (D61 option): one signature covers the goal, how far the factory goes, what
    // Deliver means (the exact destination the host will deliver to), the hold window and the size cap.
    name: `factory full run with hold ${hold}`,
    path: '/addon/factory/factory',
    setup: (s) => {
      installAndGrant(s, wsOf(s), 'factory')
      s.addonState(wsOf(s), 'factory').nav = { p_sev: { runDraft: { ...DEMO_REQUEST, hold_minutes: hold, request: 'rq-DEMO-0a1b2c3d-7' } } }
    },
    open: async (user) => {
      await user.click(await screen.findByRole('tab', { name: /^Full runs/ }, T))
      await user.click(await screen.findByRole('button', { name: 'Sign and start' }, T))
      const dialog = await dialogNamed(/^Sign: .* · AI Factory \(factory\)$/)
      const note = within(dialog).queryByTestId('core-note')
      if (hold === 0) {
        expect(note).toHaveTextContent('No hold: delivers as soon as its checks pass')
        expect(note!.compareDocumentPosition(dialog.querySelector('[data-arg-key]')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      } else expect(note).toBeNull()
      return dialog
    },
    confirm: press('Sign and run'),
    method: 'runAddonAction',
    arg: 3,
    addon: ['Start a factory full run'],
    skip: ['confirmed'],
    args: true,
    expectArgs: ['request', 'goal', 'goes_up_to', 'deliver_means', 'hold_minutes', 'largest_child'],
  })),
  {
    name: 'start agent (grant + start)',
    path: '/ticket/DEMO-0044',
    wide: true,
    setup: (s) => {
      void s.revokeGrant(wsOf(s), 'gr_01J9Z8', { kind: 'person', id: 'p_sev' })
      installAndGrant(s, wsOf(s), 'models')
      // The model-routing addon's line: its own words, shown in the start dialog.
      vi.spyOn(getAddon('models')!, 'launch').mockReturnValue({ line: LINE })
    },
    open: async (user) => {
      await openTicketPanel(user, 'Start agent')
      await user.click(await screen.findByRole('button', { name: 'Start' }, T))
      return dialogNamed('Sign a grant and start Claude Code on DEMO-0044')
    },
    confirm: press('Sign and start'),
    method: 'issueGrant',
    arg: 1,
    addon: [LINE],
    shown: grantShown,
    then: {
      method: 'runAddonAction',
      arg: 3,
      skip: ['confirmed'],
      shown: { ticket: (v) => `on ${v}`, mode: line('Mode', (v) => `(${v})`), harness: line('Harness', (v) => `(${v})`), where: line('Where', (v) => `(${v})`) },
    },
  },
  {
    name: 'agent grant (issue)',
    path: '/agents',
    open: async (user) => {
      await user.click(await screen.findByRole('button', { name: 'Issue grant…' }, T))
      return dialogNamed(/Issue a grant/)
    },
    confirm: press('Issue grant'),
    method: 'issueGrant',
    arg: 1,
    shown: grantShown,
  },
  {
    // Owner decision 2026-10-10 (5): a member signs a grant for themselves, limited to the tickets they may work on.
    name: 'member self-grant (issue)',
    path: '/agents',
    viewer: 'p_tom',
    setup: (s) => s.appendWs(wsOf(s), { type: 'member.role_changed', person: 'p_tom', role: 'member', from: 'viewer' }),
    open: async (user) => {
      await user.click(await screen.findByRole('button', { name: 'Issue grant…' }, T))
      return dialogNamed(/Issue a grant/)
    },
    confirm: press('Issue grant'),
    method: 'issueGrant',
    arg: 1,
    shown: { hours: (v) => `Duration: ${v} h`, scope: (v) => (v === 'workable' ? 'Scope: the tickets you may work on in this workspace' : `unexpected scope ${v}`) },
  },
  {
    name: 'agent grant (revoke)',
    path: '/agents',
    open: async (user) => {
      const row = await screen.findByRole('row', { name: /gr_01J9Z8/ }, T)
      await user.click(within(row).getByRole('button', { name: 'Revoke' }))
      return dialogNamed(/^Revoke /)
    },
    confirm: press(/Revoke grant/),
    method: 'revokeGrant',
    arg: 1,
    shown: { '': (v) => `· ${v}` },
  },
  {
    name: 'skill credential grant',
    path: '/settings/skills',
    open: async (user) => {
      await user.click(await screen.findByRole('button', { name: 'Open skill tariff-feed' }, T))
      const drawer = await dialogNamed(/tariff-feed/)
      await user.click(within(drawer).getByRole('button', { name: 'Grant TARIFF_WEBHOOK_SECRET' }))
      return dialogNamed('Grant credentials to tariff-feed')
    },
    confirm: press('Sign grant'),
    method: 'grantSkillCredentials',
    arg: 2,
    skip: ['confirmed'],
    shown: { env: (v) => `${v}: the agent session gets`, connections: (v) => `Connection ${v}` },
  },
  {
    name: 'addon install',
    path: '/settings/addons',
    // A viewer action whose manifest label differs from its id: the label is the addon's words.
    setup: (s) => {
      const q = s.addons.find((a) => a.name === 'quick')!
      q.actions = { ...q.actions, peek: { minRole: 'viewer', label: 'Peek at the queue' } }
    },
    open: async (user) => {
      await user.click(await screen.findByRole('button', { name: 'Browse addons' }, T))
      await user.click(within(await screen.findByRole('article', { name: /Quick tasks/ }, T)).getByRole('button', { name: 'Install' }))
      return dialogNamed(/^Install Quick tasks \(quick\)/)
    },
    confirm: press('Grant and turn on'),
    method: 'postAddonOp',
    arg: 2,
    addon: ['Peek at the queue'],
    skip: ['op', 'enable'],
    shown: addonOpShown('quick'),
  },
  {
    name: 'addon update',
    path: '/settings/addons',
    setup: (s) => {
      const gh = s.addons.find((a) => a.name === 'github')!
      gh.update!.actions = { ...gh.actions, refresh: { minRole: 'viewer', label: 'Refresh pull requests' } }
    },
    open: async (user) => {
      await user.click(within(await screen.findByRole('row', { name: /GitHub/ }, T)).getByRole('button', { name: /Update to 0\.6\.0/ }))
      return dialogNamed(/^Update GitHub \(github\) to 0\.6\.0/)
    },
    confirm: press('Update'),
    method: 'postAddonOp',
    arg: 2,
    addon: ['Review requests can now start an agent session', 'Refresh pull requests'],
    skip: ['op'],
    shown: addonOpShown('github'),
  },
  {
    name: 'relay connect',
    path: '/settings/relay',
    open: async (user) => {
      await user.click(await screen.findByRole('button', { name: 'Connect' }, T))
      return dialogNamed(/to the relay$/)
    },
    confirm: press('Sign and connect'),
    method: 'postRelay',
    arg: 1,
    op: 'connect',
    skip: ['op'],
    verbOnly: true,
  },
  {
    name: 'relay pair.confirm (add a device)',
    path: '/settings/relay',
    open: async (user) => {
      await user.click(await screen.findByRole('button', { name: 'Connect' }, T))
      await user.click(await screen.findByRole('button', { name: 'Sign and connect' }, T))
      await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Online'), T)
      await user.click(screen.getByRole('button', { name: 'Pair a device' }))
      const pair = await dialogNamed(/Pair a device/)
      await user.click(within(pair).getByRole('button', { name: /Simulate: a phone scans/ }))
      await user.click(await within(pair).findByRole('button', { name: 'Codes match' }, T))
      return dialogNamed(/^Add .* to /)
    },
    confirm: press('Sign and add device'),
    method: 'postRelay',
    arg: 1,
    op: 'pair.confirm',
    skip: ['op'],
    shown: { pairing: (v) => `pairing ${v}`, fingerprint: (v) => `(${v})` },
  },
  {
    name: 'relay device.remove',
    path: '/settings/relay',
    open: async (user) => {
      const row = (await screen.findByText("Tom's iPad", {}, T)).closest('tr')!
      await user.click(within(row).getByRole('button', { name: 'Remove' }))
      return dialogNamed(/Remove Tom's iPad/)
    },
    confirm: press('Sign and remove'),
    method: 'postRelay',
    arg: 1,
    op: 'device.remove',
    skip: ['op'],
    shown: { device: (v) => `· ${v}` },
  },
  {
    name: 'member role (settings)',
    path: '/settings/members',
    open: async (user) => {
      const row = await screen.findByRole('row', { name: /Tom/ }, T)
      await user.selectOptions(within(row).getByRole('combobox', { name: /^Role of / }), 'member')
      return screen.findByRole('dialog', {}, T)
    },
    confirm: press('Sign and save'),
    method: 'postSettings',
    arg: 1,
    skip: ['op'],
    shown: { person: (v) => `(${v})`, role: (v) => `to ${v}` },
  },
  {
    name: 'gate policy (settings)',
    path: '/settings/gates',
    setup: (s) => s.appendWs(s.workspaces[0].id, { type: 'gate.policy_set', gate: 'plan', approvers: 'maintainer', count: 1 }),
    open: async (user) => {
      await user.click(await screen.findByRole('button', { name: 'Plan: 2 approvals' }, T))
      return screen.findByRole('dialog', {}, T)
    },
    confirm: press('Sign and save'),
    method: 'postSettings',
    arg: 1,
    skip: ['op'],
    shown: { gate: (v) => `the ${v} gate`, approvers: (v) => `Approvers: ${approversText(v)}`, count: (v) => `Approvals needed: ${v}`, not: () => 'The ticket assignees cannot approve' },
  },
  {
    name: 'agent grant length (settings)',
    path: '/settings/general',
    open: async (user) => {
      await user.click(await screen.findByRole('button', { name: '1 day' }, T))
      await user.click(screen.getAllByRole('button', { name: 'Save' })[2]) // name, icon, then the grant length
      return screen.findByRole('dialog', {}, T)
    },
    confirm: press('Sign and save'),
    method: 'postSettings',
    arg: 1,
    skip: ['op'],
    shown: { hours: (v) => `Default length of a grant: ${v} hours` },
  },
]

/** [key, value] for every scalar leaf of a body (arrays and nested objects flattened; a bare value has key ''). */
function leaves(body: unknown, key = ''): [string, string][] {
  if (body === undefined || body === null) return []
  if (Array.isArray(body)) return body.flatMap((v) => leaves(v, key))
  if (typeof body === 'object') return Object.entries(body as Record<string, unknown>).flatMap(([k, v]) => leaves(v, k))
  return [[key, String(body)]]
}

/** Every element's text in the dialog as it was before confirming (cover lines, region lines, the title …). */
const textsOf = (dialog: HTMLElement) => [...dialog.querySelectorAll('*')].map((e) => e.textContent ?? '')

function expectShown(texts: string[], key: string, value: string, shown: Shown | undefined) {
  expect(shown, `no rendering declared for posted field "${key}"`).toBeDefined()
  const want = shown!(value)
  const ok = typeof want === 'string' ? texts.some((t) => t.includes(want)) : texts.some(want)
  expect(ok, `${key}=${value} was posted but its line was not shown${typeof want === 'string' ? ` ("${want}")` : ''}`).toBe(true)
}

beforeAll(async () => {
  await Promise.all([import('@/app/pages/ticket'), import('@/app/pages/today'), import('@/app/pages/agents'), import('@/app/pages/AddonPage')])
}, 30_000)
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('signing surface: these dialogs show exactly what is signed, in core\'s words', () => {
  it.each(CASES)('$name', async (c) => {
    if (c.wide) vi.stubGlobal('innerWidth', 1440)
    const spy = vi.spyOn(api, c.method)
    const next = c.then ? vi.spyOn(api, c.then.method) : null
    const { user } = renderApp(c.path, { viewer: c.viewer ?? 'p_sev', setup: c.setup })
    const dialog = await c.open(user)
    const callsBefore = spy.mock.calls.length
    const title = within(dialog).getAllByRole('heading')[0].textContent ?? ''
    const covers = within(dialog).queryByText('Covers')?.nextElementSibling?.textContent ?? ''
    const texts = textsOf(dialog)
    const regions = [...dialog.querySelectorAll('[aria-label^="From addon"], [aria-label^="From the addon"], [aria-label^="Written by the agent"]')]
    const args = new Map([...dialog.querySelectorAll('[data-arg-key]')].map((e) => [e.getAttribute('data-arg-key')!, e.getAttribute('data-arg-value')!]))
    for (const r of regions) expect(r.querySelector('[data-arg-key]'), 'an arg line sits inside the addon region').toBeNull()

    // (a) Core's words only in the title and covers; the addon's words are shown, inside a labelled region.
    // The button that confirms is core's too (it makes core send `confirmed` or sign).
    const buttons = within(dialog).getAllByRole('button').map((b) => b.textContent ?? '')
    for (const s of c.addon ?? []) {
      expect(buttons.some((b) => b.includes(s)), `a button says "${s}"`).toBe(false)
      expect(title, `title has "${s}"`).not.toContain(s)
      expect(covers, `covers have "${s}"`).not.toContain(s)
      expect(regions.some((r) => r.textContent?.includes(s)), `"${s}" is not shown inside a labelled addon region`).toBe(true)
    }

    // (b) Nothing is posted by opening the dialog; after confirming, the body is what the dialog showed, field by field.
    if (!c.op) expect(callsBefore, 'posted before the confirm').toBe(0)
    await c.confirm(user, dialog)
    const call = () => spy.mock.calls.slice(callsBefore).find((x) => !c.op || (x[c.arg] as { op?: string })?.op === c.op)
    await waitFor(() => expect(call()).toBeDefined(), T)
    const sent = leaves(call()![c.arg]).filter(([k]) => !(c.skip ?? []).includes(k))
    if (c.verbOnly) expect(sent).toEqual([])
    else expect(sent.length, 'the body carries something').toBeGreaterThan(0)
    for (const [k, v] of sent) {
      if (c.args && k !== 'ticket') expect(args.get(k), `arg ${k} was posted as "${v}" but the region shows ${JSON.stringify(args.get(k))}`).toBe(v)
      else if (c.args) expectShown(texts, k, v, (x) => `About ${x}`)
      else expectShown(texts, k, v, c.shown?.[k])
    }
    if (c.args) for (const k of args.keys()) expect(sent.map(([x]) => x), `the region shows ${k}, which was not posted`).toContain(k)
    for (const k of c.expectArgs ?? []) expect(sent.map(([x]) => x), `the signature does not cover ${k}`).toContain(k)
    if (c.then && next) {
      await waitFor(() => expect(next).toHaveBeenCalled(), T)
      for (const [k, v] of leaves(next.mock.calls.at(-1)![c.then.arg]).filter(([x]) => !(c.then!.skip ?? []).includes(x))) expectShown(texts, k, v, c.then.shown[k])
    }
  }, 40_000)
})

// The terminal hand-off (M3, real host): core's dialog does not sign. It shows the host's material, the fingerprint, the
// code and the command; the title and buttons keep core's words; nothing is posted through the signing methods.
describe('hand-off dialog', () => {
  it('shows only the host material, core words in the title, and signs nothing itself', async () => {
    const { fakeRequest, mount, spies, state } = await import('./handoffHarness')
    const { setFeatures } = await import('@/api/features')
    const s = spies()
    const request = fakeRequest()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    const { ticket } = await mount({ kind: 'approve', gate: 'plan' })
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(within(dialog).getByRole('heading', { name: 'Approve plan' })).toBeInTheDocument()
    expect(dialog.textContent).toContain('HOST-MATERIAL task text')
    expect(dialog.textContent).not.toContain(ticket.tasks[0].text)
    expect(within(dialog).getByTestId('handoff-fingerprint')).toHaveTextContent(request.fingerprint)
    for (const method of ['postAction', 'runAddonAction', 'issueGrant', 'revokeGrant', 'postAddonOp', 'postSettings'] as const) expect(vi.spyOn(api, method)).not.toHaveBeenCalled()
    setFeatures(null)
  })
})


it('records current-destination confirmation warning preserves the exact URL through Raw', async () => {
  const { ExportWarning } = await import('@/app/pages/settings/host/Records')
  const url = 'https://git.example.test/records\u202e.git'
  render(<ExportWarning url={url} current />)
  const warning = screen.getByTestId('records-export-warning')
  expect(warning.textContent).toBe('Push publishes every ticket in this workspace to https://git.example.test/records\\u{202e}.git, restricted ones included.')
  expect(warning.textContent).not.toContain('last known')
})

// M7b: Start agent (Tier A, no signature) shows exactly the host's preview and posts exactly the previewed values plus
// its spec; the runner grant (the one signed step) is the signer's View with the deposit line and the word, confirmed by holding Sign.
describe('start agent', () => {
  it.each([false, true])('runner grant (extend=%s) carries the deposit line and word before Sign', async (extend) => {
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    const { agentsApi } = await import('@/api/agents')
    const { CLICK_RISK, DEPOSIT_LINE, DEPOSIT_RISK } = await import('@/api/click')
    const { RunnerGrantCard, StartAgentDialog } = await import('@/app/terminal/real/StartAgentDialog')
    const WS = 'b'.repeat(32)
    vi.spyOn(api, 'getWorkspaces').mockResolvedValue([{ id: WS, grant_hours: 8, click: { custody: 'convenience', enabled: true, available: true } }] as Awaited<ReturnType<typeof api.getWorkspaces>>)
    vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: null } as Awaited<ReturnType<typeof api.getHostSettings>>)
    vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'click' } as Awaited<ReturnType<typeof api.getSignDefault>>)
    const display = "codex 'work on DEMO-7'‮"
    vi.spyOn(agentsApi, 'preview').mockResolvedValue({
      spec: 'e'.repeat(64), ticket: 'DEMO-7', display, display_hidden: false, cwd: { kind: 'workspace' }, cwd_path: '/w/demo', cwd_path_hidden: false,
      will_create_worktree: false, login_shell: false, env: ['TERM'], grant: null, warnings: [],
      blocked: { code: 'launch.no_grant', message: 'none', detail: { reason: 'none' } },
    })
    vi.spyOn(agentsApi, 'runner').mockResolvedValue({ runner: { connected: true, enabled: true, terminals: false, grant: null } })
    const prepare = vi.spyOn(api, 'prepareClick').mockResolvedValue({ request_id: 'hr_' + '1'.repeat(26), expires_at: new Date(Date.now() + 6e5).toISOString(), op: 'grant', confirm: 'word' })
    vi.spyOn(api, 'viewClick').mockResolvedValue({ view_digest: 'sha256:' + 'd'.repeat(64), view: {
      risk: CLICK_RISK, op: 'grant', workspace: { id: WS, name: 'demo', prefix: 'DEMO' }, fingerprint: '0123456789ab', checks: [], artifacts: null,
      lines: ['grant', '', DEPOSIT_RISK, DEPOSIT_LINE], confirm: { kind: 'word', word: 'DEMO~bbbbbbbb/GRANT' }, expires_at: new Date(Date.now() + 6e5).toISOString(),
      auth: 'convenience', device: 'd_x', deposit: true, deposit_line: DEPOSIT_LINE } })
    vi.spyOn(api, 'cancelClick').mockResolvedValue({ id: 'x', state: 'cancelled' })
    render(<QueryClientProvider client={new QueryClient()}>{extend
      ? <RunnerGrantCard ws={WS} runner={{ enabled: true, connected: true, terminals: false, grant: null }} loading={false} blocked={null} extend={{ id: 'gr_old', scope: 'workable', until: new Date(nowMs() + 5 * 36e5).toISOString() }} onChanged={() => {}} />
      : <StartAgentDialog ws={WS} ticket="DEMO-7" open choose onClose={() => {}} />}</QueryClientProvider>)
    if (!extend) {
      const shown = await screen.findByTestId('agent-preview', {}, T)
      expect(shown.textContent).toContain("codex 'work on DEMO-7'\\u{202e}") // escaped, never repaired
      await waitFor(() => expect(screen.getByTestId('agent-start')).toBeEnabled(), T)
      screen.getByTestId('agent-start').click() // "Sign and start agent": the access is signed in the popover
    } else {
      await waitFor(() => expect(screen.getByTestId('runner-grant-issue')).toBeEnabled(), T)
      screen.getByTestId('runner-grant-issue').click()
    }
    const panel = await screen.findByTestId('click-panel', {}, T)
    expect(panel.textContent).toContain(DEPOSIT_LINE)
    expect(panel.textContent).toContain(DEPOSIT_RISK)
    expect(within(panel).getByTestId('typed-word')).toHaveTextContent('Type DEMO~bbbbbbbb/GRANT to confirm')
    expect(within(panel).getByTestId('access-length')).toHaveTextContent('Let agents work for you for 8 h.')
    expect(within(panel).getByRole('button', { name: extend ? 'Sign and extend' : 'Sign and start agent' })).toBeDisabled() // until the word is typed
    expect(prepare.mock.calls[0][1]).toEqual({ action: 'runner_grant', hours: 8, scope: 'workable', ...(extend ? { extend: 'gr_old' } : {}) })
    vi.restoreAllMocks()
  })
})

it('shows the host refusal and recovery hint when removing one click would strand a custody key', async () => {
  const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
  const { SignPopover } = await import('@/components/sign/SignPopover')
  const { ApiError } = await import('@/api/types')
  const message = 'sign_methods cannot leave out click while click-to-sign is on for this workspace: the key is in convenience custody'
  const hint = 'move the key back first: orch host convenience off --workspace W and orch keys custody passphrase --workspace W --in-place'
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([])
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: null } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'click', is_default: false, allowed: ['terminal', 'click', 'phone'] })
  const prepare = vi.spyOn(api, 'prepareHandoff').mockRejectedValue(new ApiError(400, { code: 'invalid.input', message, hint, retryable: false }))
  const click = vi.spyOn(api, 'prepareClick')
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SignPopover ws="w" title="Change allowed ways to approve" action={{ action: 'settings_set', sign_methods: ['terminal', 'phone'] }} autoStart onClose={() => {}} /></QueryClientProvider>)
  expect(await screen.findByText(message)).toBeVisible()
  expect(screen.getByText(hint)).toBeVisible()
  expect(prepare).toHaveBeenCalledWith('w', { action: 'settings_set', sign_methods: ['terminal', 'phone'] }, 0)
  expect(click).not.toHaveBeenCalled()
})


it('setup Create posts the review digest shown with core rows and terminal fingerprint', async () => {
  const { Setup } = await import('@/setup/Setup')
  const { SetupAPI } = await import('@/api/setup')
  const { fireEvent } = await import('@testing-library/react')
  const posted: unknown[] = []
  const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    let value: unknown = {ok:true}
    if (String(url).endsWith('/state')) value = {
      phase:'profile',mode:'finish',dir:'/demo',dir_name:'demo',suggested:{name:'Demo',prefix:'TST'},
      addons:[{id:'worktrees',title:'Worktrees',desc:'Copies',required:false,default:false,kind:'addon',capabilities:[]}],
      methods:['terminal'],click_available:false,click_reason:'',agent_hours:[8],
      relay:{available:false,enabled:false},missing:['addon:worktrees'],labels:{},
      current:{name:'Demo',prefix:'TST',addons_on:[],agent_hours:8,runner:{enabled:false,terminals:false},runner_grant:false,method:'terminal'},
    }
    if (String(url).endsWith('/review')) value = {
      review_id:'demo-review',digest:'demo-bound-digest',fingerprint:['demo','not','real'],
      items:[{id:'settings',title:'Settings',lines:['Agents: 8 hours'],details:['Exact settings'],volatile:['set at signing'],type:'settings.changed',fields:{set:{grant_hours:8}},placeholders:[],placeholder_details:[]}],
      runner_grant:null,flags:[],risk:null,typed_word:null,tty_confirm:true,
    }
    if (String(url).endsWith('/create')) { posted.push(JSON.parse(String(init?.body))); value = {run_id:'demo-run'} }
    if (String(url).includes('/progress')) value = {steps:[],done:false,error:null,retry:[],workspace:null}
    return new Response(JSON.stringify(value))
  })
  render(<Setup api={new SetupAPI('demo-not-a-real-token', fetcher as typeof fetch)}/>)
  await screen.findByRole('heading',{name:'Finish setup in demo'})
  fireEvent.click(screen.getByLabelText(/Worktrees/))
  fireEvent.click(screen.getByRole('button',{name:'Continue →'}))
  await screen.findByRole('heading',{name:'Review your setup'})
  expect(screen.getByText('Agents: 8 hours')).not.toBeVisible() // collapsed, but in the page
  fireEvent.click(screen.getByText('Show technical details'))
  expect(screen.getByText('Agents: 8 hours')).toBeVisible()
  expect(screen.getByText((_, el) => !!el?.classList.contains('words') && el.textContent === 'demo not real')).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Finish setup'}))
  await waitFor(() => expect(posted).toEqual([{review_id:'demo-review',digest:'demo-bound-digest'}]))
})



it('addon enable confirms the grant name and renders the workspace review without a ticket', async () => {
  const { workspaceConfirmWord, fingerprintOf, reviewDigestOf, requestIsConsistent } = await import('@/api/handoff')
  const { clickEligible } = await import('@/api/click')
  const { HandoffPanel } = await import('@/components/sign/HandoffPanel')
  const { fakeRequest } = await import('@/test/handoffHarness')
  const models = { codex: { work: 'gpt-5.4' } }
  const event = { id: 'event-settings', type: 'settings.changed', actor: { kind: 'person', id: 'p_owner', device: null }, auth: null, set: { models } }
  const material = {
    kind: 'payload' as const, ticket: null,
    review: [{ event_id: event.id, type: event.type, payload: { set: { models } }, derived: {} }],
    sequence: { digest: 'sha256:demo', of: 2, intent: { v: 1, kind: 'addon_enable', name: 'model-routing', settings: { models } } },
  }
  const request = { ...fakeRequest(), key: null, op: 'settings.set', events: [event], material,
    review_digest: reviewDigestOf(material), content: { headline: 'Turn on Model routing', notes: ['Choose GPT-5.4 for work. Settings first, then turn on Model routing.'] },
  }
  request.fingerprint = fingerprintOf(request.review_digest, request.events, request.op)
  const workspace = { prefix: 'DEMO', id: '0123456789abcdef0123456789abcdef' }
  expect(requestIsConsistent(request)).toBe(true)
  expect(workspaceConfirmWord(request, workspace)).toBe('model-routing~89abcdef')
  expect(clickEligible({ action: 'addon_enable', name: 'model-routing', models })).toBe(true)
  expect(clickEligible({ action: 'addon_enable', name: 'publish', publish: { server: 'https://files.example.test', share: 'https://links.example.test' } })).toBe(false) // Publish: terminal only
  expect(clickEligible({ action: 'addon_grant', name: 'publish' })).toBe(false)
  expect(clickEligible({ action: 'addon_grant', name: 'usage' })).toBe(true)
  render(<HandoffPanel request={request} workspace={workspace} />)
  expect(screen.getByTestId('handoff-command')).toHaveTextContent('orch sign')
  expect(screen.getByText('Choose GPT-5.4 for work. Settings first, then turn on Model routing.')).toBeVisible()
  expect(screen.getAllByText('model-routing~89abcdef').length).toBeGreaterThan(0)
})
it('reviews the exact task edit and relays that action through the existing handoff', async () => {
  const { fakeRequest, state } = await import('./handoffHarness')
  const ws = 'a'.repeat(32)
  const { fingerprintOf, reviewDigestOf } = await import('@/api/handoff')
  const action = { action: 'edit_sequence' as const, key: 'DEMO-0043', head_seq: 7, tasks: { edit: { T3: { text: 'Check every header', verify: null } }, add: [] } }
  const payload = { base_rev: { 'ticket.tasks': 'sha256:' + '0'.repeat(64) }, set: { 'ticket.tasks': [{ id: 'T3', text: 'Check every header', verify: null, proves: [] }] } }
  const event = { id: 'event-task', type: 'ticket.updated', actor: { kind: 'person', id: 'p_owner', device: null }, auth: null, ...payload }
  const material = { kind: 'payload' as const, ticket: null, review: [{ event_id: event.id, type: event.type, payload, derived: {} }] }
  const request = { ...fakeRequest(), op: 'task.edit', material, events: [event], review_digest: reviewDigestOf(material), content: { headline: 'Edit task', fields_changed: [{ field: 'text', before: 'Check header', after: 'Check every header' }, { field: 'check', before: 'check-header', after: null }] } }
  request.fingerprint = fingerprintOf(request.review_digest, request.events, request.op)
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([])
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: ['terminal'] } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'terminal', is_default: true, allowed: ['terminal'] })
  const prepare = vi.spyOn(api, 'prepareHandoff').mockResolvedValue(request)
  vi.spyOn(api, 'getHandoff').mockResolvedValue(state(request))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SignPopover ws={ws} title="Edit task" action={action} onClose={() => {}} /></QueryClientProvider>)
  const button = await screen.findByRole('button', { name: 'Sign in Terminal' })
  await waitFor(() => expect(button).toBeEnabled())
  button.click()
  await waitFor(() => expect(prepare).toHaveBeenCalled())
  expect(await screen.findByText('Check every header')).toBeVisible()
  expect(screen.getByText('(not set)')).toBeVisible()
  expect(prepare).toHaveBeenCalledWith(ws, action, 0)
})

// R3: each manual authority path uses the existing host signing dialog, posting only its reviewed intent.
describe('manual acceptance signing paths', () => {
  it.each(['accept', 'withdraw', 'done'] as const)('reviews and prepares %s without signing after a host refusal', async kind => {
    const { ManualDialog } = await import('@/app/pages/ticket/ManualAcceptance')
    const { refusal } = await import('./handoffHarness')
    const { setFeatures } = await import('@/api/features')
    const ws = 'a'.repeat(32)
    vi.spyOn(api, 'getWorkspaces').mockResolvedValue([])
    vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: ['terminal'] } as Awaited<ReturnType<typeof api.getHostSettings>>)
    vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'terminal' } as Awaited<ReturnType<typeof api.getSignDefault>>)
    const prepare = vi.spyOn(api, 'prepareHandoff').mockRejectedValue(refusal('role.denied', 403))
    const post = vi.spyOn(api, 'postAction')
    const ticket = { key: 'DEMO-0001' } as import('@/api/types').TicketDocument
    const ac = { id: 'AC2', text: 'Exact criterion wording' } as import('@/api/types').AcceptanceStatus
    const task = { id: 'T2', text: 'Exact task wording', lease: null } as import('@/api/types').TaskStatus
    const user = userEvent.setup()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><ManualDialog ticket={ticket} ws={ws} choice={kind === 'done' ? { kind, task } : { kind, ac }} onClose={() => {}} /></QueryClientProvider>)
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(kind === 'done' ? task.text : ac.text)
    if (kind !== 'done') await user.type(within(dialog).getByRole('textbox'), ' Checked personally ')
    const button = await within(dialog).findByRole('button', { name: 'Sign in Terminal' })
    await waitFor(() => expect(button).toBeEnabled())
    await user.click(button)
    const expected = kind === 'done' ? { action: 'mark_task_done', key: ticket.key, task: 'T2' }
      : kind === 'accept' ? { action: 'accept_criterion', key: ticket.key, ac: 'AC2', note: 'Checked personally' }
      : { action: 'withdraw_acceptance', key: ticket.key, ac: 'AC2', reason: 'Checked personally' }
    await waitFor(() => expect(prepare).toHaveBeenCalledWith(ws, expected, 0))
    expect(post).not.toHaveBeenCalled()
    setFeatures(null)
  })
})

it('market approval (terminal fallback) displays the exact package name and stage sent; it is click-eligible, except Publish', async () => {
  const { workspaceRequest } = await import('./hostHarness')
  const { state } = await import('./handoffHarness')
  const { clickEligible } = await import('@/api/click')
  const { visible } = await import('@/components/sign/visible')
  const action = { action: 'market_grant' as const, name: 'calendar', staged: 'demo-stage\u202e' }
  const request = workspaceRequest('addon.grant', { type: 'addon.granted', name: action.name, staged: action.staged })
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([])
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: ['terminal'] } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'terminal', is_default: true, allowed: ['terminal'] })
  const prepare = vi.spyOn(api, 'prepareHandoff').mockResolvedValue(request)
  vi.spyOn(api, 'getHandoff').mockResolvedValue(state(request))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SignPopover ws={"a".repeat(32)} title="Approve add-on package" action={action} autoStart onClose={() => {}} /></QueryClientProvider>)
  await waitFor(() => expect(prepare).toHaveBeenCalledWith('a'.repeat(32), action, 0))
  expect(await screen.findByText(visible(action.staged), { exact: false })).toBeVisible()
  expect(screen.getAllByText(action.name, { exact: false }).length).toBeGreaterThan(0)
  expect(clickEligible(action)).toBe(true) // V3-D38: one click where click-to-sign is on, this Terminal review where it is off
  expect(clickEligible({ ...action, name: 'publish' })).toBe(false)
})

it('a Terminal-only action with One click on shows the Terminal hand-off: why line, exact command, pending, no click prepare', async () => {
  const { workspaceRequest } = await import('./hostHarness')
  const { state } = await import('./handoffHarness')
  const ws = 'b'.repeat(32)
  const action = { action: 'publish_setup' as const, server: 'https://files.example.test', share: 'https://links.example.test' }
  const request = workspaceRequest('settings.set', { type: 'settings.changed', publish: { server: action.server, share: action.share } })
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([{ id: ws, click: { custody: 'convenience', enabled: true, available: true } }] as unknown as Awaited<ReturnType<typeof api.getWorkspaces>>)
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: ['terminal', 'click'] } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'click', is_default: true, allowed: ['terminal', 'click'] })
  const clickPrepare = vi.spyOn(api, 'prepareClick')
  const prepare = vi.spyOn(api, 'prepareHandoff').mockResolvedValue(request)
  vi.spyOn(api, 'getHandoff').mockResolvedValue(state(request))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SignPopover ws={ws} title="Turn on Publish" action={action} autoStart onClose={() => {}} /></QueryClientProvider>)
  await waitFor(() => expect(prepare).toHaveBeenCalledWith(ws, action, 0))
  expect(clickPrepare).not.toHaveBeenCalled()
  expect(await screen.findByTestId('terminal-why')).toHaveTextContent('One click can\u2019t approve this one. Approve it in Terminal on this Mac.')
  expect(screen.getByTestId('handoff-command')).toHaveTextContent(`orch sign ${request.code}`)
  expect(screen.getByRole('button', { name: 'Copy command' })).toBeVisible()
  expect(screen.getByText('Waiting for approval in Terminal…')).toBeVisible()
  expect(document.body).not.toHaveTextContent(/convenience off|turn One click off/i)
})
