import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { ApiError } from '@/api/types'
import { canUsePty } from '@/addon-ui/capabilities'
import { pendingUpdate, viewerActions } from '@/api/addons'
import { createMockStore } from './store'
import addonsFixture from './fixtures/addons.json'
import catalogFixture from './fixtures/catalog.json'

function setup(viewer = 'p_sev') {
  const store = createMockStore({ persist: false })
  store.setViewer(viewer)
  const api = createApi(createMockTransport(store, { latency: false }))
  return { store, api, ws: store.workspaces[0].id }
}
const grantReq = (store: ReturnType<typeof createMockStore>, ws: string, name: string) => {
  const a = store.workspaceAddons(ws).find((x) => x.name === name)!
  return { op: 'grant' as const, version: a.ws.version, package_sha256: a.ws.package_sha256, capabilities: a.ws.capabilities, viewer_actions: viewerActions(store.addons.find((x) => x.name === name)!).map((x) => x.id) }
}
const updateReq = (store: ReturnType<typeof createMockStore>) => {
  const u = store.addons.find((a) => a.name === 'github')!.update!
  return { op: 'update' as const, version: u.version, package_sha256: u.package_sha256, capabilities: u.capabilities, viewer_actions: ['open'] } // github's viewer-level open
}
const code = async (p: Promise<unknown>) => {
  try {
    await p
    return 'ok'
  } catch (e) {
    return e instanceof ApiError ? `${e.status} ${e.code}` : 'err'
  }
}

const PER_WORKSPACE = ['enabled', 'installed', 'granted', 'status']

