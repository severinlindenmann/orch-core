import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { ApiError } from '@/api/types'
import { canUsePty } from '@/addon-ui/capabilities'
import { createMockStore } from './store'

function setup(viewer = 'p_sev') {
  const store = createMockStore({ persist: false })
  store.setViewer(viewer)
  const api = createApi(createMockTransport(store, { latency: false }))
  return { store, api, ws: store.workspaces[0].id }
}
const code = async (p: Promise<unknown>) => {
  try {
    await p
    return 'ok'
  } catch (e) {
    return e instanceof ApiError ? `${e.status} ${e.code}` : 'err'
  }
}

describe('addon manager API', () => {
  it('lists the catalog of not-yet-installed addons', async () => {
    const { api, ws } = setup()
    const titles = (await api.getAddonCatalog(ws)).map((a) => a.title)
    expect(titles).toEqual(['Worktrees', 'Quick tasks', 'Records', 'Activity', 'Model routing', 'AI Factory', 'Schedules'])
  })
  it('refuses enable before grant with 409 addon.needs_grant', async () => {
    const { api, ws } = setup()
    await api.postAddonOp(ws, 'quick', { op: 'install' })
    expect(await code(api.postAddonOp(ws, 'quick', { op: 'enable' }))).toBe('409 addon.needs_grant')
  })
  it('refuses a grant for a version other than the installed one', async () => {
    const { api, ws } = setup()
    await api.postAddonOp(ws, 'quick', { op: 'install' })
    expect(await code(api.postAddonOp(ws, 'quick', { op: 'grant', version: '9.9.9' }))).toBe('409 addon.version_mismatch')
  })
  it('the grant event carries the package hash and the capabilities, signed with touchid', async () => {
    const { store, api, ws } = setup()
    await api.postAddonOp(ws, 'models', { op: 'install' })
    await api.postAddonOp(ws, 'models', { op: 'grant', version: store.addons.find((a) => a.name === 'models')!.version })
    const ev = store.wsEventsOf(ws).find((e) => e.type === 'addon.granted')!
    expect(ev).toMatchObject({ name: 'models', capabilities: ['launch'], presence: 'touchid' })
    expect(ev.package_sha256).toMatch(/^[0-9a-f]{64}$/)
  })
  it('owner only', async () => {
    const { api, ws } = setup('p_mara')
    expect(await code(api.postAddonOp(ws, 'quick', { op: 'install' }))).toBe('403 forbidden')
  })
  it('agents never enable or grant: human_only', async () => {
    const { store, ws } = setup()
    const agent = { kind: 'agent', id: 'a_1' } as never
    expect(store.addonOp(ws, 'wiki', { op: 'enable' }, agent)).toMatchObject({ ok: false, code: 'human_only' })
    expect(store.addonOp(ws, 'wiki', { op: 'grant', version: '0.1.4' }, agent)).toMatchObject({ ok: false, code: 'human_only' })
  })
  it('update makes the addon needs_grant and inactive until re-granted; uninstall keeps ticket data', async () => {
    const { store, api, ws } = setup()
    await api.postAddonOp(ws, 'github', { op: 'update' })
    let gh = (await api.getWorkspaceAddons(ws)).find((a) => a.name === 'github')!
    expect(gh).toMatchObject({ version: '0.6.0', status: 'needs_grant', update: null })
    expect(store.addonStateView(ws, 'github')).toBeNull()
    await api.postAddonOp(ws, 'github', { op: 'grant', version: '0.6.0' })
    gh = (await api.getWorkspaceAddons(ws)).find((a) => a.name === 'github')!
    expect(gh.status).toBe('active')
    expect(gh.granted).toMatchObject({ version: '0.6.0', capabilities: ['network', 'spawn_agent'] })
    const before = JSON.stringify(store.listTickets(ws))
    await api.postAddonOp(ws, 'estimate', { op: 'uninstall' })
    expect(JSON.stringify(store.listTickets(ws))).toBe(before)
    expect(store.workspaces[0].addons.estimate).toBeUndefined()
  })
  it('canUsePty needs the capability, a grant for the current version and enabled', () => {
    const { store } = setup()
    const t = store.addons.find((a) => a.name === 'terminals')!
    expect(canUsePty(t)).toBe(true)
    expect(canUsePty({ ...t, granted: null })).toBe(false)
    expect(canUsePty({ ...t, granted: { ...t.granted!, version: '0.3.0' } })).toBe(false)
    expect(canUsePty({ ...t, granted: { ...t.granted!, capabilities: [] } })).toBe(false)
    expect(canUsePty({ ...t, enabled: false })).toBe(false)
  })
})
