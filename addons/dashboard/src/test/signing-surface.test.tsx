// The signing and core-confirm paths of the mock, in one table (the list is exactly the CASES below):
//  (a) the dialog's title and its Covers hold core's words only: nothing the addon wrote, except its name as
//      "Title (package id)"; what the addon wrote is shown, in full, in a labelled "From the addon" region instead;
//  (b) the body that is posted equals what the dialog showed, as key + value pairs: an addon-picked arg is core's
//      line for that exact key with that exact value (in the covers or core's "Sends" list, never inside the addon's
//      region); a core field is the line core renders for it.
import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { wordsAndId } from '@/addon-ui/SignConfirm'
import { api } from '@/api/client'
import { approversText } from '@/api/gates'
import { getAddon } from '@/mocks/addons'
import type { MockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'

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
    name: 'gate approve',
    path: '/ticket/DEMO-0044',
    open: (user) => ticketAction(user, /Approve plan/),
    confirm: press('Approve plan'),
    method: 'postAction',
    arg: 1,
    skip: ['action'],
    shown: { gate: (v) => `Approve ${v}` },
  },
  {
    name: 'answer',
    path: '/ticket/DEMO-0043',
    open: async (user) => {
      await user.click(await screen.findByRole('tab', { name: /Questions/ }, T))
      await user.click(await screen.findByRole('radio', { name: /DATE \(local midnight\)/ }, T))
      await user.click(within(document.getElementById('question-Q2')!).getByRole('button', { name: 'Answer Q2' }))
      return screen.findByRole('dialog', {}, T)
    },
    confirm: press('Send answer'),
    method: 'postAction',
    arg: 1,
    skip: ['action'],
    shown: { question: (v) => `Question ${v}:`, option: (v) => `(option ${v})` },
  },
  {
    name: 'verdict',
    path: '/ticket/DEMO-0041',
    open: async (user) => {
      const dialog = await ticketAction(user, /Give verdict/)
      await user.click(within(dialog).getByRole('radio', { name: /Pass · the evidence is enough/ }))
      return dialog
    },
    confirm: press('Pass'),
    method: 'postAction',
    arg: 1,
    skip: ['action'],
    shown: { result: (v) => `${v[0].toUpperCase()}${v.slice(1)} · the evidence` },
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
    shown: { id: (v) => `Decision ${v}`, option: (v) => `Answer: option ${v}`, ticket: (v) => `About ${v}` },
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
      await user.click(within(drawer).getByRole('button', { name: 'Grant TARIFF_WEBHOOK_SECRET…' }))
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
    const regions = [...dialog.querySelectorAll('[aria-label^="From addon"], [aria-label^="From the addon"]')]
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
    if (c.then && next) {
      await waitFor(() => expect(next).toHaveBeenCalled(), T)
      for (const [k, v] of leaves(next.mock.calls.at(-1)![c.then.arg]).filter(([x]) => !(c.then!.skip ?? []).includes(x))) expectShown(texts, k, v, c.then.shown[k])
    }
  }, 40_000)
})
