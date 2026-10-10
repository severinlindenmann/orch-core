// Factory full runs (owner decision 2026-10-10 evening, D61 option): the request, the signed start, the steps with
// their labels, the hold before Deliver, Stop during the hold and the delivery after it.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { RUN_STEP_MS, runArgs, type Run, type RunDraft } from './factory-runs'

const EPIC = 'DEMO-0050'
const DELIVER = { goal: 'Release monthly billing v2', goes_up_to: 'Deliver', deliver_means: 'Deploy to production', hold_minutes: 30 }

function setup(viewer = 'p_sev') {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  installAndGrant(store, ws, 'factory')
  store.setViewer(viewer)
  const run = (id: string, body: Record<string, unknown>) => store.runAddon(ws, 'factory', id, body)!
  const view = () => store.addonStateView(ws, 'factory')!
  const runs = () => store.addonState(ws, 'factory').runs as Run[]
  const events = (type: string) => store.eventsOf(EPIC).filter((e) => e.type === type)
  return { store, ws, run, view, runs, events }
}
type S = ReturnType<typeof setup>

/** Review the request, then sign and start it (core's `confirmed`), as the page does. */
function startDeliverRun(s: S, form: Record<string, unknown> = DELIVER) {
  expect(s.run('prepare_run', { formData: form })).toMatchObject({ ok: true })
  const res = s.run('start_run', { ...runArgs(form as unknown as RunDraft), confirmed: true })
  expect(res).toMatchObject({ ok: true })
  return s.runs()[0]
}
/** Steps until the run holds before Deliver (plan + 3 children × 4 steps + Preview + hold). */
const toHold = () => vi.advanceTimersByTime(RUN_STEP_MS * 20)

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('factory full run: the request', () => {
  it('Deliver needs a destination; Preview needs none', () => {
    const s = setup()
    expect(s.run('prepare_run', { formData: { goal: 'X', goes_up_to: 'Deliver', hold_minutes: 30 } })).toMatchObject({ ok: false, status: 400, code: 'validation', message: expect.stringMatching(/^Say what Deliver means/) })
    expect(s.run('prepare_run', { formData: { goal: 'X', goes_up_to: 'Deliver', deliver_means: '   ', hold_minutes: 30 } })).toMatchObject({ ok: false, code: 'validation' })
    expect(s.run('prepare_run', { formData: { goal: '', goes_up_to: 'Preview' } })).toMatchObject({ ok: false, code: 'validation' })
    expect(s.run('prepare_run', { formData: { goal: 'X', goes_up_to: 'Deliver', deliver_means: 'Publish', hold_minutes: 7 } })).toMatchObject({ ok: false, code: 'validation' })
    expect(s.run('prepare_run', { formData: { goal: 'Draft a campaign', goes_up_to: 'Preview' } })).toMatchObject({ ok: true })
  })
  it('only an owner signs a run that goes to Deliver; a maintainer may request Preview', () => {
    const s = setup('p_mara')
    expect(s.store.roleIn(s.ws, 'p_mara')).toBe('maintainer')
    expect(s.run('prepare_run', { formData: DELIVER })).toMatchObject({ ok: false, status: 409, code: 'factory.deliver_owner_only' })
    expect(s.run('prepare_run', { formData: { goal: 'Draft', goes_up_to: 'Preview' } })).toMatchObject({ ok: true })
  })
  it('start needs core\'s signature and exactly the reviewed values', () => {
    const s = setup()
    s.run('prepare_run', { formData: DELIVER })
    const args = runArgs(DELIVER as unknown as RunDraft)
    expect(s.run('start_run', args)).toMatchObject({ ok: false, code: 'confirm.required' })
    expect(s.run('start_run', { ...args, deliver_means: 'Deploy to staging', confirmed: true })).toMatchObject({ ok: false, code: 'factory.stale' })
    expect(s.run('start_run', { ...args, confirmed: true })).toMatchObject({ ok: true })
    expect(s.runs()[0]).toMatchObject({ id: 'R-1', goesUpTo: 'Deliver', deliverMeans: 'Deploy to production', holdMinutes: 30, signedBy: 'p_sev', stage: 'working' })
    // Core records the signature with every value; the addon records the request on the epic.
    const signed = s.store.wsEventsOf(s.ws).filter((e) => e.type === 'addon.action_signed' && e.action === 'start_run')
    expect(signed.at(-1)).toMatchObject({ args: { goal: DELIVER.goal, goes_up_to: 'Deliver', deliver_means: 'Deploy to production', hold_minutes: 30, largest_child: 'm' } })
    expect(s.events('factory.run_requested').at(-1)).toMatchObject({ run: 'R-1', deliver_means: 'Deploy to production', actor: { kind: 'addon', id: 'factory' } })
  })
})

