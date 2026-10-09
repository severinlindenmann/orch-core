import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore, type MockStore } from '@/mocks/store'
import { describeEvent } from '@/mocks/derive'
import { refused } from '@/test/refused'
import { parseBlock } from '@/app/pages/ticket/widgets/parse'
import { asLand, attemptOf, recordMerge, step, type LandState } from './land-worker'

// Landing (D53): one serial worker over queues per (remote, target); approval binds to the candidate; any resolution
// voids the approval through core; stacked parents first; crash-resume never merges twice; main is never a target.
const setup = (viewer = 'p_sev', dataset: 'normal' | 'busy' = 'normal') => {
  const store = createMockStore({ persist: false, dataset })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  store.setViewer(viewer)
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
type S = ReturnType<typeof setup>
type View = Record<string, unknown> & { cards: Record<string, string>; body: { children: Record<string, unknown>[] }; workerAlert: { title: string; text: string } }
const view = async (s: S) => (await s.api.getAddonState(s.ws, 'land')) as View
const panel = async (s: S, key: string) => ((await s.api.getAddonState(s.ws, 'land', key)) as { byTicket: Record<string, { children: Record<string, unknown>[] }> }).byTicket[key]
const run = (s: S, id: string, body: Record<string, unknown> = {}) => s.api.runAddonAction(s.ws, 'land', id, body)
const land = (s: S) => asLand(s.store.addonState(s.ws, 'land'))
const text = (x: unknown) => JSON.stringify(x)
const landEvents = (s: S, key: string, type = 'land.attempt') => s.store.eventsOf(key).filter((e) => e.type === type)
/** Steps the worker until it is idle (or `max` steps). */
const drain = (s: S, max = 30) => {
  const log: string[] = []
  for (let i = 0; i < max; i++) {
    const did = step(s.store, land(s))
    if (did === null) break
    log.push(did)
  }
  return log
}
/** Give the verdict again as the reviewer (a person), the way the ticket page does. */
const verdict = async (s: S, key: string, as = 'p_mara') => {
  const before = s.store.viewer
  s.store.setViewer(as)
  await s.api.postAction(key, { action: 'verdict', result: 'pass', text: 'Looks right.' })
  s.store.setViewer(before)
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('land package', () => {
  it('is a preview catalog package, installed and on in DEMO, with manifest roles', () => {
    const { store, ws } = setup()
    const pkg = store.addons.find((a) => a.name === 'land')!
    expect(pkg.preview).toBe(true)
    expect(pkg.capabilities).toEqual(['network', 'git_push'])
    expect(store.workspaces.find((w) => w.id === ws)!.addons.land).toMatchObject({ enabled: true, installed: true })
    expect(pkg.actions).toMatchObject({
      save_settings: { minRole: 'owner' },
      resolve: { minRole: 'maintainer', decision: true },
      enqueue: { minRole: 'member' },
      view_history: { minRole: 'viewer', kind: 'navigation' },
      open_checks: { minRole: 'viewer', kind: 'navigation' },
    })
    expect(pkg.contributions.map((c) => c.slot).sort()).toEqual(['board.card_field', 'nav', 'settings', 'ticket.panel'])
  })
})

describe('normal day seed', () => {
  it('one queue per remote and target; DEMO-0052 is checked now (#14); history #9–#13 with the rebuilt candidate and the conflict', async () => {
    const s = setup()
    const st = land(s)
    expect(st.queues.map((q) => `${q.remote} → ${q.target}`).sort()).toEqual(['github.com/acme-energy/billing-api → develop', 'github.com/acme-energy/energy-dbt → develop'])
    const cur = attemptOf(st, st.worker.current)!
    expect(cur).toMatchObject({ n: 14, ticket: 'DEMO-0052', outcome: null, rebase: 'clean', timeout_at: '2026-10-09T11:50:00Z' })
    expect(cur.checks.map((c) => `${c.level}:${c.result}`)).toEqual(['T2:pass', 'CI:running'])
    expect(st.attempts.map((a) => `#${a.n} ${a.ticket} ${a.outcome ?? 'running'}${a.reason ? ` ${a.reason}` : ''}`)).toEqual([
      '#9 DEMO-0038 merged',
      '#10 DEMO-0042 merged',
      '#11 DEMO-0051 merged',
      '#12 DEMO-0053 requeued target_moved',
      '#13 DEMO-0053 failed conflict',
      '#14 DEMO-0052 running',
    ])
    // A rebuilt candidate is checked against the moved target, not the old one.
    expect(st.attempts[4].target_sha).not.toBe(st.attempts[3].target_sha)
    const v = await view(s)
    expect(v.cards).toMatchObject({ 'DEMO-0052': 'checking', 'DEMO-0053': 'back to review' })
    expect(v.workerAlert.title).toBe('Worker: checking DEMO-0052 (attempt #14) on github.com/acme-energy/billing-api → develop')
    expect(text(v.body)).toContain('1 from another workspace on this machine')
    // Raw lists never leave the store: only the filtered, derived view does.
    expect(v.attempts).toBeUndefined()
    expect(v.queues).toBeUndefined()
  })

  it("DEMO-0053's approval was voided by core (gate.invalidated with a reason): back to review, no verdict", async () => {
    const s = setup()
    const t = s.store.ticket('DEMO-0053')!
    expect(t.status).toBe('testing')
    expect(t.verdict).toBeNull()
    expect(t.gates.verify.state).toBe('invalidated')
    const inv = s.store.eventsOf('DEMO-0053').find((e) => e.type === 'gate.invalidated')!
    expect(inv).toMatchObject({ actor: { kind: 'host' }, gate: 'verify', addon: 'land' })
    expect(String(inv.reason)).toMatch(/resolved a rebase conflict/)
    const p = await panel(s, 'DEMO-0053')
    expect(text(p)).toContain('Conflict resolution voids the approval — back to review')
    expect(text(p)).toContain('Verify: invalidated by orch')
    const diff = p.children.find((n) => n.type === 'widget' && String(n.block).includes('"diff"'))!
    expect(parseBlock(String(diff.block)).reason).toBeUndefined()
  })

  it('a merged ticket says approved, tested and merged are the same candidate', async () => {
    const s = setup()
    const merged = land(s).attempts.find((a) => a.ticket === 'DEMO-0042')!
    const p = await panel(s, 'DEMO-0042')
    expect(text(p)).toContain(`Approved, tested and merged: the same candidate ${merged.candidate_sha}`)
    expect(merged.candidate_sha).not.toBe(merged.source_sha)
  })

  it('a ticket in testing without a verdict is not queued and says why; backlog tickets get no panel', async () => {
    const s = setup()
    expect(text(await panel(s, 'DEMO-0041'))).toContain('Enters the queue after the verdict')
    expect(await panel(s, 'DEMO-0049')).toBeUndefined()
  })
})

describe('the serial worker', () => {
  it('merges the current attempt after CI passes on the exact candidate, and records land.attempt as the addon', () => {
    const s = setup()
    const cur = attemptOf(land(s), land(s).worker.current)!
    expect(drain(s)).toEqual(['#14 DEMO-0052: merged'])
    const ev = landEvents(s, 'DEMO-0052')
    expect(ev).toHaveLength(1)
    expect(ev[0]).toMatchObject({ actor: { kind: 'addon', id: 'land' }, outcome: 'merged', candidate_sha: cur.candidate_sha, source_sha: cur.source_sha, target_sha: cur.target_sha, target: 'develop' })
    expect(land(s).queues.find((q) => q.id === cur.queue)!.head).toBe(cur.candidate_sha)
    expect(describeEvent(ev[0])).toBe(`landed on develop as ${cur.candidate_sha}`)
  })

  it('only approved tickets enter; a new verdict after the void lets DEMO-0053 land on a clean rebase', async () => {
    const s = setup()
    expect((await refused(run(s, 'enqueue', { ticket: 'DEMO-0041' }))).code).toBe('land.not_approved')
    expect((await refused(run(s, 'enqueue', { ticket: 'DEMO-0053' }))).code).toBe('land.not_approved')
    await verdict(s, 'DEMO-0053') // Mara approved before the void; the void lets her approve the resolved code
    expect(s.store.ticket('DEMO-0053')!.status).toBe('done')
    await run(s, 'enqueue', { ticket: 'DEMO-0053' })
    expect(landEvents(s, 'DEMO-0053', 'land.queued')).toHaveLength(1)
    expect((await view(s)).cards['DEMO-0053']).toBe('landing #2') // behind DEMO-0052, which is being checked
    const log = drain(s)
    expect(log).toEqual(['#14 DEMO-0052: merged', '#15 DEMO-0053: checking', '#15 DEMO-0053: T2 passed', '#15 DEMO-0053: merged'])
    expect(s.store.eventsOf('DEMO-0053').filter((e) => e.type === 'gate.invalidated')).toHaveLength(1) // a clean rebase keeps the approval
    expect((await view(s)).cards['DEMO-0053']).toBe('landed')
  })

  it('one attempt at a time: a second queue waits while the first is checked', async () => {
    const s = setup()
    await verdict(s, 'DEMO-0041', 'p_sev')
    await run(s, 'enqueue', { ticket: 'DEMO-0041' }) // energy-dbt queue
    expect(step(s.store, land(s))).toBe('#14 DEMO-0052: merged')
    expect(land(s).attempts.filter((a) => a.outcome === null)).toHaveLength(0)
    expect(step(s.store, land(s))).toBe('#15 DEMO-0041: checking')
    expect(land(s).attempts.filter((a) => a.outcome === null)).toHaveLength(1)
  })

  it('target moved while checking: the candidate is rebuilt and checked again, the approval kept', async () => {
    const s = setup()
    drain(s)
    await verdict(s, 'DEMO-0041', 'p_sev')
    await run(s, 'enqueue', { ticket: 'DEMO-0041' })
    land(s).queues.flatMap((q) => q.entries).find((e) => e.ticket === 'DEMO-0041')!.script = ['moves', 'clean']
    expect(drain(s)).toEqual(['#15 DEMO-0041: checking', '#15 DEMO-0041: develop moved, candidate rebuilt', '#16 DEMO-0041: checking', '#16 DEMO-0041: T2 passed', '#16 DEMO-0041: merged'])
    const [a, b] = land(s).attempts.slice(-2)
    expect(a.outcome).toBe('requeued')
    expect(b.target_sha).not.toBe(a.target_sha)
    expect(b.candidate_sha).not.toBe(a.candidate_sha)
    expect(s.store.ticket('DEMO-0041')!.gates.verify.state).toBe('approved')
  })

  it('a conflict fails the attempt, opens a need for the agent and a Today decision; the agent resolution voids the approval through core', async () => {
    const s = setup()
    drain(s)
    await verdict(s, 'DEMO-0041', 'p_sev')
    await run(s, 'enqueue', { ticket: 'DEMO-0041' })
    land(s).queues.flatMap((q) => q.entries).find((e) => e.ticket === 'DEMO-0041')!.script = ['conflict']
    expect(step(s.store, land(s))).toBe('#15 DEMO-0041: conflict')
    expect(land(s).queues.flatMap((q) => q.entries)).toHaveLength(0) // the queue moved on
    const need = land(s).needs.find((n) => n.ticket === 'DEMO-0041')!
    expect(need).toMatchObject({ kind: 'conflict', open: true, person: false, agent: 'claude-code:s_5d10:p_sev' })
    const d = (await s.api.getAddonDecisions(s.ws)).find((x) => x.addon === 'land')!
    expect(d).toMatchObject({ title: 'Needs: conflict', ticket: 'DEMO-0041', action: 'resolve' })
    expect(d.detail).toMatch(/Claude Code for Severin picks this up/)
    expect((await view(s)).cards['DEMO-0041']).toBe('conflict')
    expect(step(s.store, land(s))).toBeNull() // the agent picks it up
    expect(step(s.store, land(s))).toBe('DEMO-0041: Claude Code resolved the conflict')
    const t = s.store.ticket('DEMO-0041')!
    expect(t.gates.verify.state).toBe('invalidated')
    expect(t.status).toBe('testing')
    expect(t.verdict).toBeNull()
    const inv = s.store.eventsOf('DEMO-0041').filter((e) => e.type === 'gate.invalidated')
    expect(inv).toHaveLength(1)
    expect(inv[0]).toMatchObject({ actor: { kind: 'host' }, addon: 'land', gate: 'verify' })
    expect(landEvents(s, 'DEMO-0041', 'land.resolved')[0].actor).toMatchObject({ kind: 'addon', id: 'land' })
    expect(text(await panel(s, 'DEMO-0041'))).toContain('Conflict resolution voids the approval — back to review')
    expect(s.store.needsYou(s.ws, 'p_sev').some((i) => i.kind === 'verdict' && i.ticket === 'DEMO-0041')).toBe(true) // back to review
  })

  it('a conflict a person must resolve: deciding "I will resolve it" records the resolution and core voids the approval', async () => {
    const s = setup()
    drain(s)
    await verdict(s, 'DEMO-0041', 'p_sev')
    await run(s, 'enqueue', { ticket: 'DEMO-0041' })
    land(s).queues.flatMap((q) => q.entries)[0].script = ['conflict']
    step(s.store, land(s))
    land(s).needs[land(s).needs.length - 1].person = true
    const d = (await s.api.getAddonDecisions(s.ws)).find((x) => x.addon === 'land')!
    expect(d.title).toBe('Needs: conflict, a person must resolve')
    expect(d.options.map((o) => o.key)).toEqual(['self', 'agent', 'drop'])
    expect((await refused(run(s, 'resolve', { id: d.id, option: 'self' }))).code).toBe('confirm.required') // only core's prompt sets confirmed
    await run(s, 'resolve', { id: d.id, option: 'self', confirmed: true })
    expect(s.store.ticket('DEMO-0041')!.gates.verify.state).toBe('invalidated')
    expect(land(s).resolutions.at(-1)).toMatchObject({ ticket: 'DEMO-0041', by: 'p_sev' })
    expect(s.store.wsEventsOf(s.ws).some((e) => e.type === 'addon.decided' && e.name === 'land')).toBe(true)
    expect((await s.api.getAddonDecisions(s.ws)).some((x) => x.addon === 'land')).toBe(false)
  })

  it('red checks: the failing check is shown, the approval stands, and "Re-run the checks" queues the same code again', async () => {
    const s = setup()
    drain(s)
    await verdict(s, 'DEMO-0041', 'p_sev')
    await run(s, 'enqueue', { ticket: 'DEMO-0041' })
    land(s).queues.flatMap((q) => q.entries)[0].script = ['red']
    expect(drain(s, 3)).toEqual(['#15 DEMO-0041: checking', '#15 DEMO-0041: T2 passed', '#15 DEMO-0041: red checks'])
    expect(s.store.ticket('DEMO-0041')!.gates.verify.state).toBe('approved')
    const p = text(await panel(s, 'DEMO-0041'))
    expect(p).toContain('Red checks: CI on the pull request failed')
    const d = (await s.api.getAddonDecisions(s.ws)).find((x) => x.addon === 'land')!
    expect(d.title).toBe('Needs: red checks')
    await run(s, 'resolve', { id: d.id, option: 'rerun', confirmed: true })
    expect(land(s).queues.flatMap((q) => q.entries).map((e) => e.ticket)).toEqual(['DEMO-0041'])
    expect(land(s).queues.flatMap((q) => q.entries)[0].source_sha).toBe(land(s).attempts.at(-1)!.source_sha)
  })

  it('stacked tickets land parent first; a failed parent blocks its descendant', async () => {
    const s = setup()
    drain(s)
    await verdict(s, 'DEMO-0041', 'p_sev')
    await verdict(s, 'DEMO-0053')
    await run(s, 'enqueue', { ticket: 'DEMO-0053' })
    await run(s, 'enqueue', { ticket: 'DEMO-0041' })
    const entries = land(s).queues.flatMap((q) => q.entries)
    // DEMO-0053 (billing-api) is stacked on DEMO-0041 and waits for it although it was queued first.
    Object.assign(entries.find((e) => e.ticket === 'DEMO-0053')!, { stacked_on: 'DEMO-0041' })
    entries.find((e) => e.ticket === 'DEMO-0041')!.script = ['red']
    expect(text((await view(s)).body)).toContain('waits for DEMO-0041 (ahead in the queue)')
    expect(drain(s)).toEqual(['#15 DEMO-0041: checking', '#15 DEMO-0041: T2 passed', '#15 DEMO-0041: red checks'])
    expect(text((await view(s)).body)).toContain('waits for DEMO-0041 (its landing failed)')
    expect((await view(s)).cards['DEMO-0053']).toBe('landing · waits')
    expect(text(await panel(s, 'DEMO-0053'))).toContain('A stacked ticket never lands without its parent.')
  })

  it('after a crash between recording a merge and tidying the queue, the worker resumes and never merges again', () => {
    const s = setup()
    const st = land(s)
    const cur = attemptOf(st, st.worker.current)!
    step(s.store, st) // CI passes: merged (phase 1 + 2)
    expect(landEvents(s, 'DEMO-0052').filter((e) => e.outcome === 'merged')).toHaveLength(1)
    // Simulate a crash: the next merge is recorded, the queue not yet tidied.
    const s2 = setup()
    const st2 = land(s2)
    const a = attemptOf(st2, st2.worker.current)!
    a.checks.forEach((c) => (c.result = 'pass'))
    recordMerge(s2.store, st2, a)
    const head = st2.queues.find((q) => q.id === a.queue)!.head
    expect(step(s2.store, st2)).toBe(`resumed from attempt #${a.n}, no repeat merge`)
    expect(landEvents(s2, a.ticket).filter((e) => e.outcome === 'merged')).toHaveLength(1)
    expect(st2.queues.find((q) => q.id === a.queue)!.head).toBe(head)
    expect(st2.queues.flatMap((q) => q.entries).some((e) => e.ticket === a.ticket)).toBe(false)
    expect(st2.worker.resumed).toMatchObject({ from: a.n })
    expect(cur.n).toBe(a.n)
  })

  it('Run the worker (demo) plays steps on the simulator and stops when idle', async () => {
    const s = setup()
    await run(s, 'run_worker')
    expect(s.store.sim.running()).toContain(`land:${s.ws}`)
    expect((await view(s)).workerAlert.text).toMatch(/Demo worker running/)
    await vi.advanceTimersByTimeAsync(3000)
    expect(landEvents(s, 'DEMO-0052')).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(3000)
    expect(s.store.sim.running()).not.toContain(`land:${s.ws}`)
  })
})

describe('queue actions and settings', () => {
  it('dequeue takes a waiting ticket off; the one being checked cannot be taken off', async () => {
    const s = setup()
    expect((await refused(run(s, 'dequeue', { ticket: 'DEMO-0052' }))).code).toBe('land.checking')
    expect(await run(s, 'dequeue', { ticket: 'DEMO-0041' })).toMatchObject({ ok: true, message: 'DEMO-0041 was not queued.' })
    drain(s)
    await verdict(s, 'DEMO-0041', 'p_sev')
    await run(s, 'enqueue', { ticket: 'DEMO-0041' })
    await run(s, 'dequeue', { ticket: 'DEMO-0041' })
    expect(land(s).queues.flatMap((q) => q.entries)).toHaveLength(0)
    expect(landEvents(s, 'DEMO-0041', 'land.dequeued')[0].actor).toMatchObject({ kind: 'addon', id: 'land' })
  })

  it('main is never an allowed target (D33); owners only; names checked; a busy target is kept', async () => {
    const s = setup()
    const r = await refused(run(s, 'save_settings', { targets: 'develop, main', timeout_minutes: 30 }))
    expect(r).toMatchObject({ status: 409, code: 'land.target_refused' })
    expect(r.message).toMatch(/D33/)
    expect((await refused(run(s, 'save_settings', { targets: 'dev elop' }))).code).toBe('validation')
    expect((await refused(run(s, 'save_settings', { targets: 'release', timeout_minutes: 30 }))).code).toBe('land.target_busy') // DEMO-0052 waits on develop
    expect((await refused(run(s, 'save_settings', { targets: 'develop', timeout_minutes: 500 }))).code).toBe('validation')
    expect(await run(s, 'save_settings', { targets: 'develop, release/2026.10', timeout_minutes: 45 })).toMatchObject({ ok: true, changed: true })
    expect(land(s).settings).toEqual({ targets: ['develop', 'release/2026.10'], timeout_minutes: 45 })
    expect(((await view(s)).settings as { targets: string }).targets).toBe('develop, release/2026.10')
    s.store.setViewer('p_mara')
    expect((await refused(run(s, 'save_settings', { targets: 'develop' }))).status).toBe(403)
  })

  it('viewers read and switch tabs but cannot queue; open_checks hands out the CI link', async () => {
    const s = setup('p_tom')
    expect((await refused(run(s, 'enqueue', { ticket: 'DEMO-0052' }))).status).toBe(403)
    await run(s, 'view_history')
    const v = await view(s)
    expect(text(v.body)).toContain('"columns"')
    expect((await refused(run(s, 'open_checks', { n: 13 }))).code).toBe('not_found') // a conflict ran no checks
    expect(((await run(s, 'open_checks', { n: 14 })) as { url: string }).url).toMatch(/^https:\/\/github\.com\/acme-energy\/billing-api\/actions\/runs\/\d+$/)
    expect((await refused(run(s, 'open_checks', { n: 999 }))).code).toBe('not_found')
  })
})

describe('visibility', () => {
  const hide = (store: MockStore, keys: string[]) => {
    for (const k of keys) (store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get(k)!.visibility = { restricted: ['p_sev'] }
  }
  it('an outside member sees no data about restricted tickets: queue rows, history, chips, panel, decisions', async () => {
    const s = setup('p_mara')
    hide(s.store, ['DEMO-0052', 'DEMO-0053'])
    const v = await view(s)
    const all = text(v)
    for (const k of ['DEMO-0052', 'DEMO-0053', 'Prorate mid-month', 'billing_run_id to invoice']) expect(all).not.toContain(k)
    expect(v.workerAlert.title).toMatch(/checking a ticket you cannot see/)
    expect(all).toContain('checking a ticket you cannot see')
    expect((await refused(s.api.getAddonState(s.ws, 'land', 'DEMO-0052'))).code).toBe('not_visible')
    expect((await refused(run(s, 'enqueue', { ticket: 'DEMO-0052' }))).code).toBe('not_visible')
    s.store.setViewer('p_sev')
    land(s).needs.push({ id: 'N-99', ticket: 'DEMO-0053', kind: 'conflict', attempt: 13, agent: null, person: true, open: true, at: s.store.now(), file: 'x.sql' })
    expect((await s.api.getAddonDecisions(s.ws)).some((d) => d.id === 'land.need:N-99')).toBe(true)
    s.store.setViewer('p_mara')
    expect((await s.api.getAddonDecisions(s.ws)).some((d) => d.id === 'land.need:N-99')).toBe(false)
  })
})

describe('core voidApproval', () => {
  it('refuses for an inactive addon or a gate that is not approved', () => {
    const s = setup()
    expect(s.store.voidApproval('DEMO-0041', 'verify', { addon: 'land', reason: 'x' })).toMatchObject({ ok: false, code: 'gate.not_approved' })
    s.store.addonOp(s.ws, 'land', { op: 'disable' }, { kind: 'person', id: 'p_sev' })
    expect(s.store.voidApproval('DEMO-0052', 'verify', { addon: 'land', reason: 'x' })).toMatchObject({ ok: false, code: 'addon.inactive' })
  })
})

describe('busy day', () => {
  it('a long serial history with rebuilt candidates, a red check and a conflict waiting for a person, stacked entries and the resume note', async () => {
    const s = setup('p_sev', 'busy')
    const st: LandState = land(s)
    expect(st.attempts.length).toBeGreaterThan(20)
    expect(st.attempts.filter((a) => a.outcome === null)).toHaveLength(1)
    // Serial: no attempt starts before the previous one ended.
    for (let i = 1; i < st.attempts.length; i++) expect(st.attempts[i].started >= (st.attempts[i - 1].ended ?? st.attempts[i - 1].started)).toBe(true)
    expect(st.attempts.some((a) => a.outcome === 'requeued')).toBe(true)
    expect(st.needs.filter((n) => n.open).map((n) => `${n.kind}:${n.person}`).sort()).toEqual(['conflict:true', 'red_checks:false'])
    expect(st.worker.resumed?.from).toBe(12)
    const v = await view(s)
    expect(v.workerAlert.text).toContain('resumed from attempt #12, no repeat merge')
    expect(text(v.body)).toMatch(/waits for DEMO-\d+ \(its landing failed\)/)
    expect(text(v.body)).toMatch(/waits for DEMO-\d+ \(ahead in the queue\)/)
    const ds = (await s.api.getAddonDecisions(s.ws)).filter((d) => d.addon === 'land')
    expect(ds.map((d) => d.title).sort()).toEqual(['Needs: conflict, a person must resolve', 'Needs: red checks'])
  })
})
