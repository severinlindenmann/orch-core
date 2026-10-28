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

const TITLES = ['Start here', 'Today, Board and Tickets', 'A ticket and quick tasks', 'Approving things', 'Agents and their access', 'Worktrees, Activity and add-ons', 'Settings', 'Repositories', 'Worktrees', 'Shares and Publish', 'Coming later', 'Keyboard shortcuts']

describe('guide package', () => {
  it('starts installed and granted in every demo workspace; open is a viewer action', () => {
    const { store } = setup()
    const pkg = store.addons.find((a) => a.name === 'guide')!
    expect(pkg.capabilities).toEqual([])
    expect(pkg.actions?.open).toMatchObject({ minRole: 'viewer' })
    for (const w of store.workspaces) expect(w.addons.guide).toMatchObject({ enabled: true, status: 'active', installed: true })
  })
  it('has the twelve pages, in order', async () => {
    const st = await state(setup())
    expect(st.pages.map((p) => p.title)).toEqual(TITLES)
    expect(st.items.map((i) => i.title)).toEqual(TITLES)
    for (const p of st.pages) expect(p.markdown.length).toBeGreaterThan(200)
  })
})

describe('guide navigation is per viewer', () => {
  it('opens a page for the viewer who asked, and a viewer may do it', async () => {
    const s = setup('p_tom')
    expect((await state(s)).current.title).toBe('Start here')
    await s.api.runAddonAction(s.ws, 'guide', 'open', { slug: 'approving' })
    expect((await state(s)).current.title).toBe('Approving things')
    s.store.setViewer('p_sev')
    expect((await state(s)).current.title).toBe('Start here')
  })
})

describe('route to help page', () => {
  it('maps routes as the brief says; anything else is Getting around', async () => {
    const { routes } = (await state(setup())).help
    const t = (path: string) => helpPageFor(routes, path)
    expect(t('/')).toBe('today-board-tickets')
    expect(t('/board')).toBe('today-board-tickets')
    expect(t('/tickets/new')).toBe('today-board-tickets')
    expect(t('/ticket/DEMO-0043')).toBe('a-ticket')
    expect(t('/agents')).toBe('agents')
    expect(t('/settings/members')).toBe('settings')
    expect(t('/addon/wiki/pages')).toBe('more-tools')
    expect(t('/nope')).toBe('start-here')
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

describe('what the pages promise', () => {
  it('says coming later for relay, phone and auto-close, and approving is One click with no terminal hand-off', async () => {
    const pages = (await state(setup())).pages
    const later = pages.find((p) => p.slug === 'later')!.markdown.toLowerCase()
    for (const w of ['relay', 'phone', 'close themselves']) expect(later, w).toContain(w)
    const approving = pages.find((p) => p.slug === 'approving')!.markdown
    expect(approving).toContain('One click')
    expect(approving).not.toMatch(/orch sign|terminal/i)
  })
  it('has no raw ids', async () => {
    for (const p of (await state(setup())).pages) expect(p.markdown, p.slug).not.toMatch(/\b[A-Z]{2,6}-\d{3,}\b|p_[a-z]+|ws_/)
  })
})
