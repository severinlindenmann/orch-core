import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { ApiError } from '@/api/types'
import { viewerActions } from '@/api/addons'
import { getAddon } from './addons'
import { createMockStore } from './store'

const setup = (viewer = 'p_sev') => {
  const store = createMockStore({ persist: false })
  store.setViewer(viewer)
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws }
}
const fail = async (p: Promise<unknown>) => {
  try {
    await p
    return null
  } catch (e) {
    return e instanceof ApiError ? e : null
  }
}

describe('addon state view hides per-viewer keys', () => {
  it('does not return the raw nav map of wiki or terminals', async () => {
    const { api, ws } = setup()
    await api.runAddonAction(ws, 'wiki', 'search', { query: 'deploy' })
    const wiki = await api.getAddonState(ws, 'wiki')
    expect(Object.keys(wiki)).not.toContain('nav')
    expect(Object.keys(await api.getAddonState(ws, 'terminals'))).not.toContain('nav')
  })
  it('strips nav generically, from any addon', () => {
    const { store, ws } = setup()
    store.addonState(ws, 'estimate').nav = { p_tom: { current: 'x' } }
    expect(store.addonStateView(ws, 'estimate')).not.toHaveProperty('nav')
  })
})

describe('view() does not mutate state on read', () => {
  it.each(['wiki', 'terminals'])('%s', (name) => {
    const { store, ws } = setup()
    const state: Record<string, unknown> = { ...structuredClone(getAddon(name)!.seed(ws, store)) }
    delete state.nav
    const before = JSON.stringify(state)
    getAddon(name)!.view?.(state, { store, ws, viewer: 'p_sev' })
    expect(JSON.stringify(state)).toBe(before)
    expect(state).not.toHaveProperty('nav')
  })
})

describe('runAddon', () => {
  it('says viewers cannot do this when a member-level action is refused', async () => {
    const { api, ws } = setup('p_tom')
    const e = await fail(api.runAddonAction(ws, 'publish', 'share', { ticket: 'DEMO-0041' }))
    expect(e).toMatchObject({ status: 403 })
    expect(e!.message).toBe('Viewers cannot do this.')
  })
  it.each(['constructor', 'toString', 'hasOwnProperty', '__proto__'])('treats the inherited name %s as an unknown action (404)', async (id) => {
    const { api, ws } = setup()
    expect(await fail(api.runAddonAction(ws, 'estimate', id, {}))).toMatchObject({ status: 404 })
  })
})

describe('signed grant payload covers the viewer-level actions', () => {
  it('lists the viewer actions of a package with their labels', () => {
    const { store } = setup()
    const wiki = store.addons.find((a) => a.name === 'wiki')!
    expect(viewerActions(wiki)).toEqual([{ id: 'open', label: 'Open page' }, { id: 'search', label: 'Search' }])
  })
  it('refuses a grant that omits or changes the viewer actions (409 addon.changed)', async () => {
    const { store, api, ws } = setup()
    store.appendWs(ws, { type: 'addon.updated', name: 'wiki', version: '0.1.4', package_sha256: 'c'.repeat(64), capabilities: [] })
    const st = store.workspaceAddons(ws).find((a) => a.name === 'wiki')!.ws
    const g = { op: 'grant' as const, version: st.version, package_sha256: st.package_sha256, capabilities: st.capabilities }
    expect(await fail(api.postAddonOp(ws, 'wiki', { ...g, viewer_actions: [] }))).toMatchObject({ status: 409, code: 'addon.changed' })
    expect(await fail(api.postAddonOp(ws, 'wiki', { ...g, viewer_actions: ['open'] }))).toMatchObject({ status: 409, code: 'addon.changed' })
    expect(await api.postAddonOp(ws, 'wiki', { ...g, viewer_actions: ['search', 'open'] })).toMatchObject({ name: 'wiki' })
  })
  it('refuses an update that does not list the new viewer action it adds', async () => {
    const { store, api, ws } = setup()
    const gh = store.addons.find((a) => a.name === 'github')!
    gh.update!.actions = { ...gh.actions, refresh: { minRole: 'viewer', label: 'Refresh' } }
    const u = { op: 'update' as const, version: gh.update!.version, package_sha256: gh.update!.package_sha256, capabilities: gh.update!.capabilities }
    expect(await fail(api.postAddonOp(ws, 'github', { ...u, viewer_actions: ['open'] }))).toMatchObject({ status: 409, code: 'addon.changed' })
    expect(await api.postAddonOp(ws, 'github', { ...u, viewer_actions: ['open', 'refresh'] })).toMatchObject({ name: 'github' })
  })
})

