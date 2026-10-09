// Every signing (and core confirm) path the mock has, in one table:
//  (a) the dialog's title and its Covers hold core's words only: nothing the addon wrote, except its name as
//      "Title (package id)"; what the addon wrote is shown in a labelled "From the addon" region instead;
//  (b) the body that is posted equals what the dialog showed: every value (and, for addon-chosen args, every key).
import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { MockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'

const T = { timeout: 8000 }
type User = ReturnType<typeof renderApp>['user']
type Method = 'postAction' | 'runAddonAction' | 'issueGrant' | 'grantSkillCredentials' | 'postAddonOp' | 'postRelay'

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
  /** Strings the addon wrote that this dialog shows: never in the title or covers, always inside a labelled region. */
  addon?: string[]
  /** Body keys that are core's protocol or plumbing (not values the person chose or the addon picked). */
  skip?: string[]
  /** Check the body's keys too (args the addon picked are shown with their exact key; `ticket` is core's own, said as "About …"). */
  keys?: boolean
  /** A second request the same signature leads to (starting the agent after the grant). */
  then?: { method: Method; arg: number; skip?: string[] }
}

const wsOf = (s: MockStore) => s.workspaces.find((w) => w.prefix === 'DEMO')!.id
const dialogNamed = (name: string | RegExp) => screen.findByRole('dialog', { name }, T)
const press = (name: string | RegExp) => async (user: User, dialog: HTMLElement) => user.click(within(dialog).getByRole('button', { name }))

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

