import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { describeEvent } from '@/mocks/derive'
import { installAndGrant } from '@/test/installAddon'
import { refused } from '@/test/refused'
import { getAddon } from './registry'

// AI Factory (Phase 2 preview): one factory epic with a signed charter, children, permits as core decisions,
// pause/resume signed by core, and a "Watch live" simulator that is capped.
const EPIC = 'DEMO-0050'
const setup = (viewer = 'p_sev', install = true) => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  if (install) installAndGrant(store, ws, 'factory')
  store.setViewer(viewer)
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
type S = ReturnType<typeof setup>
interface Row {
  ticket: string
  title: string
  status: string
  approval: string
  size: string
}
interface State {
  epic: { key: string; title: string } | null
  mode: 'running' | 'paused' | 'stopped'
  stateAlert: { type: string; tone?: string; title?: string; text?: string }
  charter: string
  used: number
  maxChildren: number
  hoursUsed: number
  maxHours: number
  children: Row[]
  permits: { id: string; command: string; ticket: string; state: string }[]
  report: string
  watching: boolean
  controls: { type: string; children?: { label?: string; action?: string }[] }
  [k: string]: unknown
}
const state = async (s: S) => (await s.api.getAddonState(s.ws, 'factory')) as unknown as State
const run = (s: S, id: string, body: Record<string, unknown> = {}) => s.api.runAddonAction(s.ws, 'factory', id, body)
const permitsOf = async (s: S) => (await s.api.getAddonDecisions(s.ws)).filter((d) => d.addon === 'factory')
const fail = (p: Promise<unknown>) => p.then(() => 'ok', (e: { status: number; code: string }) => `${e.status} ${e.code}`)
const labels = async (s: S) => ((await state(s)).controls.children ?? []).map((c) => c.label)

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('factory package and seed', () => {
  it('starts in the catalog as a preview; pause/resume are signed and maintainer-only, watching changes shared state so it needs a member', () => {
    const { store, ws } = setup('p_sev', false)
    expect(store.workspaces.find((w) => w.id === ws)!.addons.factory).toBeUndefined()
    const pkg = store.addons.find((a) => a.name === 'factory')!
    expect(pkg.preview).toBe(true)
    expect(pkg.actions).toMatchObject({
      pause: { minRole: 'maintainer', confirm: 'sign' },
      resume: { minRole: 'maintainer', confirm: 'sign' },
      watch: { minRole: 'member' },
      stop_watching: { minRole: 'member' },
    })
  })
  it('DEMO has the epic "Monthly billing v2" with children of several statuses and sizes, some auto-approved by an agent', async () => {
    const s = setup()
    const epic = s.store.ticket(EPIC)!
    expect(epic).toMatchObject({ type: 'epic', title: 'Monthly billing v2' })
    const st = await state(s)
    expect(st.epic).toEqual({ key: EPIC, title: 'Monthly billing v2' })
    expect(st.children.length).toBeGreaterThanOrEqual(4)
    expect(new Set(st.children.map((c) => c.status)).size).toBeGreaterThanOrEqual(3)
    expect(new Set(st.children.map((c) => c.size)).size).toBeGreaterThanOrEqual(2)
    expect(st.children.some((c) => c.approval === 'auto-approved by agent')).toBe(true)
    expect(st.children.some((c) => c.approval !== 'auto-approved by agent')).toBe(true)
    expect(st.children.every((c) => s.store.ticket(c.ticket)!.parent === EPIC)).toBe(true)
  })
  it('states the charter limits: 25 children or 72 hours, size m or smaller; the budget is partly used', async () => {
    const st = await state(setup())
    expect(st.charter).toContain('25 children or 72 hours')
    expect(st.charter).toContain('size m or smaller')
    expect(st.maxChildren).toBe(25)
    expect(st.maxHours).toBe(72)
    expect(st.used).toBe(st.children.length)
    expect(st.hoursUsed).toBeGreaterThan(0)
    expect(st.hoursUsed).toBeLessThan(72)
    expect(st.mode).toBe('running')
    expect(st.stateAlert).toMatchObject({ type: 'stack' })
  })
})

