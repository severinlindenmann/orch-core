import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import type { ActionMeta } from '@/api/types'
import { createMockStore } from '@/mocks/store'
import addons from '@/mocks/fixtures/addons.json'
import catalog from '@/mocks/fixtures/catalog.json'
import { installAndGrant } from '@/test/installAddon'

const setup = (viewer = 'p_sev') => {
  const store = createMockStore({ persist: false })
  store.setViewer(viewer)
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  for (const name of ['activity', 'schedules', 'quick']) installAndGrant(store, ws, name)
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws }
}
const manifest = (name: string) => ([...addons, ...catalog].find((a) => a.name === name) as unknown as { actions: Record<string, ActionMeta> }).actions

/** Per-viewer navigation actions: they only move what this viewer is looking at. */
const NAVIGATION: [addon: string, action: string, body: Record<string, unknown>][] = [
  ['activity', 'apply', { formData: { period: 'week', type: 'gates', person: 'p:p_mara', q: 'tariff' } }],
  ['activity', 'view_ticket', {}],
  ['activity', 'view_timeline', {}],
  ['activity', 'show_new', {}],
  ['activity', 'clear_filters', {}],
  ['activity', 'show_older', {}],
  ['wiki', 'open', { slug: 'glossary' }],
  ['wiki', 'search', { formData: { q: 'dbt' } }],
  ['guide', 'open', { slug: 'getting-around' }],
  ['schedules', 'open_run', { run: 'R-4' }],
  ['terminals', 'open', {}],
  ['github', 'open', { id: 'acme-energy/energy-dbt#29' }],
]

describe('navigation actions (manifest kind "navigation")', () => {
  it.each(NAVIGATION)('%s.%s is declared navigation', (addon, action) => {
    expect(manifest(addon)[action]?.kind).toBe('navigation')
  })
  it.each(NAVIGATION)('%s.%s is honoured and does not move the workspace cursor, so other clients do not refetch', async (addon, action, body) => {
    const { store, api, ws } = setup()
    const before = store.cursor(ws)
    expect((await api.runAddonAction(ws, addon, action, body)).ok).toBe(true)
    expect(store.cursor(ws)).toBe(before)
  })
  it('a shared-state action still moves the cursor', async () => {
    const { store, api, ws } = setup()
    const before = store.cursor(ws)
    await api.runAddonAction(ws, 'publish', 'share', { ticket: 'DEMO-0041' })
    expect(store.cursor(ws)).toBeGreaterThan(before)
  })
  it('a viewer\'s navigation still works and is theirs alone', async () => {
    const tom = setup('p_tom')
    await tom.api.runAddonAction(tom.ws, 'wiki', 'open', { slug: 'glossary' })
    const mine = (await tom.api.getAddonState(tom.ws, 'wiki')) as { current?: { slug?: string } }
    expect(mine.current?.slug).toBe('glossary')
    tom.store.setViewer('p_sev')
    const theirs = (await tom.api.getAddonState(tom.ws, 'wiki')) as { current?: { slug?: string } }
    expect(theirs.current?.slug).not.toBe('glossary')
  })
})

describe('refused actions', () => {
  it('a refused action (StoreFailure) neither moves the cursor nor saves', async () => {
    const { store, api, ws } = setup()
    const before = store.cursor(ws)
    let saves = 0
    const s = store as unknown as { save: () => void }
    const real = s.save.bind(store)
    s.save = () => {
      saves++
      real()
    }
    await api.runAddonAction(ws, 'publish', 'start', { id: 'nope' }).catch(() => undefined)
    await api.runAddonAction(ws, 'worktrees', 'remove', { id: 'nope' }).catch(() => undefined)
    expect(store.cursor(ws)).toBe(before)
    expect(saves).toBe(0)
  })
})
