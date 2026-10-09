import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import addonsFixture from '@/mocks/fixtures/addons.json'
import catalogFixture from '@/mocks/fixtures/catalog.json'
import { CORE_EVENT_NAMESPACES, registerAddon } from './registry'

const setup = () => {
  const store = createMockStore({ persist: false })
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws: store.workspaces[0].id }
}

describe('addon registry', () => {
  it('refuses an addon named like a core event namespace, so the seedLog prefix guard cannot be sidestepped', () => {
    for (const name of ['gate', 'ticket', 'status', 'verdict', 'addon', 'claim', 'question', 'task', 'workspace', 'member'])
      expect(() => registerAddon({ name, seed: () => ({}), actions: {} }), name).toThrow(/core event namespace/)
    for (const p of [...addonsFixture, ...catalogFixture]) expect(CORE_EVENT_NAMESPACES, p.name).not.toContain(p.name)
  })
  it('serves per-workspace addon state', async () => {
    const { api, ws } = setup()
    const s = await api.getAddonState(ws, 'publish')
    expect(Array.isArray(s.apps)).toBe(true)
  })
  it('an action mutates state and the next read sees it', async () => {
    const { api, ws } = setup()
    await api.runAddonAction(ws, 'publish', 'share', { ticket: 'DEMO-0043' })
    const s = await api.getAddonState(ws, 'publish')
    expect((s.shares as unknown[]).length).toBeGreaterThan(0)
  })
  it('is 409 addon.inactive for an addon disabled in the workspace', async () => {
    const { api, store, ws } = setup()
    store.appendWs(ws, { type: 'addon.disabled', name: 'wiki' })
    await expect(api.getAddonState(ws, 'wiki')).rejects.toMatchObject({ status: 409, code: 'addon.inactive' })
  })
  it('keeps the six iteration-1 actions working', async () => {
    const { api, ws } = setup()
    const calls = [
      ['publish', 'decide', { id: 'dec_publish_failed_build', confirmed: true, option: 'no' }],
      ['estimate', 'save_settings', {}],
      ['github', 'refresh', {}],
      ['terminals', 'save_settings', {}],
      ['usage', 'save_settings', {}],
      ['wiki', 'open', { slug: 'glossary' }],
    ] as const
    for (const [a, id, body] of calls) expect((await api.runAddonAction(ws, a, id, { ...body })).ok).toBe(true)
  })
})