describe('permits are core decisions', () => {
  it('open requests appear as decisions with the three options; viewers see none', async () => {
    const s = setup()
    const ds = await permitsOf(s)
    expect(ds.length).toBeGreaterThanOrEqual(1)
    expect(ds[0].options.map((o) => o.label)).toEqual(['Grant once', 'Grant for this epic', 'Refuse'])
    expect(ds[0].title).toBe('AI Factory permit')
    const v = setup('p_tom')
    expect(await permitsOf(v)).toEqual([])
  })
  it('grant once: the decision disappears and factory.permit_granted lands on the epic', async () => {
    const s = setup()
    const [d] = await permitsOf(s)
    const before = (await permitsOf(s)).length
    await run(s, 'permit', { id: d.id, confirmed: true, option: 'once', ticket: d.ticket })
    expect((await permitsOf(s)).length).toBe(before - 1)
    const ev = s.store.eventsOf(EPIC).filter((e) => e.type === 'factory.permit_granted')
    expect(ev).toHaveLength(1)
    expect(ev[0]).toMatchObject({ scope: 'once', permit: d.id.split(':')[1] })
    expect(ev[0].actor).toEqual({ kind: 'addon', id: 'factory' }) // the person's answer is core's addon.decided
    expect(s.store.wsEventsOf(s.ws).find((e) => e.type === 'addon.decided')).toMatchObject({ id: d.id, option: 'once', actor: { kind: 'person', id: 'p_sev' } })
    expect(describeEvent(ev[0])).toMatch(/granted P-\d+ once/)
    expect((await state(s)).permits.find((p) => p.id === ev[0].permit)!.state).toBe('granted once')
  })
  it('refuse logs factory.permit_refused on the epic', async () => {
    const s = setup()
    const [d] = await permitsOf(s)
    await run(s, 'permit', { id: d.id, confirmed: true, option: 'refuse', ticket: d.ticket })
    expect(s.store.eventsOf(EPIC).filter((e) => e.type === 'factory.permit_refused')).toHaveLength(1)
    expect(describeEvent(s.store.eventsOf(EPIC).at(-1)!)).toMatch(/refused P-\d+/)
  })
  it('grant for this epic is a standing grant: the same command later needs no decision', async () => {
    const s = setup()
    const [d] = await permitsOf(s)
    const command = (await state(s)).permits.find((p) => d.id.endsWith(p.id))!.command
    await run(s, 'permit', { id: d.id, confirmed: true, option: 'epic', ticket: d.ticket })
    expect(s.store.eventsOf(EPIC).find((e) => e.type === 'factory.permit_granted')).toMatchObject({ scope: 'epic' })
    await run(s, 'watch')
    vi.advanceTimersByTime(20_000 * 10)
    const st = await state(s)
    const same = st.permits.filter((p) => p.command === command)
    expect(same.length).toBeGreaterThan(1)
    expect(same.filter((p) => p.state === 'open')).toEqual([])
  })
  it('a closed decision does nothing, and a viewer cannot decide', async () => {
    const s = setup()
    const [d] = await permitsOf(s)
    await run(s, 'permit', { id: d.id, confirmed: true, option: 'once', ticket: d.ticket })
    const again = await refused(run(s, 'permit', { id: d.id, confirmed: true, option: 'refuse', ticket: d.ticket }))
    expect(again).toMatchObject({ status: 409, code: 'decision.closed', message: 'That decision is closed.' })
    expect(s.store.eventsOf(EPIC).filter((e) => e.type === 'factory.permit_refused')).toHaveLength(0)
    const v = setup('p_tom')
    const [vd] = [{ id: 'factory.permit:P-1', ticket: 'DEMO-0052' }]
    expect(await fail(v.api.runAddonAction(v.ws, 'factory', 'permit', { id: vd.id, confirmed: true, option: 'once' }))).toBe('403 forbidden')
  })
})