describe('factory full run: steps, hold, Stop, delivery', () => {
  it('every decided step carries the full-run label', () => {
    const s = setup()
    startDeliverRun(s)
    vi.advanceTimersByTime(RUN_STEP_MS * 3)
    const node = JSON.stringify(s.view().runsNode)
    expect(node).toMatch(/via the factory full run you signed on \d+ Oct \d\d:\d\d — no person reviewed this step/)
    // Another viewer reads the signer's name.
    s.store.setViewer('p_mara')
    expect(JSON.stringify(s.view().runsNode)).toContain('via the factory full run Severin signed on')
  })
  it('reaches the hold window: a calm notice, a core decision with one option, Stop', () => {
    const s = setup()
    startDeliverRun(s)
    toHold()
    const r = s.runs()[0]
    expect(r.stage).toBe('holding')
    expect(s.events('factory.run_step').map((e) => e.step)).toEqual(['Plan', 'Preview'])
    expect(s.events('factory.deliver_held').at(-1)).toMatchObject({ run: 'R-1', deliver_means: 'Deploy to production', until: r.holdUntil })
    const rows = JSON.parse(JSON.stringify(s.view().runsNode)).children.find((n: { children?: unknown[] }) => n.children)?.children.at(-1).rows as { step: string; decided: string }[]
    expect(rows.map((x) => x.step)).toEqual(['Plan', 'Evidence collected', 'Evidence collected', 'Evidence collected', 'Preview', 'Deliver'])
    for (const row of rows.slice(0, 5)) expect(row.decided).toMatch(/^via the factory full run you signed on .* — no person reviewed this step$/)
    expect(JSON.stringify(s.view().attentionNode)).toContain('Delivering in 30 min · Deploy to production')
    const d = s.store.addonDecisions(s.ws).find((x) => x.id === 'factory.hold:R-1')!
    expect(d).toMatchObject({ options: [{ key: 'stop', label: 'Stop delivery' }], terms: { run: 'R-1', deliver_means: 'Deploy to production', hold_until: r.holdUntil }, hold: { until: r.holdUntil, deliver_means: 'Deploy to production' } })
  })
  it('Stop during the hold cancels: no delivery event, even after the window', () => {
    const s = setup()
    startDeliverRun(s)
    toHold()
    const d = s.store.addonDecisions(s.ws).find((x) => x.id === 'factory.hold:R-1')!
    expect(s.run('hold', { id: d.id, option: 'stop', ticket: d.ticket, terms: d.terms })).toMatchObject({ ok: false, code: 'confirm.required' })
    expect(s.run('hold', { id: d.id, option: 'stop', ticket: d.ticket, terms: d.terms, confirmed: true })).toMatchObject({ ok: true })
    expect(s.runs()[0]).toMatchObject({ stage: 'stopped', stopped: { by: 'p_sev' } })
    expect(s.events('factory.deliver_stopped')).toHaveLength(1)
    expect(s.store.wsEventsOf(s.ws).filter((e) => e.type === 'addon.decided' && e.id === 'factory.hold:R-1').at(-1)).toMatchObject({ option: 'stop', presence: 'touchid' })
    vi.advanceTimersByTime(31 * 60_000)
    s.view()
    expect(s.events('factory.delivered')).toHaveLength(0)
    expect(s.store.addonDecisions(s.ws).some((x) => x.id === 'factory.hold:R-1')).toBe(false)
    expect(JSON.stringify(s.view().runsNode)).toMatch(/Stopped by Severin at .*: nothing went out/)
  })
  it('the window ends with no Stop: delivered to exactly the signed destination', () => {
    const s = setup()
    startDeliverRun(s)
    toHold()
    vi.advanceTimersByTime(30 * 60_000 + RUN_STEP_MS)
    s.view()
    expect(s.runs()[0].stage).toBe('delivered')
    expect(s.events('factory.delivered')).toEqual([expect.objectContaining({ run: 'R-1', deliver_means: 'Deploy to production' })])
    expect(JSON.stringify(s.view().runsNode)).toMatch(/Delivered: Deploy to production at \d+ Oct \d\d:\d\d/)
    expect(s.store.addonDecisions(s.ws).some((x) => x.id === 'factory.hold:R-1')).toBe(false)
    // A late Stop finds nothing to stop.
    expect(s.run('hold', { id: 'factory.hold:R-1', option: 'stop', confirmed: true })).toMatchObject({ ok: false, code: 'decision.closed' })
  })
  it('a paused factory holds: no delivery while paused, and the paused time is given back', () => {
    const s = setup()
    startDeliverRun(s)
    toHold()
    const until = s.runs()[0].holdUntil!
    expect(s.run('pause', { confirmed: true })).toMatchObject({ ok: true })
    vi.advanceTimersByTime(40 * 60_000)
    s.view()
    expect(s.events('factory.delivered')).toHaveLength(0)
    expect(s.run('resume', { confirmed: true })).toMatchObject({ ok: true })
    expect(Date.parse(s.runs()[0].holdUntil!)).toBeGreaterThan(Date.parse(until) + 39 * 60_000)
  })
  it('a run up to Preview ends at Preview and never holds', () => {
    const s = setup()
    s.run('prepare_run', { formData: { goal: 'Draft the campaign', goes_up_to: 'Preview' } })
    expect(s.run('start_run', { goal: 'Draft the campaign', goes_up_to: 'Preview', largest_child: 'm', confirmed: true })).toMatchObject({ ok: true })
    toHold()
    vi.advanceTimersByTime(60 * 60_000)
    expect(s.runs()[0].stage).toBe('preview')
    expect(s.events('factory.deliver_held')).toHaveLength(0)
    expect(s.store.addonDecisions(s.ws).some((x) => x.id.startsWith('factory.hold:'))).toBe(false)
  })
  it('the busy day seeds a run on hold (28 min left) and an earlier delivered one', () => {
    const store = createMockStore({ persist: false, dataset: 'busy' })
    const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
    const v = store.addonStateView(ws, 'factory')!
    expect(JSON.stringify(v.attentionNode)).toContain('Delivering in 28 min · Publish campaign to the newsletter list')
    expect(JSON.stringify(v.runsNode)).toContain('Delivered: Send to finance@example.test (boss)')
    expect(store.addonDecisions(ws).some((d) => d.id === 'factory.hold:R-2' && d.hold)).toBe(true)
  })
})
