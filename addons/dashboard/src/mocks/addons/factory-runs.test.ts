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

/** The reviewed draft (with its host-issued request id) of this viewer. */
const draftOf = (s: S, viewer = 'p_sev') => (s.store.addonState(s.ws, 'factory').nav as Record<string, { runDraft?: RunDraft }>)[viewer].runDraft!
/** Review the request, then sign and start it (core's `confirmed`), as the page does. */
function startDeliverRun(s: S, form: Record<string, unknown> = DELIVER) {
  expect(s.run('prepare_run', { formData: form })).toMatchObject({ ok: true })
  const res = s.run('start_run', { ...runArgs(draftOf(s)), confirmed: true })
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
    const args = runArgs(draftOf(s))
    expect(args.request).toMatch(/^rq-\d+$/)
    expect(s.run('start_run', args)).toMatchObject({ ok: false, code: 'confirm.required' })
    expect(s.run('start_run', { ...args, deliver_means: 'Deploy to staging', confirmed: true })).toMatchObject({ ok: false, code: 'factory.stale' })
    expect(s.run('start_run', { ...args, confirmed: true })).toMatchObject({ ok: true })
    expect(s.runs()[0]).toMatchObject({ id: 'R-1', goesUpTo: 'Deliver', deliverMeans: 'Deploy to production', holdMinutes: 30, signedBy: 'p_sev', stage: 'working' })
    // Core records the signature with every value; the addon records the request on the epic.
    const signed = s.store.wsEventsOf(s.ws).filter((e) => e.type === 'addon.action_signed' && e.action === 'start_run')
    expect(signed.at(-1)).toMatchObject({ args: { request: args.request, goal: DELIVER.goal, goes_up_to: 'Deliver', deliver_means: 'Deploy to production', hold_minutes: 30, largest_child: 'm' } })
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
    expect(JSON.stringify(s.view().attentionNode)).toContain('on hold before Deliver')
    expect(JSON.stringify(s.view().runsNode)).toContain('Delivering in 30 min · Deploy to production')
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
  it('no operation shortens a hold: the demo simulator is owner-only and refused outside the demo data', () => {
    const s = setup()
    startDeliverRun(s)
    toHold()
    s.store.setViewer('p_mara')
    expect(s.run('simulate_time', { run: 'R-1' })).toMatchObject({ ok: false, status: 403 })
    expect(JSON.stringify(s.view().attentionNode)).not.toContain('simulate_time')
    s.store.setViewer('p_sev')
    const real = s.store.dataset
    ;(s.store as { dataset: string }).dataset = 'host'
    expect(s.run('simulate_time', { run: 'R-1' })).toMatchObject({ ok: false, status: 409, code: 'factory.not_demo' })
    expect(s.runs()[0].stage).toBe('holding')
    expect(s.events('factory.delivered')).toHaveLength(0)
    ;(s.store as { dataset: string }).dataset = real
    expect(s.run('simulate_time', { run: 'R-1' })).toMatchObject({ ok: true })
    expect(s.events('factory.delivered')).toHaveLength(1)
  })
  it('a restricted epic: a maintainer without access sees no hold decision and no run, and Stop is 404', () => {
    const s = setup()
    startDeliverRun(s)
    toHold()
    ;(s.store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get(EPIC)!.visibility = { restricted: ['p_sev'] }
    s.store.setViewer('p_mara')
    expect(s.store.addonDecisions(s.ws).some((d) => d.id.startsWith('factory.hold:'))).toBe(false)
    const seen = JSON.stringify(s.view())
    for (const leak of ['Deploy to production', 'Release monthly billing v2', 'factory.hold:R-1']) expect(seen).not.toContain(leak)
    expect(s.run('hold', { id: 'factory.hold:R-1', option: 'stop', ticket: EPIC, confirmed: true })).toMatchObject({ ok: false, status: 404, code: 'not_visible' })
    expect(s.run('prepare_run', { formData: { goal: 'X', goes_up_to: 'Preview' } })).toMatchObject({ ok: false, status: 404, code: 'not_visible' })
    expect(s.runs()[0].stage).toBe('holding')
    // Severin, who can see it, still can.
    s.store.setViewer('p_sev')
    expect(s.store.addonDecisions(s.ws).some((d) => d.id === 'factory.hold:R-1')).toBe(true)
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
    expect(s.run('start_run', { ...runArgs(draftOf(s)), confirmed: true })).toMatchObject({ ok: true })
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
    expect(JSON.stringify(v.attentionNode)).toContain('Full run R-2 · Autumn tariff campaign: on hold before Deliver')
    expect(JSON.stringify(v.runsNode)).toContain('Delivering in 28 min · Publish campaign to the newsletter list')
    expect(JSON.stringify(v.runsNode)).toContain('Delivered: Send to finance@example.test (boss)')
    expect(store.addonDecisions(ws).some((d) => d.id === 'factory.hold:R-2' && d.hold)).toBe(true)
  })
})

