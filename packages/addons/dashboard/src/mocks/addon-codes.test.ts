import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport, type HttpMethod } from '@/api/transport'
import { createMockStore } from './store'
import { createMockHandler } from './router'

// One code per situation, on every route that meets it.
const setup = (viewer = 'p_sev') => {
  const store = createMockStore({ persist: false })
  store.setViewer(viewer)
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })), raw: (m: HttpMethod, path: string) => createMockHandler(store, { latency: false })(m, path) }
}
const fail = (p: Promise<unknown>) => p.then(() => 'ok', (e: { status: number; code: string }) => `${e.status} ${e.code}`)

describe('consistent addon codes', () => {
  it('an addon that is not active is 409 addon.inactive on the state route and the action route', async () => {
    const { store, ws, api } = setup()
    store.appendWs(ws, { type: 'addon.disabled', name: 'wiki' })
    expect(await fail(api.getAddonState(ws, 'wiki'))).toBe('409 addon.inactive')
    expect(await fail(api.runAddonAction(ws, 'wiki', 'open', { slug: 'glossary' }))).toBe('409 addon.inactive')
    expect(await fail(api.getAddonState(ws, 'no-such-addon'))).toBe('404 not_found')
  })
  it('a hidden ticket is 404 not_visible on ticket routes, addon actions and per-ticket state alike', async () => {
    const { store, ws, api } = setup('p_mara')
    ;(store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get('DEMO-0044')!.visibility = { restricted: ['p_sev'] }
    expect(await fail(api.getTicket('DEMO-0044'))).toBe('404 not_visible')
    expect(await fail(api.runAddonAction(ws, 'publish', 'share', { ticket: 'DEMO-0044' }))).toBe('404 not_visible')
    expect(await fail(api.getAddonState(ws, 'start-agent', 'DEMO-0044'))).toBe('404 not_visible')
    expect(await fail(api.runAddonAction(ws, 'publish', 'share', { ticket: 'DEMO-9999' }))).toBe('404 not_found')
  })
  it('decisions and the catalog live outside the /addons/:name namespace', async () => {
    const { ws, api, raw } = setup()
    expect((await api.getAddonDecisions(ws)).length).toBeGreaterThan(0)
    expect(Array.isArray(await api.getAddonCatalog(ws))).toBe(true)
    expect((await raw('GET', `/api/workspaces/${ws}/addon-decisions`)).status).toBe(200)
    expect((await raw('GET', `/api/workspaces/${ws}/addon-catalog`)).status).toBe(200)
    // An addon may now be called "decisions" or "catalog": these paths are addon state routes only.
    expect((await raw('GET', `/api/workspaces/${ws}/addons/decisions`)).status).toBe(404)
    expect((await raw('GET', `/api/workspaces/${ws}/addons/catalog`)).status).toBe(404)
  })
})