describe('the signed manifest is the one enforced after an update', () => {
  const ok = async (p: Promise<unknown>) => ((await fail(p)) ? 'refused' : 'ok')
  it('an update that removes a viewer action takes it away from viewers after the re-grant', async () => {
    const { store, api, ws } = setup()
    const wiki = store.addons.find((a) => a.name === 'wiki')!
    wiki.update = { version: '0.2.0', capabilities: [], package_sha256: 'd'.repeat(64), changelog: 'x', actions: { save_settings: { minRole: 'owner' }, open: { minRole: 'viewer', label: 'Open page' } } }
    const base = { version: '0.2.0', package_sha256: 'd'.repeat(64), capabilities: [] as string[], viewer_actions: ['open'] }
    await api.postAddonOp(ws, 'wiki', { op: 'update', ...base })
    await api.postAddonOp(ws, 'wiki', { op: 'grant', ...base })
    store.setViewer('p_tom')
    expect(await ok(api.runAddonAction(ws, 'wiki', 'search', { query: 'a' }))).toBe('refused')
    expect(await fail(api.runAddonAction(ws, 'wiki', 'search', { query: 'a' }))).toMatchObject({ status: 403 })
    expect(await ok(api.runAddonAction(ws, 'wiki', 'open', { slug: 'glossary' }))).toBe('ok')
  })
  it('an update that adds one lets viewers run it after the re-grant, not before', async () => {
    const { store, api, ws } = setup()
    const gh = store.addons.find((a) => a.name === 'github')!
    gh.update!.actions = { ...gh.actions, refresh: { minRole: 'viewer', label: 'Refresh' } }
    store.setViewer('p_tom')
    expect(await fail(api.runAddonAction(ws, 'github', 'refresh', {}))).toMatchObject({ status: 403 })
    store.setViewer('p_sev')
    const u = { version: gh.update!.version, package_sha256: gh.update!.package_sha256, capabilities: gh.update!.capabilities, viewer_actions: ['open', 'refresh'] }
    await api.postAddonOp(ws, 'github', { op: 'update', ...u })
    await api.postAddonOp(ws, 'github', { op: 'grant', ...u })
    store.setViewer('p_tom')
    expect(await ok(api.runAddonAction(ws, 'github', 'refresh', {}))).toBe('ok')
  })
  it('a grant request without viewer_actions at all is refused (409)', async () => {
    const { store, api, ws } = setup()
    store.appendWs(ws, { type: 'addon.updated', name: 'wiki', version: '0.1.4', package_sha256: 'c'.repeat(64), capabilities: [] })
    const g = { op: 'grant', version: '0.1.4', package_sha256: 'c'.repeat(64), capabilities: [] }
    expect(await fail(api.postAddonOp(ws, 'wiki', g as never))).toMatchObject({ status: 409, code: 'addon.changed' })
  })
  it('records the signed viewer_actions in addon.granted and addon.updated', async () => {
    const { store, api, ws } = setup()
    const gh = store.addons.find((a) => a.name === 'github')!
    const u = { version: gh.update!.version, package_sha256: gh.update!.package_sha256, capabilities: gh.update!.capabilities, viewer_actions: ['open'] }
    await api.postAddonOp(ws, 'github', { op: 'update', ...u })
    await api.postAddonOp(ws, 'github', { op: 'grant', ...u })
    const evs = store.wsEventsOf(ws).filter((e) => e.type === 'addon.updated' || e.type === 'addon.granted').slice(-2)
    expect(evs.map((e) => e.type)).toEqual(['addon.updated', 'addon.granted'])
    for (const e of evs) expect(e.viewer_actions).toEqual(['open'])
    const w = store.addons.find((a) => a.name === 'wiki')!
    store.appendWs(ws, { type: 'addon.updated', name: 'wiki', version: '0.1.4', package_sha256: 'c'.repeat(64), capabilities: [] })
    await api.postAddonOp(ws, 'wiki', { op: 'grant', version: '0.1.4', package_sha256: 'c'.repeat(64), capabilities: [], viewer_actions: viewerActions(w).map((a) => a.id) })
    expect(store.wsEventsOf(ws).filter((e) => e.type === 'addon.granted').at(-1)!.viewer_actions).toEqual(['open', 'search'])
  })
})

describe('workspace reads need membership', () => {
  it.each([
    ['addon state', (api: ReturnType<typeof setup>['api'], ws: string) => api.getAddonState(ws, 'estimate')],
    ['today', (api: ReturnType<typeof setup>['api'], ws: string) => api.getToday(ws)],
    ['tickets', (api: ReturnType<typeof setup>['api'], ws: string) => api.listTickets(ws)],
    ['addons', (api: ReturnType<typeof setup>['api'], ws: string) => api.getWorkspaceAddons(ws)],
  ])('a non-member gets 403 for %s', async (_n, call) => {
    const { api, ws } = setup('p_stranger')
    expect(await fail(call(api, ws))).toMatchObject({ status: 403 })
  })
})

describe('ticket and view routes need membership', () => {
  it('a non-member gets 403 for a ticket and its events; members are unchanged', async () => {
    const { api, store } = setup('p_stranger')
    expect(await fail(api.getTicket('DEMO-0043'))).toMatchObject({ status: 403 })
    expect(await fail(api.getEvents('DEMO-0043'))).toMatchObject({ status: 403 })
    expect(await fail(api.getTicket('DEMO-9999'))).toMatchObject({ status: 404 })
    store.setViewer('p_tom')
    expect((await api.getTicket('DEMO-0043')).key).toBe('DEMO-0043')
    expect(Array.isArray(await api.getEvents('DEMO-0043'))).toBe(true)
  })
  it('a non-member cannot delete a saved view (403)', async () => {
    const { api, store, ws } = setup('p_sev')
    const v = await api.saveView(ws, { name: 'mine', shared: false, params: {} })
    store.setViewer('p_stranger')
    expect(await fail(api.deleteView(ws, v.id))).toMatchObject({ status: 403 })
  })
})