// Fix round 1 (Codex review U2).
describe('fix round 1: one charter check, budget reservation', () => {
  const expire = (s: S) => {
    s.store.addonState(s.ws, 'factory').startedAt = new Date(Date.parse(s.store.now()) - 73 * 3_600_000).toISOString()
  }
  it('admission: a stopped charter refuses a run; the budget is reserved atomically (refused when it does not fit)', () => {
    const s = setup()
    const st = s.store.addonState(s.ws, 'factory')
    st.used = 23
    s.run('prepare_run', { formData: DELIVER })
    const body = { ...runArgs(draftOf(s)), confirmed: true }
    expect(s.run('start_run', body)).toMatchObject({ ok: false, status: 409, code: 'factory.budget' })
    expect(st.used).toBe(23)
    expect(s.runs()).toHaveLength(0)
    st.used = 20
    expect(s.run('start_run', body)).toMatchObject({ ok: true }) // the refused attempt did not consume the request
    expect(st.used).toBe(23)
    s.run('prepare_run', { formData: DELIVER })
    expire(s)
    expect(s.run('start_run', { ...runArgs(draftOf(s)), confirmed: true })).toMatchObject({ ok: false, status: 409, code: 'factory.not_running' })
  })
  it('every step: an expired charter stops progress', () => {
    const s = setup()
    startDeliverRun(s)
    expire(s)
    vi.advanceTimersByTime(RUN_STEP_MS * 30)
    expect(s.runs()[0]).toMatchObject({ stage: 'working', planned: false })
    expect(s.events('factory.run_step')).toHaveLength(0)
  })
  it('settlement: a charter that stops during the hold ends it "not delivered", never delivered', () => {
    const s = setup()
    startDeliverRun(s)
    toHold()
    expire(s)
    vi.advanceTimersByTime(31 * 60_000)
    s.view()
    expect(s.runs()[0]).toMatchObject({ stage: 'not_delivered', notDelivered: { reason: 'charter stopped' } })
    expect(s.events('factory.delivered')).toHaveLength(0)
    expect(s.events('factory.deliver_cancelled')).toEqual([expect.objectContaining({ run: 'R-1', reason: 'charter_stopped' })])
    expect(JSON.stringify(s.view().runsNode)).toContain('Not delivered: the charter stopped')
    expect(s.store.addonDecisions(s.ws).some((d) => d.id.startsWith('factory.hold:'))).toBe(false)
  })
})

describe('fix round 1: the code review gate stays human in a full run', () => {
  const policyOn = (s: S) => s.store.appendWs(s.ws, { type: 'gate.policy_set', gate: 'code', approvers: 'maintainer', count: 1, not: 'assignees', applies: 'all' })
  it('each child waits at Code review for a person before Validate; Preview needs every review', () => {
    const s = setup()
    policyOn(s)
    startDeliverRun(s)
    vi.advanceTimersByTime(RUN_STEP_MS * 60)
    const r = s.runs()[0]
    expect(r.stage).toBe('working')
    expect(r.children.map((k) => [k.done, k.review?.state])).toEqual([[2, 'waiting'], [2, 'waiting'], [2, 'waiting']])
    const ids = s.store.addonDecisions(s.ws).filter((d) => d.id.startsWith('factory.code:')).map((d) => d.id)
    expect(ids).toEqual(['factory.code:R-1:1', 'factory.code:R-1:2', 'factory.code:R-1:3'])
    expect(JSON.stringify(s.view().runsNode)).toContain('Code review (waits for a person)')
    // Only through core's prompt (a person's signature), never on its own.
    const d = s.store.addonDecisions(s.ws).find((x) => x.id === ids[0])!
    expect(s.run('code_review', { id: d.id, option: 'approve', ticket: d.ticket, terms: d.terms })).toMatchObject({ ok: false, code: 'confirm.required' })
    s.store.setViewer('p_tom')
    expect(s.run('code_review', { id: d.id, option: 'approve', ticket: d.ticket, terms: d.terms, confirmed: true })).toMatchObject({ ok: false, status: 403 })
    s.store.setViewer('p_sev')
    for (const id of ids.slice(0, 2)) {
      const x = s.store.addonDecisions(s.ws).find((y) => y.id === id)!
      expect(s.run('code_review', { id, option: 'approve', ticket: x.ticket, terms: x.terms, confirmed: true })).toMatchObject({ ok: true })
    }
    vi.advanceTimersByTime(RUN_STEP_MS * 30)
    expect(s.runs()[0].stage).toBe('working') // one review still waits
    const last = s.store.addonDecisions(s.ws).find((y) => y.id === ids[2])!
    expect(s.run('code_review', { id: last.id, option: 'approve', ticket: last.ticket, terms: last.terms, confirmed: true })).toMatchObject({ ok: true })
    toHold()
    expect(s.runs()[0].stage).toBe('holding')
    expect(s.store.wsEventsOf(s.ws).filter((e) => e.type === 'addon.decided' && String(e.id).startsWith('factory.code:')).every((e) => e.presence === 'touchid' && (e.actor as { kind: string }).kind === 'person')).toBe(true)
    expect(JSON.stringify(s.view().runsNode)).toContain('code review approved by Severin')
  })
  it('without the policy, no code review step', () => {
    const s = setup()
    startDeliverRun(s)
    toHold()
    expect(s.runs()[0].stage).toBe('holding')
    expect(s.runs()[0].children.every((k) => !k.review)).toBe(true)
  })
})