describe('addon packages vs per-workspace state', () => {
  it('GET /api/addons lists every known package (seeded and catalog) with no per-workspace fields', async () => {
    const { api } = setup()
    const pkgs = await api.getAddons()
    expect(pkgs.map((p) => p.name)).toEqual(expect.arrayContaining(['publish', 'github', 'estimate', 'quick', 'models']))
    for (const p of pkgs) for (const k of PER_WORKSPACE) expect(p, `${p.name}.${k}`).not.toHaveProperty(k)
  })
  it('the fixtures carry no per-workspace fields', () => {
    for (const p of [...addonsFixture, ...catalogFixture] as Record<string, unknown>[]) for (const k of PER_WORKSPACE) expect(p, `${String(p.name)}.${k}`).not.toHaveProperty(k)
  })
  it('GET /api/workspaces/:ws/addons returns installed packages with this workspace state under ws', async () => {
    const { store, api, ws } = setup()
    await api.postAddonOp(ws, 'github', updateReq(store))
    const gh = (await api.getWorkspaceAddons(ws)).find((a) => a.name === 'github')!
    expect(gh.ws).toMatchObject({ version: '0.6.0', status: 'needs_grant', enabled: true })
    expect(pendingUpdate(gh)).toBeNull()
    const other = (await api.getWorkspaceAddons(store.workspaces[1].id)).find((a) => a.name === 'github')!
    expect(other.ws.version).toBe('0.5.2')
    expect(pendingUpdate(other)?.version).toBe('0.6.0')
  })
  it('a seeded addon, once uninstalled, is in the catalog and can be installed again (needs a new grant)', async () => {
    const { api, ws } = setup()
    expect((await api.getAddonCatalog(ws)).map((a) => a.name)).not.toContain('estimate')
    await api.postAddonOp(ws, 'estimate', { op: 'uninstall' })
    expect((await api.getAddonCatalog(ws)).map((a) => a.name)).toContain('estimate')
    await api.postAddonOp(ws, 'estimate', { op: 'install' })
    const est = (await api.getWorkspaceAddons(ws)).find((a) => a.name === 'estimate')!
    expect(est.ws).toMatchObject({ version: '0.2.0', status: 'needs_grant', enabled: false, granted: null })
  })
})

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
    expect(await code(api.postAddonOp(ws, 'quick', { op: 'grant', version: '9.9.9', package_sha256: 'x', capabilities: [], viewer_actions: [] }))).toBe('409 addon.version_mismatch')
  })
  it('the grant event carries the package hash and the capabilities, signed with touchid', async () => {
    const { store, api, ws } = setup()
    await api.postAddonOp(ws, 'models', { op: 'install' })
    await api.postAddonOp(ws, 'models', grantReq(store, ws, 'models'))
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
    expect(store.addonOp(ws, 'wiki', { op: 'grant', version: '0.1.4', package_sha256: 'x', capabilities: [], viewer_actions: [] }, agent)).toMatchObject({ ok: false, code: 'human_only' })
  })
  it('update makes the addon needs_grant and inactive until re-granted; uninstall keeps ticket data', async () => {
    const { store, api, ws } = setup()
    await api.postAddonOp(ws, 'github', updateReq(store))
    let gh = (await api.getWorkspaceAddons(ws)).find((a) => a.name === 'github')!
    expect(gh.ws).toMatchObject({ version: '0.6.0', status: 'needs_grant' })
    expect(pendingUpdate(gh)).toBeNull()
    expect(store.addonStateView(ws, 'github')).toBeNull()
    await api.postAddonOp(ws, 'github', { op: 'grant', version: '0.6.0', package_sha256: store.addons.find((a) => a.name === 'github')!.update!.package_sha256, capabilities: ['network', 'spawn_agent'], viewer_actions: ['open'] })
    gh = (await api.getWorkspaceAddons(ws)).find((a) => a.name === 'github')!
    expect(gh.ws.status).toBe('active')
    expect(gh.ws.granted).toMatchObject({ version: '0.6.0', capabilities: ['network', 'spawn_agent'] })
    const before = JSON.stringify(store.listTickets(ws))
    await api.postAddonOp(ws, 'estimate', { op: 'uninstall' })
    expect(JSON.stringify(store.listTickets(ws))).toBe(before)
    expect(store.workspaces[0].addons.estimate).toBeUndefined()
  })
  it('canUsePty needs the capability, a grant for the current version and enabled', () => {
    const { store } = setup()
    const t = store.addons.find((a) => a.name === 'terminals')!
    const w = store.workspaces[0].addons.terminals
    expect(canUsePty(t, w)).toBe(true)
    expect(canUsePty(t, { ...w, granted: null, status: 'needs_grant' })).toBe(false)
    expect(canUsePty(t, { ...w, granted: { ...w.granted!, version: '0.3.0' } })).toBe(false)
    expect(canUsePty(t, { ...w, granted: { ...w.granted!, package_sha256: 'f'.repeat(64) } })).toBe(false)
    expect(canUsePty(t, { ...w, granted: { ...w.granted!, capabilities: [] } })).toBe(false)
    expect(canUsePty(t, { ...w, enabled: false })).toBe(false)
    expect(canUsePty(t, undefined)).toBe(false) // not installed in this workspace
    expect(canUsePty(undefined, w)).toBe(false) // unknown package
  })

  it('refuses a grant whose capabilities or hash differ from the installed package (409 addon.changed)', async () => {
    const { store, api, ws } = setup()
    await api.postAddonOp(ws, 'models', { op: 'install' })
    const g = grantReq(store, ws, 'models')
    expect(await code(api.postAddonOp(ws, 'models', { ...g, capabilities: ['launch', 'pty'] }))).toBe('409 addon.changed')
    expect(await code(api.postAddonOp(ws, 'models', { ...g, package_sha256: 'e'.repeat(64) }))).toBe('409 addon.changed')
    expect(store.workspaceAddons(ws).find((a) => a.name === 'models')!.ws.status).toBe('needs_grant')
  })
  it('refuses an update whose target differs from the offered one (409 addon.changed)', async () => {
    const { store, api, ws } = setup()
    const u = updateReq(store)
    expect(await code(api.postAddonOp(ws, 'github', { ...u, capabilities: ['network'] }))).toBe('409 addon.changed')
    expect(await code(api.postAddonOp(ws, 'github', { ...u, package_sha256: 'd'.repeat(64) }))).toBe('409 addon.changed')
    expect(await code(api.postAddonOp(ws, 'github', { ...u, version: '0.7.0' }))).toBe('409 addon.changed')
  })
  it('same version but a different package hash is needs_grant', () => {
    const { store, ws } = setup()
    store.appendWs(ws, { type: 'addon.updated', name: 'wiki', version: '0.1.4', package_sha256: 'c'.repeat(64), capabilities: [] })
    expect(store.workspaceAddons(ws).find((a) => a.name === 'wiki')!.ws.status).toBe('needs_grant')
  })
  it('runs no action and shows no decision for a needs_grant or disabled addon', async () => {
    const { store, api, ws } = setup()
    expect((await api.getAddonDecisions(ws)).length).toBeGreaterThan(0)
    await api.postAddonOp(ws, 'publish', { op: 'disable' })
    expect((await api.getAddonDecisions(ws)).filter((d) => d.addon === 'publish')).toEqual([])
    expect(await code(api.runAddonAction(ws, 'publish', 'share', { ticket: 'DEMO-0041' }))).toBe('409 addon.inactive')
    await api.postAddonOp(ws, 'github', updateReq(store))
    expect(await code(api.runAddonAction(ws, 'github', 'import', { item: { title: 'x' } }))).toBe('409 addon.inactive')
  })
})
