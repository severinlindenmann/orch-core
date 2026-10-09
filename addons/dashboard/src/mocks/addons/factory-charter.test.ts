import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import type { NewTicketRequest } from '@/api/types'
import { describeEvent } from '@/mocks/derive'
import { createMockStore, type MockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'

// Auto-approval under the factory charter goes through one core function (store.autoApprove); the addon writes its
// own events as itself (`factory.*`, actor {kind:'addon', id:'factory'}), never core events as core.
const EPIC = 'DEMO-0050'
const AGENT = 'claude-code:s_demo:p_sev'
const setup = () => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  installAndGrant(store, ws, 'factory')
  store.setViewer('p_sev')
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
const child = (store: MockStore, ws: string, parent: string | null = EPIC) => {
  const req: NewTicketRequest = { type: 'feature', title: 'A charter child', priority: 'medium', size: 's', labels: [], parent, due: null, visibility: 'workspace', people: { owner: null, assignees: [], reviewers: [] }, sections: { summary: 'x', requirements: '- x' }, acceptance: ['x'] }
  const r = store.createFromRequest(ws, req)
  if (!r.ok) throw new Error(r.message)
  return r.ticket.key
}
const approvals = (store: MockStore, key: string) => store.eventsOf(key).filter((e) => e.type === 'gate.approved')

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('store.autoApprove (core)', () => {
  it('approves a gate of an epic child for an agent under the active charter, recorded as via factory_charter', () => {
    const { store, ws } = setup()
    const key = child(store, ws)
    expect(store.autoApprove(key, 'requirements', { charter: 'factory', by: AGENT })).toMatchObject({ ok: true })
    expect(store.autoApprove(key, 'plan', { charter: 'factory', by: AGENT })).toMatchObject({ ok: true })
    const [req, plan] = approvals(store, key)
    expect(req).toMatchObject({ gate: 'requirements', via: 'factory_charter', charter: EPIC, actor: { kind: 'agent', for: 'p_sev' } })
    expect(plan).toMatchObject({ gate: 'plan', via: 'factory_charter' })
    expect(store.ticket(key)!.status).toBe('open') // core moves a backlog ticket on once its plan is approved
    expect(store.ticket(key)!.gates.requirements.approvals[0]).toMatchObject({ via: 'factory_charter' })
  })
  it('refuses while the factory is paused or stopped, for tickets outside the epic, and for a person', async () => {
    const { store, ws, api } = setup()
    const key = child(store, ws)
    const other = child(store, ws, null)
    expect(store.autoApprove(other, 'requirements', { charter: 'factory', by: AGENT })).toMatchObject({ ok: false, status: 409, code: 'charter.out_of_scope' })
    expect(store.autoApprove(key, 'requirements', { charter: 'factory', by: 'p_sev' })).toMatchObject({ ok: false, status: 403, code: 'charter.agent_only' })
    await api.runAddonAction(ws, 'factory', 'pause', { confirmed: true })
    expect(store.autoApprove(key, 'requirements', { charter: 'factory', by: AGENT })).toMatchObject({ ok: false, status: 409, code: 'charter.inactive' })
    await api.runAddonAction(ws, 'factory', 'resume', { confirmed: true })
    store.addonState(ws, 'factory').used = 25 // the child budget is used up: stopped
    expect(store.autoApprove(key, 'requirements', { charter: 'factory', by: AGENT })).toMatchObject({ ok: false, status: 409, code: 'charter.inactive' })
    expect(approvals(store, key)).toHaveLength(0)
  })
  it('refuses when the addon that holds the charter is off', () => {
    const { store, ws } = setup()
    const key = child(store, ws)
    store.addonOp(ws, 'factory', { op: 'disable' }, { kind: 'person', id: 'p_sev' })
    expect(store.autoApprove(key, 'requirements', { charter: 'factory', by: AGENT })).toMatchObject({ ok: false, status: 409, code: 'charter.inactive' })
  })
})

describe('the factory writes its own events as itself', () => {
  it('Watch live: children are auto-approved through core; permits are factory.* events by the addon', async () => {
    const { store, ws, api } = setup()
    const before = new Set(store.ticketKeys(ws))
    await api.runAddonAction(ws, 'factory', 'watch')
    vi.advanceTimersByTime(20_000 * 3)
    const made = store.ticketKeys(ws).filter((k) => !before.has(k))
    expect(made.length).toBe(3)
    for (const k of made) {
      expect(approvals(store, k).map((e) => e.via)).toEqual(['factory_charter', 'factory_charter'])
      expect(store.eventsOf(k).some((e) => e.actor.kind === 'host' && e.type !== 'status.changed')).toBe(false)
    }
  })
  it('answering a permit logs factory.permit_granted / factory.permit_refused by the addon; no permit.* or host-acted addon event', async () => {
    const { store, ws, api } = setup()
    const [d] = (await api.getAddonDecisions(ws)).filter((x) => x.addon === 'factory')
    await api.runAddonAction(ws, 'factory', 'permit', { id: d.id, confirmed: true, option: 'epic', ticket: d.ticket })
    const ev = store.eventsOf(EPIC).filter((e) => e.type.startsWith('factory.permit_'))
    expect(ev).toEqual([expect.objectContaining({ type: 'factory.permit_granted', scope: 'epic', actor: { kind: 'addon', id: 'factory' } })])
    expect(store.eventsOf(EPIC).some((e) => e.type.startsWith('permit.'))).toBe(false)
    expect(describeEvent(ev[0])).toMatch(/^granted P-\d+ for this epic$/)
    expect(describeEvent({ type: 'factory.permit_refused', permit: 'P-9' })).toBe('refused P-9')
  })
  it('pause and resume are factory.paused / factory.resumed by the addon (the signer is in core\'s addon.action_signed)', async () => {
    const { store, ws, api } = setup()
    await api.runAddonAction(ws, 'factory', 'pause', { confirmed: true })
    expect(store.eventsOf(EPIC).find((e) => e.type === 'factory.paused')).toMatchObject({ actor: { kind: 'addon', id: 'factory' } })
    expect(store.wsEventsOf(ws).find((e) => e.type === 'addon.action_signed')).toMatchObject({ actor: { kind: 'person', id: 'p_sev' }, action: 'pause' })
  })
})

describe('permit decisions name only what the person can see', () => {
  it('the detail leaves out the epic key when the epic is hidden from the person', async () => {
    const { store, ws, api } = setup()
    ;(store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get(EPIC)!.visibility = { restricted: ['p_sev'] }
    store.setViewer('p_mara')
    const permits = (await api.getAddonDecisions(ws)).filter((d) => d.addon === 'factory')
    expect(permits.length).toBeGreaterThan(0)
    for (const d of permits) expect(JSON.stringify(d)).not.toContain(EPIC)
    store.setViewer('p_sev')
    expect((await api.getAddonDecisions(ws)).find((d) => d.addon === 'factory')!.detail).toContain(`Epic ${EPIC}.`)
  })
})

describe('no double approval, no budget for a refused step', () => {
  it('a gate already approved (or already approved under the charter) is refused, with no second gate.approved', () => {
    const { store, ws } = setup()
    const key = child(store, ws)
    expect(store.autoApprove(key, 'requirements', { charter: 'factory', by: AGENT }).ok).toBe(true)
    expect(store.autoApprove(key, 'requirements', { charter: 'factory', by: AGENT })).toMatchObject({ ok: false, status: 409, code: 'gate.already_approved' })
    expect(approvals(store, key)).toHaveLength(1)
  })
  it('a simulated step whose approval core refuses does not use up the budget', async () => {
    const { store, ws, api } = setup()
    const used = () => store.addonState(ws, 'factory').used as number
    await api.runAddonAction(ws, 'factory', 'watch')
    const before = used()
    vi.spyOn(store, 'autoApprove').mockReturnValue({ ok: false, status: 409, code: 'charter.inactive', message: 'no' })
    vi.advanceTimersByTime(20_000)
    expect(used()).toBe(before)
    vi.restoreAllMocks()
  })
})
