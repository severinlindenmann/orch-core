import { render, screen } from '@testing-library/react'
import axe from 'axe-core'
import { describe, expect, it } from 'vitest'
import type { MockStore } from '@/mocks/store'
import { installAndGrant } from './installAddon'
import { renderApp } from './renderApp'

// jsdom cannot compute colours, so contrast is checked in the browser pass instead.
const T = { timeout: 8000 }
const demo = (s: MockStore) => s.workspaces.find((w) => w.prefix === 'DEMO')!.id

const ADDON_PAGES = [
  'publish/shares', 'github/reviews', 'usage/overview', 'wiki/pages', 'terminals/sessions', 'worktrees/worktrees', 'quick/quick',
  'records/records', 'activity/activity', 'widgets/widgets', 'start-agent/start', 'guide/guide', 'factory/factory', 'schedules/schedules',
]
const ADDONS = [...new Set(ADDON_PAGES.map((p) => p.split('/')[0]))]

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

async function check(path: string, heading?: string | RegExp, open?: (user: ReturnType<typeof renderApp>['user']) => Promise<void>) {
  const { user } = renderApp(path, { viewer: 'p_sev', setup: installAll })
  await screen.findByRole('heading', { level: 1, ...(heading ? { name: heading } : {}) }, T)
  await open?.(user)
  expect(await violations()).toEqual([])
}

describe('accessibility smoke (axe, no serious or critical violations)', () => {
  it('the check itself catches an unnamed button and an unlabelled input', async () => {
    render(<div><button type="button" /><input /></div>)
    const found = (await violations()).join('\n')
    expect(found).toMatch(/button-name/)
    expect(found).toMatch(/label/)
  })

  const routes: [string, string | RegExp | undefined][] = [
    ['/', undefined],
    ['/board', undefined],
    ['/tickets', undefined],
    ['/tickets/new', undefined],
    ['/agents', undefined],
    ['/settings/general', undefined],
    ['/settings/members', undefined],
    ['/settings/gates', undefined],
    ['/settings/addons', undefined],
    ['/settings/addon/estimate', undefined],
  ]
  it.each(routes)('%s', async (path, heading) => {
    await check(path, heading)
  })

  const tabs = ['Overview', 'Acceptance', 'Questions', 'Artifacts', 'History', 'Raw']
  it.each(tabs)('ticket DEMO-0043, %s tab', async (tab) => {
    await check('/ticket/DEMO-0043', /DEMO-0043|./, async (user) => {
      await user.click(await screen.findByRole('tab', { name: new RegExp(`^${tab}`) }, T))
      await screen.findByRole('tabpanel', undefined, T)
    })
  })

  it.each(ADDON_PAGES)('addon page %s', async (p) => {
    await check(`/addon/${p}`)
  })
})