describe('pause and resume are signed by core', () => {
  it('refused without core\'s confirmation, and only maintainers may', async () => {
    const s = setup()
    expect(await fail(run(s, 'pause'))).toBe('409 confirm.required')
    expect((await state(s)).mode).toBe('running')
    const t = setup('p_tom')
    expect(await fail(t.api.runAddonAction(t.ws, 'factory', 'pause', { confirmed: true }))).toBe('403 forbidden')
  })
  it('pausing shows a calm paused alert (info) and Resume; resuming clears it', async () => {
    const s = setup()
    expect(await labels(s)).toContain('Pause factory')
    await run(s, 'pause', { confirmed: true })
    const st = await state(s)
    expect(st.mode).toBe('paused')
    expect(st.stateAlert).toMatchObject({ type: 'alert', tone: 'info' })
    expect(st.stateAlert.title).toMatch(/paused/i)
    expect(await labels(s)).toContain('Resume')
    expect(await labels(s)).not.toContain('Pause factory')
    expect(s.store.eventsOf(EPIC).some((e) => e.type === 'factory.paused')).toBe(true)
    await run(s, 'resume', { confirmed: true })
    expect((await state(s)).mode).toBe('running')
    expect(s.store.eventsOf(EPIC).some((e) => e.type === 'factory.resumed')).toBe(true)
  })
  it('pausing twice is a no-op', async () => {
    const s = setup()
    await run(s, 'pause', { confirmed: true })
    expect((await run(s, 'pause', { confirmed: true })).message).toMatch(/already paused/i)
    expect(s.store.eventsOf(EPIC).filter((e) => e.type === 'factory.paused')).toHaveLength(1)
  })
})

describe('Stopped and Ready', () => {
  it('a used-up time budget shows the Stopped alert (warn), and Pause is gone', async () => {
    const s = setup()
    ;(s.store.addonState(s.ws, 'factory') as { startedAt: string }).startedAt = '2026-10-01T00:00:00Z'
    const st = await state(s)
    expect(st.mode).toBe('stopped')
    expect(st.stateAlert).toMatchObject({ type: 'alert', tone: 'warn' })
    expect(st.stateAlert.title).toMatch(/stopped/i)
    expect(st.stateAlert.text).toMatch(/time budget/)
    expect(await labels(s)).not.toContain('Pause factory')
  })
  it('the Ready report appears once every child is in testing or done', async () => {
    const s = setup()
    expect((await state(s)).report).toBe('')
    for (const c of (await state(s)).children) {
      const status = s.store.ticket(c.ticket)!.status
      if (status !== 'testing' && status !== 'done') s.store.append(c.ticket, { type: 'status.changed', actor: 'p_mara', to: 'testing' })
    }
    const st = await state(s)
    expect(st.report).toContain('Ready')
    for (const c of st.children) expect(st.report).toContain(c.ticket)
    expect(st.report).toMatch(/auto-approved by agent/)
  })
})