describe('fix round 1: single-use request ids', () => {
  it('a signed start cannot be replayed, not even after re-reviewing the same values', () => {
    const s = setup()
    s.run('prepare_run', { formData: DELIVER })
    const first: Record<string, unknown> = { ...runArgs(draftOf(s)), confirmed: true }
    expect(s.run('start_run', first)).toMatchObject({ ok: true })
    expect(s.run('start_run', first)).toMatchObject({ ok: false, status: 409, code: 'factory.request_used' })
    s.run('prepare_run', { formData: DELIVER })
    const second = runArgs(draftOf(s))
    expect(second.request).not.toBe(first.request)
    expect(s.run('start_run', first)).toMatchObject({ ok: false, status: 409, code: 'factory.request_used' })
    expect(s.run('start_run', { ...second, confirmed: true })).toMatchObject({ ok: true })
    expect(s.runs()).toHaveLength(2)
  })
})

describe('fix round 1: the factory stays on while a delivery holds', () => {
  it('disable, update and uninstall are refused while a run holds; allowed again after Stop', () => {
    const s = setup()
    startDeliverRun(s)
    toHold()
    const actor = { kind: 'person', id: 'p_sev' } as const
    for (const op of [{ op: 'disable' }, { op: 'uninstall' }, { op: 'update', version: '9', package_sha256: 'x', capabilities: [], viewer_actions: [] }] as const) {
      const r = s.store.addonOp(s.ws, 'factory', op as never, actor)
      expect(r).toMatchObject({ ok: false, status: 409, code: 'addon.delivery_on_hold' })
      expect((r as { message: string }).message).toMatch(/^Full run R-1 "Release monthly billing v2" is on hold before Deliver \(Deploy to production\)/)
    }
    const d = s.store.addonDecisions(s.ws).find((x) => x.id === 'factory.hold:R-1')!
    s.run('hold', { id: d.id, option: 'stop', ticket: d.ticket, terms: d.terms, confirmed: true })
    expect(s.store.addonOp(s.ws, 'factory', { op: 'disable' }, actor)).toMatchObject({ ok: true })
  })
})

describe('fix round 1: a reload never extends a hold', () => {
  afterEach(() => localStorage.clear())
  it('the restarted mock clock does not restart the hold: the wall-clock deadline holds', () => {
    localStorage.clear()
    const a = createMockStore({ persist: true })
    const ws = a.workspaces.find((w) => w.prefix === 'DEMO')!.id
    installAndGrant(a, ws, 'factory')
    a.runAddon(ws, 'factory', 'prepare_run', { formData: DELIVER })
    const draft = (a.addonState(ws, 'factory').nav as Record<string, { runDraft: RunDraft }>).p_sev.runDraft
    expect(a.runAddon(ws, 'factory', 'start_run', { ...runArgs(draft), confirmed: true })).toMatchObject({ ok: true })
    toHold()
    a.sim.stopAll()
    vi.advanceTimersByTime(10 * 60_000)
    // Reload: a new store reads the saved state; its clock restarts near the last saved event.
    const b = createMockStore({ persist: true })
    const v = b.addonStateView(ws, 'factory')!
    const run = (b.addonState(ws, 'factory').runs as Run[])[0]
    expect(run.stage).toBe('holding')
    const left = (Date.parse(run.holdUntil!) - Date.parse(b.now())) / 60_000
    expect(left).toBeGreaterThan(19)
    expect(left).toBeLessThan(21)
    expect(JSON.stringify(v.runsNode)).toMatch(/Delivering in (20|21) min/)
    vi.advanceTimersByTime(21 * 60_000)
    b.addonStateView(ws, 'factory')
    expect((b.addonState(ws, 'factory').runs as Run[])[0].stage).toBe('delivered')
    b.sim.stopAll()
  })
})

describe('fix round 1: values through visible.tsx', () => {
  it('a destination with invisible characters is shown escaped everywhere; the signed terms keep the exact value', () => {
    const s = setup()
    const means = 'Deploy​ to prod‮'
    startDeliverRun(s, { ...DELIVER, deliver_means: means })
    toHold()
    const view = JSON.stringify(s.view())
    expect(view).not.toMatch(/[​‮]/)
    expect(view).toContain('Deploy\\\\u{200b} to prod\\\\u{202e}')
    const d = s.store.addonDecisions(s.ws).find((x) => x.id === 'factory.hold:R-1')!
    expect(d.question + d.detail).not.toMatch(/[​‮]/)
    expect(d.terms!.deliver_means).toBe(means)
  })
})
