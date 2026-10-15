// R2 review: what core accepts from an addon module's seed log and from an action's answer.
import { afterEach, describe, expect, it } from 'vitest'
import { createMockStore } from './store'
import { getAddon, registerAddon, type MockAddon } from './addons/registry'
import { installAndGrant } from '@/test/installAddon'
import './addons'

const land = getAddon('land')!
const quick = getAddon('quick')!
afterEach(() => {
  registerAddon(land)
  registerAddon(quick)
})

describe('seedLog: an addon seeds only its own records, as itself', () => {
  it('drops core events and another addon\'s records, and writes the addon as the actor', () => {
    // The land addon (active in DEMO) tries to seed a gate approval, a quick.* record and a land record under a person's name.
    registerAddon({
      ...land,
      seedLog: () => [
        { ticket: 'DEMO-0043', event: { type: 'gate.approved', actor: 'p_sev', at: '2026-10-09T11:00:00Z', gate: 'verify' } },
        { ticket: 'DEMO-0043', event: { type: 'verdict.given', actor: 'p_sev', at: '2026-10-09T11:00:00Z', result: 'pass' } },
        { ticket: 'DEMO-0043', event: { type: 'quick.made_ticket', actor: 'addon:quick', at: '2026-10-09T11:00:00Z' } },
        { ticket: 'DEMO-0043', event: { type: 'land.queued', actor: 'p_mara', at: '2026-10-09T11:00:00Z', target: 'develop' } },
      ],
    } as MockAddon)
    const store = createMockStore({ persist: false })
    const evs = store.eventsOf('DEMO-0043')
    expect(evs.some((e) => e.type === 'gate.approved' && e.at === '2026-10-09T11:00:00Z')).toBe(false)
    expect(evs.some((e) => e.type === 'verdict.given')).toBe(false)
    expect(evs.some((e) => e.type === 'quick.made_ticket')).toBe(false)
    const queued = evs.find((e) => e.type === 'land.queued')!
    expect(queued.actor).toEqual({ kind: 'addon', id: 'land' })
  })
  it('a land.attempt seeded by another addon is ignored, so it never feeds landingResolved', () => {
    registerAddon({ ...quick, seedLog: () => [{ ticket: 'DEMO-0042', event: { type: 'land.attempt', actor: 'addon:land', at: '2026-10-09T11:00:00Z', attempt: 99, outcome: 'failed', reason: 'conflict' } }] } as MockAddon)
    const store = createMockStore({ persist: false })
    installAndGrant(store, store.workspaces.find((w) => w.prefix === 'DEMO')!.id, 'quick')
    expect(store.eventsOf('DEMO-0042').some((e) => e.type === 'land.attempt' && e.attempt === 99)).toBe(false)
    expect(store.ticket('DEMO-0042')!.landing).toBeUndefined()
  })
})

describe('action answers: core keeps only what the viewer may be pointed at', () => {
  const setup = (answer: Record<string, unknown>) => {
    registerAddon({ ...quick, actions: { ...quick.actions, add: () => ({ ok: true, message: 'Done.', ...answer }) } } as MockAddon)
    const store = createMockStore({ persist: false })
    const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
    installAndGrant(store, ws, 'quick')
    return { store, ws }
  }
  it('keeps a visible ticket of this workspace; drops another workspace\'s, a hidden one and an unknown one', () => {
    let s = setup({ ticket: 'DEMO-0043' })
    expect(s.store.runAddon(s.ws, 'quick', 'add', {})).toMatchObject({ ok: true, ticket: 'DEMO-0043' })
    const other = s.store.workspaces.find((w) => w.prefix !== 'DEMO')!
    const otherKey = s.store.ticketKeys(other.id)[0]
    s = setup({ ticket: otherKey })
    expect(s.store.runAddon(s.ws, 'quick', 'add', {})).not.toHaveProperty('ticket')
    s = setup({ ticket: 'DEMO-9999' })
    expect(s.store.runAddon(s.ws, 'quick', 'add', {})).not.toHaveProperty('ticket')
    s = setup({ ticket: 'DEMO-0043' })
    ;(s.store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get('DEMO-0043')!.visibility = { restricted: ['p_mara'] }
    expect(s.store.runAddon(s.ws, 'quick', 'add', {})).not.toHaveProperty('ticket')
  })
  it('keeps a terminal only while terminals is active and the session is in this viewer\'s view', () => {
    let s = setup({ terminal: 'shell1' })
    expect(s.store.runAddon(s.ws, 'quick', 'add', {})).toMatchObject({ terminal: 'shell1' })
    // Mara cannot see Severin's own shell.
    s = setup({ terminal: 'shell1' })
    s.store.setViewer('p_mara')
    expect(s.store.runAddon(s.ws, 'quick', 'add', {})).not.toHaveProperty('terminal')
    s = setup({ terminal: 'nope' })
    expect(s.store.runAddon(s.ws, 'quick', 'add', {})).not.toHaveProperty('terminal')
    s = setup({ terminal: 'shell1' })
    s.store.addonOp(s.ws, 'terminals', { op: 'disable' }, { kind: 'person', id: 'p_sev' })
    expect(s.store.runAddon(s.ws, 'quick', 'add', {})).not.toHaveProperty('terminal')
  })
})
