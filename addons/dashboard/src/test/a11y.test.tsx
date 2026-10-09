import { render, screen, waitFor } from '@testing-library/react'
import axe from 'axe-core'
import { describe, expect, it } from 'vitest'
import type { MockStore } from '@/mocks/store'
import { installAndGrant } from './installAddon'
import { renderApp } from './renderApp'

// jsdom cannot compute colours, so contrast is checked in the browser pass instead.
const T = { timeout: 8000 }
const demo = (s: MockStore) => s.workspaces.find((w) => w.prefix === 'DEMO')!.id

// [path, h1 title, text that only the loaded page content shows]. A mistyped page id lands on "Page not found",
// so every case names its title and a content marker, and the run waits for the skeletons to go before axe looks.
const ADDON_PAGES: [string, RegExp, RegExp][] = [
  ['publish/shares', /Apps & shares/, /Live shares/],
  ['github/reviews', /Code reviews/, /Open PRs/],
  ['usage/overview', /Usage/, /Month to date/],
  ['wiki/pages', /Wiki/, /Tariff data conventions/],
  ['terminals/sessions', /Terminals/, /view-only mirrors/],
  ['worktrees/worktrees', /Worktrees/, /With changes/],
  ['quick/quick', /Quick tasks/, /Quick task \(one line\)/],
  ['records/records', /Records/, /Record changes/],
  ['activity/activity', /Activity/, /events today/],
  ['widgets/widgets', /Widgets/, /small visual blocks/],
  ['start-agent/start', /Start agent/, /Start an agent session on a ticket/],
  ['guide/guide', /Guide/, /Getting around/],
  ['factory/factory', /AI Factory/, /Children used/],
  ['schedules/schedules', /Schedules/, /Check inbox/],
]
const ADDONS = [...new Set(ADDON_PAGES.map(([p]) => p.split('/')[0]))]

/** Every catalog addon installed, granted and enabled (the ones already installed are left alone). */
function installAll(s: MockStore) {
  const ws = demo(s)
  for (const name of ADDONS) {
    const a = s.workspaceAddons(ws).find((x) => x.name === name)
    if (!a || !a.ws.enabled) {
      if (!a) installAndGrant(s, ws, name)
      else {
        const e = s.addonOp(ws, name, { op: 'enable' }, { kind: 'person', id: 'p_sev' })
        if (!e.ok) throw new Error(`enable ${name}: ${e.message}`)
      }
    }
  }
}

async function violations() {
  // Addon HTML frames are sandboxed documents jsdom cannot hand to axe; their title is asserted instead.
  for (const f of document.querySelectorAll('iframe')) expect(f.getAttribute('title'), 'iframe title').toBeTruthy()
  const r = await axe.run({ include: [document.body], exclude: [['iframe']] }, { rules: { 'color-contrast': { enabled: false } } })
  return r.violations
    .filter((v) => v.impact === 'serious' || v.impact === 'critical')
    .map((v) => `${v.impact} ${v.id}: ${v.help}\n${v.nodes.map((n) => `  ${n.target.join(' ')}  ${n.html.slice(0, 160)}`).join('\n')}`)
}

type User = ReturnType<typeof renderApp>['user']

/** Renders the route, waits for its title and content marker and for every loading skeleton to go, then runs axe. */
async function check(path: string, title: RegExp, marker: RegExp, open?: (user: User) => Promise<void>) {
  const { user } = renderApp(path, { viewer: 'p_sev', setup: installAll })
  await screen.findByRole('heading', { level: 1, name: title }, T)
  await open?.(user)
  await screen.findAllByText(marker, {}, T)
  await waitFor(() => expect(document.querySelectorAll('[data-slot="skeleton"], [aria-busy="true"]')).toHaveLength(0), T)
  expect(screen.queryByText('Page not found')).toBeNull()
  expect(screen.queryByText(/is not enabled in/)).toBeNull()
  expect(await violations()).toEqual([])
}

describe('accessibility smoke (axe, no serious or critical violations)', () => {
  it('the check itself catches an unnamed button and an unlabelled input', async () => {
    render(<div><button type="button" /><input /></div>)
    const found = (await violations()).join('\n')
    expect(found).toMatch(/button-name/)
    expect(found).toMatch(/label/)
  })

  it('a mistyped addon page id fails the check instead of passing on "Page not found"', async () => {
    await expect(check('/addon/quick/nope', /Quick tasks/, /Quick task \(one line\)/)).rejects.toThrow()
  })

  const routes: [string, RegExp, RegExp][] = [
    ['/', /^Today$/, /need you/],
    ['/board', /^Board$/, /Backlog/],
    ['/tickets', /^Tickets$/, /All tickets/],
    ['/tickets/new', /^New ticket$/, /Out of scope/],
    ['/agents', /^Agents$/, /Issue grant/],
    ['/settings/general', /^Settings$/, /Key fingerprint/],
    ['/settings/members', /^Settings$/, /Add member/],
    ['/settings/gates', /^Settings$/, /Approvals already given stay valid/],
    ['/settings/relay', /^Settings$/, /Everything on this tab is simulated/],
    ['/artifacts', /^Artifacts$/, /tariff-export\.log/],
    ['/settings/addons', /^Settings$/, /Browse addons/],
    ['/settings/addon/estimate', /^Settings$/, /Scale/],
  ]
  it.each(routes)('%s', async (path, title, marker) => {
    await check(path, title, marker)
  })

  const tabs: [string, RegExp][] = [
    ['Overview', /Tariff tables live in a shared drive/],
    ['Acceptance', /Acceptance criteria/],
    ['Questions', /Should .valid_from. in the seeds be a DATE/],
    ['Artifacts', /tariff-export\.log/],
    ['History', /Timeline/],
    ['Raw', /Ticket document/],
  ]
  it.each(tabs)('ticket DEMO-0043, %s tab', async (tab, marker) => {
    await check('/ticket/DEMO-0043', /^Load tariff tables as dbt seeds$/, marker, async (user) => {
      const t = await screen.findByRole('tab', { name: new RegExp(`^${tab}`) }, T)
      await user.click(t)
      await waitFor(() => expect(t).toHaveAttribute('aria-selected', 'true'))
    })
  }, 30_000) // the Raw tab renders the whole ticket document as highlighted JSON; axe needs longer than the default 10 s

  it.each(ADDON_PAGES)('addon page %s', async (p, title, marker) => {
    await check(`/addon/${p}`, title, marker)
  })
})
