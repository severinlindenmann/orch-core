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
    expect(await fail(api.postAddonOp(ws, 'github', { ...u, viewer_actions: [] }))).toMatchObject({ status: 409, code: 'addon.changed' })
    expect(await api.postAddonOp(ws, 'github', { ...u, viewer_actions: ['refresh'] })).toMatchObject({ name: 'github' })
  })
})