const CASES: Case[] = [
  {
    name: 'gate approve',
    path: '/ticket/DEMO-0044',
    open: (user) => ticketAction(user, /Approve plan/),
    confirm: press('Approve plan'),
    method: 'postAction',
    arg: 1,
    skip: ['action'],
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
    keys: true,
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
    confirm: press('Create show-once link'),
    method: 'runAddonAction',
    arg: 3,
    addon: ['Create show-once link', 'The link is shown once'],
    keys: true,
  },
  {
    name: 'addon destructive (confirm: destructive)',
    path: '/addon/publish/shares',
    open: async (user) => {
      const row = (await screen.findByText('Billing explorer', {}, T)).closest('tr')!
      await user.click(within(row).getByRole('button', { name: 'Stop' }))
      return screen.findByRole('alertdialog', {}, T)
    },
    confirm: press('Stop app'),
    method: 'runAddonAction',
    arg: 3,
    addon: ['Stop app', 'Anyone using the app loses it', 'Billing explorer'],
    keys: true,
  },
  {
    name: 'start agent (grant + start)',
    path: '/ticket/DEMO-0044',
    wide: true,
    setup: (s) => void s.revokeGrant(wsOf(s), 'gr_01J9Z8', { kind: 'person', id: 'p_sev' }),
    open: async (user) => {
      await openTicketPanel(user, 'Start agent')
      await user.click(await screen.findByRole('button', { name: 'Start' }, T))
      return dialogNamed('Sign a grant and start Claude Code on DEMO-0044')
    },
    confirm: press('Sign and start'),
    method: 'issueGrant',
    arg: 1,
    then: { method: 'runAddonAction', arg: 3, skip: ['confirmed'] },
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
  },
  {
    name: 'addon install',
    path: '/settings/addons',
    open: async (user) => {
      await user.click(await screen.findByRole('button', { name: 'Browse addons' }, T))
      await user.click(within(await screen.findByRole('article', { name: /Quick tasks/ }, T)).getByRole('button', { name: 'Install' }))
      return dialogNamed(/^Install Quick tasks \(quick\)/)
    },
    confirm: press('Grant and turn on'),
    method: 'postAddonOp',
    arg: 2,
    skip: ['op', 'enable'],
  },
  {
    name: 'addon update',
    path: '/settings/addons',
    open: async (user) => {
      await user.click(within(await screen.findByRole('row', { name: /GitHub/ }, T)).getByRole('button', { name: /Update to 0\.6\.0/ }))
      return dialogNamed(/^Update GitHub \(github\) to 0\.6\.0/)
    },
    confirm: press('Update'),
    method: 'postAddonOp',
    arg: 2,
    skip: ['op'],
  },
  {
    name: 'relay op (remove a device)',
    path: '/settings/relay',
    open: async (user) => {
      const row = (await screen.findByText("Tom's iPad", {}, T)).closest('tr')!
      await user.click(within(row).getByRole('button', { name: 'Remove' }))
      return dialogNamed(/Remove Tom's iPad/)
    },
    confirm: press('Sign and remove'),
    method: 'postRelay',
    arg: 1,
    skip: ['op'],
  },
]

/** [key, value] for every scalar leaf of a body (arrays and nested objects flattened). */
function leaves(body: unknown, key = ''): [string, string][] {
  if (body === undefined || body === null) return []
  if (Array.isArray(body)) return body.flatMap((v) => leaves(v, key))
  if (typeof body === 'object') return Object.entries(body as Record<string, unknown>).flatMap(([k, v]) => leaves(v, k))
  return [[key, String(body)]]
}

/** Text of the dialog outside every region labelled as the addon's. */
function coreText(el: Element): string {
  const copy = el.cloneNode(true) as Element
  for (const r of copy.querySelectorAll('[aria-label^="From addon"], [aria-label^="From the addon"]')) r.remove()
  return copy.textContent ?? ''
}

beforeAll(async () => {
  await Promise.all([import('@/app/pages/ticket'), import('@/app/pages/today'), import('@/app/pages/agents'), import('@/app/pages/AddonPage')])
}, 30_000)
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('signing surface: every dialog shows exactly what is signed, in core\'s words', () => {
  it.each(CASES)('$name', async (c) => {
    if (c.wide) vi.stubGlobal('innerWidth', 1440)
    const spy = vi.spyOn(api, c.method)
    const next = c.then ? vi.spyOn(api, c.then.method) : null
    const { user } = renderApp(c.path, { viewer: c.viewer ?? 'p_sev', setup: c.setup })
    const dialog = await c.open(user)
    const title = within(dialog).getAllByRole('heading')[0].textContent ?? ''
    const covers = within(dialog).queryByText('Covers')?.nextElementSibling?.textContent ?? ''
    const shown = dialog.textContent ?? ''

    // (a) Core's words only in the title and covers; the addon's words are shown, inside a labelled region.
    for (const s of c.addon ?? []) {
      expect(title, `title has "${s}"`).not.toContain(s)
      expect(covers, `covers have "${s}"`).not.toContain(s)
      expect(shown, `"${s}" is not shown at all`).toContain(s)
      const regions = [...dialog.querySelectorAll('[aria-label^="From addon"], [aria-label^="From the addon"]')]
      expect(regions.some((r) => r.textContent?.includes(s)), `"${s}" is outside a labelled addon region`).toBe(true)
    }
    expect(coreText(dialog)).toBeTruthy()

    // (b) Nothing is posted before the confirm; then the body is what the dialog showed.
    expect(spy).not.toHaveBeenCalled()
    await c.confirm(user, dialog)
    await waitFor(() => expect(spy).toHaveBeenCalled(), T)
    const body = spy.mock.calls[0][c.arg]
    const sent = leaves(body).filter(([k]) => !(c.skip ?? []).includes(k))
    expect(sent.length, 'the body carries something').toBeGreaterThan(0)
    const check = (pairs: [string, string][], keys?: boolean) => {
      for (const [k, v] of pairs) {
        expect(shown.toLowerCase(), `${k}=${v} was posted but not shown`).toContain(v.toLowerCase())
        if (keys && k !== 'ticket') expect(shown, `key ${k} was posted but not shown`).toContain(k)
      }
    }
    check(sent, c.keys)
    if (c.then && next) {
      await waitFor(() => expect(next).toHaveBeenCalled(), T)
      check(leaves(next.mock.calls[0][c.then.arg]).filter(([k]) => !(c.then!.skip ?? []).includes(k)))
    }
  }, 30_000)
})