describe('Watch live simulator', () => {
  it('adds a child and a permit request about every 20 s, only while watching', async () => {
    const s = setup()
    const before = await state(s)
    vi.advanceTimersByTime(60_000)
    expect((await state(s)).children).toHaveLength(before.children.length)
    await run(s, 'watch')
    expect((await state(s)).watching).toBe(true)
    vi.advanceTimersByTime(19_000)
    expect((await state(s)).children).toHaveLength(before.children.length)
    vi.advanceTimersByTime(1_000)
    const one = await state(s)
    expect(one.children).toHaveLength(before.children.length + 1)
    expect(one.permits.length).toBe(before.permits.length + 1)
    expect(one.used).toBe(before.used + 1)
    const added = one.children.find((c) => !before.children.some((b) => b.ticket === c.ticket))!
    expect(s.store.ticket(added.ticket)!.parent).toBe(EPIC)
    expect(added.approval).toBe('auto-approved by agent')
  })
  it('stops on "Stop watching"', async () => {
    const s = setup()
    await run(s, 'watch')
    vi.advanceTimersByTime(20_000)
    const n = (await state(s)).children.length
    await run(s, 'stop_watching')
    expect((await state(s)).watching).toBe(false)
    vi.advanceTimersByTime(200_000)
    expect((await state(s)).children).toHaveLength(n)
  })
  it('is capped at 10 steps so the demo never floods, then watching ends by itself', async () => {
    const s = setup()
    const n = (await state(s)).children.length
    await run(s, 'watch')
    vi.advanceTimersByTime(20_000 * 40)
    const st = await state(s)
    expect(st.children).toHaveLength(n + 10)
    expect(st.watching).toBe(false)
    expect(s.store.sim.running()).toEqual([])
  })
  it('watching is per viewer: another person is not shown as watching', async () => {
    const s = setup()
    await run(s, 'watch')
    s.store.setViewer('p_mara')
    expect((await state(s)).watching).toBe(false)
    s.store.setViewer('p_sev')
    expect((await state(s)).watching).toBe(true)
  })
  it('a viewer cannot watch live: it would change shared state', async () => {
    const t = setup('p_tom')
    expect(await fail(run(t, 'watch'))).toBe('403 forbidden')
    expect(await fail(run(t, 'stop_watching'))).toBe('403 forbidden')
    expect(t.store.sim.running()).toEqual([])
  })
  it('children are created through core by the watcher\'s own simulated agent', async () => {
    const s = setup('p_mara')
    await run(s, 'watch')
    vi.advanceTimersByTime(20_000)
    const added = (await state(s)).children.find((c) => c.ticket > 'DEMO-0054')!
    const created = s.store.eventsOf(added.ticket).find((e) => e.type === 'ticket.created')!
    expect(created.actor).toMatchObject({ kind: 'agent', for: 'p_mara' })
    expect(s.store.eventsOf(added.ticket).filter((e) => e.type === 'gate.approved').every((e) => e.actor.kind === 'agent' && (e.actor as { for: string }).for === 'p_mara')).toBe(true)
  })
  it('pressing Watch live again cannot go past the cap within the hour, and the cap lifts later', async () => {
    const s = setup()
    const n = (await state(s)).children.length
    await run(s, 'watch')
    vi.advanceTimersByTime(20_000 * 12)
    expect((await state(s)).children).toHaveLength(n + 10)
    const limit = await refused(run(s, 'watch'))
    expect(limit).toMatchObject({ status: 409, code: 'factory.demo_limit' })
    expect(limit.message).toMatch(/limit reached/i)
    vi.advanceTimersByTime(20_000 * 12)
    expect((await state(s)).children).toHaveLength(n + 10)
    expect((await state(s)).watching).toBe(false)
    vi.advanceTimersByTime(3_600_000)
    await run(s, 'watch')
    vi.advanceTimersByTime(20_000)
    expect((await state(s)).children).toHaveLength(n + 11)
  })
  it('a paused factory adds nothing', async () => {
    const s = setup()
    await run(s, 'pause', { confirmed: true })
    const n = (await state(s)).children.length
    vi.advanceTimersByTime(60_000)
    expect((await state(s)).children).toHaveLength(n)
    expect((await state(s)).watching).toBe(false)
  })
  it('stops writing when the addon is disabled or uninstalled', async () => {
    for (const op of ['disable', 'uninstall'] as const) {
      const s = setup()
      await run(s, 'watch')
      s.store.addonOp(s.ws, 'factory', { op }, { kind: 'person', id: 'p_sev' })
      expect(s.store.sim.running()).toEqual([])
      const n = s.store.ticketKeys(s.ws).length
      vi.advanceTimersByTime(60_000)
      expect(s.store.ticketKeys(s.ws)).toHaveLength(n)
    }
  })
  it('the step itself refuses while the addon is inactive', async () => {
    const s = setup()
    await run(s, 'watch')
    // A script that survived (e.g. restored state) still writes nothing once the addon is off.
    const w = s.store.workspaces.find((x) => x.id === s.ws)!
    w.addons.factory.enabled = false
    const n = s.store.ticketKeys(s.ws).length
    vi.advanceTimersByTime(60_000)
    expect(s.store.ticketKeys(s.ws)).toHaveLength(n)
  })
})

