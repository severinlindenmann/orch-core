import { describe, expect, it } from 'vitest'
import { offered } from '@/test/offered'
import { addonActive } from '@/api/addons'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import type { ActionMeta, AddonDecision } from '@/api/types'
import { createMockStore } from '@/mocks/store'
import addons from '@/mocks/fixtures/addons.json'
import catalog from '@/mocks/fixtures/catalog.json'
import { installAndGrant } from '@/test/installAddon'
import { refused } from '@/test/refused'

// One decision rule, in core, for every addon that asks a human (manifest `actions[id].decision: true`).
const DECIDING = ['publish', 'quick', 'models', 'factory', 'schedules'] as const

const setup = (viewer = 'p_sev') => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  for (const name of DECIDING) if (!addonActive(store.workspaces.find((w) => w.id === ws), name)) installAndGrant(store, ws, name)
  store.appendWs(ws, { type: 'member.added', person: 'p_mem', name: 'Mem', role: 'member' })
  store.setViewer(viewer)
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
type S = ReturnType<typeof setup>
const decisionOf = async (s: S, addon: string) => (await s.api.getAddonDecisions(s.ws)).find((d) => d.addon === addon)!
/** As core's Today prompt posts it: after signing, with core's `confirmed` flag. */
const decide = (s: S, d: AddonDecision, option: string, confirmed = true) => s.api.runAddonAction(s.ws, d.addon, d.action, offered(s.store, s.ws, d.addon, d.action, { id: d.id, option, ...(d.ticket ? { ticket: d.ticket } : {}), ...(confirmed ? { confirmed: true } : {}) }))
const decided = (s: S) => s.store.wsEventsOf(s.ws).filter((e) => e.type === 'addon.decided')
const manifest = (name: string) => ([...addons, ...catalog].find((a) => a.name === name) as unknown as { actions?: Record<string, ActionMeta> }).actions ?? {}

describe('addon decisions: one rule in core', () => {
  it('every action an open decision posts to is declared a decision action', async () => {
    const s = setup()
    const open = await s.api.getAddonDecisions(s.ws)
    expect(new Set(open.map((d) => d.addon))).toEqual(new Set(DECIDING))
    for (const d of open) expect(manifest(d.addon)[d.action]?.decision, `${d.addon}.${d.action}`).toBe(true)
  })

  describe.each(DECIDING)('%s', (addon) => {
    it('a plain member cannot decide (403), even with the id', async () => {
      const d = await decisionOf(setup(), addon)
      const m = setup('p_mem')
      expect(await refused(decide(m, d, d.options[0].key))).toMatchObject({ status: 403, code: 'forbidden' })
      expect(decided(m)).toHaveLength(0)
    })
    it('an option the decision does not offer is 400 validation.option and changes nothing', async () => {
      const s = setup()
      const d = await decisionOf(s, addon)
      const before = JSON.stringify(s.store.addonState(s.ws, addon))
      expect(await refused(decide(s, d, 'made-up'))).toMatchObject({ status: 400, code: 'validation.option' })
      expect(JSON.stringify(s.store.addonState(s.ws, addon))).toBe(before)
      expect((await s.api.getAddonDecisions(s.ws)).some((x) => x.id === d.id)).toBe(true)
      expect(decided(s)).toHaveLength(0)
    })
    it('deciding records core\'s addon.decided once; the same decision again is 409 decision.closed', async () => {
      const s = setup('p_mara')
      const d = await decisionOf(s, addon)
      const option = d.options[d.options.length - 1].key
      await decide(s, d, option)
      expect(decided(s)).toEqual([expect.objectContaining({ name: addon, id: d.id, option, presence: 'touchid', actor: { kind: 'person', id: 'p_mara' } })])
      expect(await refused(decide(s, d, option))).toMatchObject({ status: 409, code: 'decision.closed' })
      expect(decided(s)).toHaveLength(1)
    })
  })

  it('a decision posted without core\'s signing prompt (an addon node\'s own args) is 409 confirm.required: no event, no change, no refresh', async () => {
    for (const addon of DECIDING) {
      const s = setup()
      const d = await decisionOf(s, addon)
      const before = JSON.stringify(s.store.addonState(s.ws, addon))
      const cursor = s.store.cursor(s.ws)
      expect(await refused(decide(s, d, d.options[0].key, false)), addon).toMatchObject({ status: 409, code: 'confirm.required' })
      expect(decided(s), addon).toHaveLength(0)
      expect(JSON.stringify(s.store.addonState(s.ws, addon)), addon).toBe(before)
      expect(s.store.cursor(s.ws), addon).toBe(cursor)
    }
  })

  it('a manifest that lost the decision flag (e.g. in update.actions) answers 409 decision.closed, never a 500', async () => {
    for (const addon of DECIDING) {
      const s = setup()
      const d = await decisionOf(s, addon)
      const pkg = s.store.addons.find((a) => a.name === addon)!
      pkg.actions = { ...pkg.actions, [d.action]: { minRole: 'maintainer' } }
      expect(await refused(decide(s, d, d.options[0].key)), addon).toMatchObject({ status: 409, code: 'decision.closed' })
    }
  })

  it('a made-up decision id is closed', async () => {
    const s = setup()
    expect(await refused(s.api.runAddonAction(s.ws, 'publish', 'decide', offered(s.store, s.ws, 'publish', 'decide', { id: 'nope', confirmed: true, option: 'yes' })))).toMatchObject({ status: 409, code: 'decision.closed' })
  })

  it('addon.decided shows in Activity for owners and maintainers only, and not when its ticket is hidden', async () => {
    const s = setup()
    installAndGrant(s.store, s.ws, 'activity')
    const d = (await s.api.getAddonDecisions(s.ws)).find((x) => x.addon === 'publish' && x.ticket)!
    await decide(s, d, d.options[0].key)
    const lines = async (viewer: string) => {
      s.store.setViewer(viewer)
      return JSON.stringify(await s.api.getAddonState(s.ws, 'activity'))
    }
    expect(await lines('p_sev')).toContain('on a publish decision')
    expect(await lines('p_mara')).toContain('on a publish decision')
    expect(await lines('p_mem')).not.toContain('on a publish decision')
    ;(s.store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get(d.ticket!)!.visibility = { restricted: ['p_sev'] }
    expect(await lines('p_mara')).not.toContain('on a publish decision')
  })
})
