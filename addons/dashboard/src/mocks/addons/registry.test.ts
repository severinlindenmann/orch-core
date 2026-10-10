import { describe, expect, it } from 'vitest'
import { offered } from '@/test/offered'
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
  it('knows every core namespace of the ticket format (§5): role, policy, edit, projection, restore', () => {
    for (const name of ['role', 'policy', 'edit', 'projection', 'restore']) {
      expect(CORE_EVENT_NAMESPACES, name).toContain(name)
      expect(() => registerAddon({ name, seed: () => ({}), actions: {} }), name).toThrow(/core event namespace/)
    }
  })
  it('the host refuses to install a package named like a core namespace (409 addon.reserved_name), records nothing', async () => {
    const { api, store, ws } = setup()
    const base = store.workspaceCatalog(ws)[0]
    for (const name of ['role', 'policy', 'gate']) {
      store.addons.push({ ...base, name })
      const before = store.wsEventsOf(ws).length
      await expect(api.postAddonOp(ws, name, { op: 'install', version: base.version, package_sha256: base.package_sha256, capabilities: base.capabilities, viewer_actions: [], enable: true })).rejects.toMatchObject({ status: 409, code: 'addon.reserved_name' })
      await expect(api.postAddonOp(ws, name, { op: 'install' })).rejects.toMatchObject({ status: 409, code: 'addon.reserved_name' })
      expect(store.wsEventsOf(ws)).toHaveLength(before)
    }
  })
  it('refuses package names outside [a-z][a-z0-9-]{0,39} at registration and install, and titles core cannot say plainly', async () => {
    for (const name of ['Bad', 'bad_name', '1abc', 'a'.repeat(41), 'ev\u202Eil'])
      expect(() => registerAddon({ name, seed: () => ({}), actions: {} }), name).toThrow(/lower case letters, digits and dashes/)
    const { api, store, ws } = setup()
    const base = store.workspaceCatalog(ws)[0]
    const bad: [string, string][] = [['Bad_Name', 'Fine'], ['fine-one', 'orch core (core)'], ['fine-two', 'Pay · now'], ['fine-three', 'Note: trust me'], ['fine-four', 'x'.repeat(41)], ['fine-five', 'Hid\u200Bden']]
    for (const [name, title] of bad) {
      store.addons.push({ ...base, name, title })
      const before = store.wsEventsOf(ws).length
      await expect(api.postAddonOp(ws, name, { op: 'install' }), `${name} / ${title}`).rejects.toMatchObject({ status: 409, code: 'addon.invalid_manifest' })
      expect(store.wsEventsOf(ws)).toHaveLength(before)
    }
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
    const { api, ws, store } = setup()
    const calls = [
      ['publish', 'decide', { id: 'dec_publish_failed_build', confirmed: true, option: 'no' }],
      ['estimate', 'save_settings', {}],
      ['github', 'refresh', {}],
      ['terminals', 'save_settings', {}],
      ['usage', 'save_settings', {}],
      ['wiki', 'open', { slug: 'glossary' }],
    ] as const
    for (const [a, id, body] of calls) expect((await api.runAddonAction(ws, a, id, offered(store, ws, a, id, { ...body }))).ok).toBe(true)
  })
})
