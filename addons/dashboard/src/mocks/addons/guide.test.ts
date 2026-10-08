import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { helpPageFor, type HelpRoute } from '@/api/guide'
import { createMockTransport } from '@/api/transport'
import { SHORTCUTS } from '@/app/shell/shortcuts'
import { createMockStore } from '@/mocks/store'

const setup = (viewer = 'p_sev') => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  store.setViewer(viewer)
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
interface Page { slug: string; title: string; markdown: string }
interface State {
  pages: Page[]
  items: { title: string }[]
  current: Page
  help: { routes: HelpRoute[]; pages: Page[] }
}
const state = async (s: ReturnType<typeof setup>) => (await s.api.getAddonState(s.ws, 'guide')) as unknown as State

const TITLES = ['Getting around', 'Tickets and sections', 'Gates and approvals', 'Agents and grants', 'Questions', 'Addons and the orange A', 'Keyboard shortcuts', 'What is simulated in this mockup']

describe('guide package', () => {
  it('starts installed and granted in every demo workspace; open is a viewer action', () => {
    const { store } = setup()
    const pkg = store.addons.find((a) => a.name === 'guide')!
    expect(pkg.capabilities).toEqual([])
    expect(pkg.actions?.open).toMatchObject({ minRole: 'viewer' })
    for (const w of store.workspaces) expect(w.addons.guide).toMatchObject({ enabled: true, status: 'active', installed: true })
  })
  it('has the eight pages, in order', async () => {
    const st = await state(setup())
    expect(st.pages.map((p) => p.title)).toEqual(TITLES)
    expect(st.items.map((i) => i.title)).toEqual(TITLES)
    for (const p of st.pages) expect(p.markdown.length).toBeGreaterThan(200)
  })
})

describe('guide navigation is per viewer', () => {
  it('opens a page for the viewer who asked, and a viewer may do it', async () => {
    const s = setup('p_tom')
    expect((await state(s)).current.title).toBe('Getting around')
    await s.api.runAddonAction(s.ws, 'guide', 'open', { slug: 'questions' })
    expect((await state(s)).current.title).toBe('Questions')
    s.store.setViewer('p_sev')
    expect((await state(s)).current.title).toBe('Getting around')
  })
})

describe('route to help page', () => {
  it('maps routes as the brief says; anything else is Getting around', async () => {
    const { routes } = (await state(setup())).help
    const t = (path: string) => helpPageFor(routes, path)
    expect(t('/')).toBe('getting-around')
    expect(t('/board')).toBe('tickets-and-sections')
    expect(t('/tickets')).toBe('tickets-and-sections')
    expect(t('/tickets/new')).toBe('tickets-and-sections')
    expect(t('/ticket/DEMO-0043')).toBe('gates-and-approvals')
    expect(t('/agents')).toBe('agents-and-grants')
    expect(t('/settings/addons')).toBe('addons')
    expect(t('/settings/addon/wiki')).toBe('addons')
    expect(t('/addon/wiki/pages')).toBe('addons')
    expect(t('/settings/members')).toBe('getting-around')
    expect(t('/nope')).toBe('getting-around')
    for (const r of routes) expect((await state(setup())).help.pages.some((p) => p.slug === r.page)).toBe(true)
  })
})

describe('Keyboard shortcuts page is generated from the shortcut registry', () => {
  it('lists every registry shortcut (keys and label), including g b and ?', async () => {
    const md = (await state(setup())).pages.find((p) => p.title === 'Keyboard shortcuts')!.markdown
    expect(md).toContain('g b')
    for (const sh of SHORTCUTS.filter((x) => !x.id.startsWith('workspace.'))) {
      expect(md, sh.id).toContain(sh.keys)
      expect(md, sh.id).toContain(sh.label)
    }
    expect(SHORTCUTS.some((x) => x.keys === '?')).toBe(true)
    expect(md).toMatch(/workspace/i)
  })
})

describe('What is simulated', () => {
  it('names each simulated thing', async () => {
    const md = (await state(setup())).pages.find((p) => p.title === 'What is simulated in this mockup')!.markdown
    for (const w of ['Touch ID', 'terminal', 'GitHub', 'relay', 'agent run', '2026-10-09 11:30', 'reset']) expect(md.toLowerCase(), w).toContain(w.toLowerCase())
  })
})