describe('minors', () => {
  it('an unknown permit option is a 400 and changes nothing', async () => {
    const s = setup()
    const [d] = await permitsOf(s)
    expect(await fail(run(s, 'permit', { id: d.id, confirmed: true, option: 'whatever', ticket: d.ticket }))).toBe('400 validation.option')
    expect((await permitsOf(s)).length).toBeGreaterThan(0)
    expect(s.store.eventsOf(EPIC).some((e) => e.type === 'factory.permit_granted')).toBe(false)
  })
  it('a standing grant answering a later request is logged on the epic as standing', async () => {
    const s = setup()
    const [d] = await permitsOf(s)
    await run(s, 'permit', { id: d.id, confirmed: true, option: 'epic', ticket: d.ticket })
    await run(s, 'watch')
    vi.advanceTimersByTime(20_000 * 10)
    const standing = s.store.eventsOf(EPIC).filter((e) => e.type === 'factory.permit_granted' && e.standing === true)
    expect(standing.length).toBeGreaterThan(0)
    expect(standing[0]).toMatchObject({ scope: 'epic' })
    expect(describeEvent(standing[0])).toMatch(/standing grant/)
  })
  it('the view carries no watcher map', async () => {
    const s = setup()
    await run(s, 'watch')
    const json = JSON.stringify(await state(s))
    expect(json).not.toContain('p_sev') // the actual watcher
    expect(json).not.toContain('watching":true,"') // no per-person flag map
    for (const k of ['nav', 'simBy', 'simTimes', 'simSteps']) expect(Object.keys(JSON.parse(json))).not.toContain(k)
    expect(await state(s)).toMatchObject({ watching: true })
  })
})

describe('signed actions leave a core record', () => {
  it('records a success even when the addon does not set `changed`', async () => {
    const s = setup()
    const addon = getAddon('factory')!
    const orig = addon.actions.resume
    addon.actions.resume = () => ({ ok: true, message: 'quiet' })
    try {
      await run(s, 'resume', { confirmed: true })
    } finally {
      addon.actions.resume = orig
    }
    expect(s.store.wsEventsOf(s.ws).find((e) => e.type === 'addon.action_signed')).toMatchObject({ action: 'resume', changed: false })
  })
  it('pause appends addon.action_signed with scalar args only; a no-op or a refusal does not', async () => {
    const s = setup()
    expect(await fail(run(s, 'pause'))).toBe('409 confirm.required')
    expect(s.store.wsEventsOf(s.ws).some((e) => e.type === 'addon.action_signed')).toBe(false)
    await run(s, 'pause', { confirmed: true, id: 'x'.repeat(500), nested: { a: 1 } })
    const ev = s.store.wsEventsOf(s.ws).filter((e) => e.type === 'addon.action_signed')
    expect(ev).toHaveLength(1)
    expect(ev[0]).toMatchObject({ name: 'factory', action: 'pause', presence: 'touchid', actor: { kind: 'person', id: 'p_sev' } })
    expect((ev[0].args as Record<string, string>).id).toHaveLength(120)
    expect(ev[0].args).not.toHaveProperty('nested')
    expect(ev[0].args).not.toHaveProperty('confirmed')
    expect(ev[0].changed).toBe(true)
    await run(s, 'pause', { confirmed: true, ticket: 'DEMO-0052' })
    const all = s.store.wsEventsOf(s.ws).filter((e) => e.type === 'addon.action_signed')
    expect(all).toHaveLength(2)
    expect(all[1]).toMatchObject({ changed: false, args: { ticket: 'DEMO-0052' } })
    expect(describeEvent(ev[0])).toBe('signed pause of factory')
  })
  it('Activity shows the record to owners and maintainers only', async () => {
    const s = setup()
    installAndGrant(s.store, s.ws, 'activity')
    await run(s, 'pause', { confirmed: true })
    const text = async () => JSON.stringify(await s.api.getAddonState(s.ws, 'activity'))
    expect(await text()).toContain('signed pause of factory')
    s.store.setViewer('p_tom')
    expect(await text()).not.toContain('signed pause')
  })
})

describe('visibility', () => {
  it('a child the viewer cannot see is not listed and its permit is not a decision', async () => {
    const s = setup()
    const hidden = (await state(s)).children[0].ticket
    ;(s.store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get(hidden)!.visibility = { restricted: ['p_sev'] }
    s.store.setViewer('p_mara')
    const st = await state(s)
    expect(JSON.stringify(st)).not.toContain(hidden)
    expect(JSON.stringify(await permitsOf(s))).not.toContain(hidden)
    expect(await fail(run(s, 'permit', { id: `factory.permit:P-1`, confirmed: true, option: 'once', ticket: hidden }))).toBe('404 not_visible')
  })
})
