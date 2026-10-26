import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { offered } from '@/test/offered'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { refused } from '@/test/refused'

// Schedules (later, preview): three kinds with when/on, skill, armed state, last and next run from the mock clock.
// Arm/Disarm are signed by core; "Run now" adds a run with a markdown report; a recurring finding is a Today decision.
const setup = (viewer = 'p_sev', install = true) => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  if (install) installAndGrant(store, ws, 'schedules')
  store.setViewer(viewer)
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
type S = ReturnType<typeof setup>
interface Row {
  id: string
  name: string
  kind: string
  trigger: string
  skill: string
  armed: boolean
  last: string
  next: string
}
interface State {
  rows: Row[]
  scheduleRows: { id: string; name: string; state: string; timing: string; next: string; last: string; outcome: string; enabled: boolean }[]
  runs: { id: string; schedule: string; result: string }[]
  runItems: { title: string; subtitle?: string }[]
  report: string
  reportTitle: string
  openFindings: number
  armedCount: number
}
const state = async (s: S) => (await s.api.getAddonState(s.ws, 'schedules')) as unknown as State
// A decision answer carries the digest of the decision as offered (security review #3), as core's prompt posts it.
const run = (s: S, id: string, body: Record<string, unknown> = {}) => s.api.runAddonAction(s.ws, 'schedules', id, offered(s.store, s.ws, 'schedules', id, body))
const decisions = async (s: S) => (await s.api.getAddonDecisions(s.ws)).filter((d) => d.addon === 'schedules')
const fail = (p: Promise<unknown>) => p.then(() => 'ok', (e: { status: number; code: string; message: string }) => `${e.status} ${e.code}`)
const row = async (s: S, id: string) => (await state(s)).rows.find((r) => r.id === id)!

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('schedules package and seed', () => {
  it('starts in the catalog as a preview; arm/disarm are signed maintainer actions, opening a run is viewer-level', () => {
    const { store, ws } = setup('p_sev', false)
    expect(store.workspaces.find((w) => w.id === ws)!.addons.schedules).toBeUndefined()
    const pkg = store.addons.find((a) => a.name === 'schedules')!
    expect(pkg.preview).toBe(true)
    expect(pkg.actions).toMatchObject({ arm: { minRole: 'maintainer', confirm: 'sign' }, disarm: { minRole: 'maintainer', confirm: 'sign' }, open_run: { minRole: 'viewer' } })
  })
  it('three kinds: schedule, listener and recurring, each with when/on, skill, armed state, last and next run', async () => {
    const { rows } = await state(setup())
    expect(rows.map((r) => r.kind).sort()).toEqual(['listener', 'recurring', 'schedule'])
    const sch = rows.find((r) => r.kind === 'schedule')!
    expect(sch.trigger).toBe('every 1 h, 08:00-19:00, Mon-Fri')
    expect(sch.skill).toBe('triage-inbox')
    expect(rows.find((r) => r.kind === 'listener')!.trigger).toBe('when a ticket moves to testing (cooldown 30 min)')
    expect(rows.find((r) => r.kind === 'recurring')!.trigger).toBe('weekly on Monday at 07:00')
    expect(rows.some((r) => r.armed)).toBe(true)
    expect(rows.some((r) => !r.armed)).toBe(true)
  })
  it('last and next run come from the mock clock (Friday 11:30 UTC)', async () => {
    const s = setup()
    expect((await row(s, 'check-inbox')).next).toBe('Fri 12:00')
    expect((await row(s, 'deps-weekly')).next).toBe('Mon 07:00')
    expect((await row(s, 'deps-weekly')).last).toMatch(/^4 days ago/)
    vi.advanceTimersByTime(60 * 60 * 1000)
    expect((await row(s, 'check-inbox')).next).toBe('Fri 13:00')
  })
})

describe('arming', () => {
  it('is refused without core\'s signature and for people who are not maintainers', async () => {
    const s = setup()
    expect(await fail(run(s, 'arm', { id: 'smoke-on-testing' }))).toBe('409 confirm.required')
    const t = setup('p_tom')
    expect(await fail(t.api.runAddonAction(t.ws, 'schedules', 'arm', { id: 'smoke-on-testing', confirmed: true }))).toBe('403 forbidden')
  })
  it('arming shows the next run; a listener waits for the next event', async () => {
    const s = setup()
    expect((await row(s, 'smoke-on-testing')).armed).toBe(false)
    expect((await row(s, 'smoke-on-testing')).next).toBe('disabled')
    await run(s, 'arm', { id: 'smoke-on-testing', confirmed: true })
    const r = await row(s, 'smoke-on-testing')
    expect(r.armed).toBe(true)
    expect(r.next).toBe('on the next ticket moved to testing')
  })
  it('disarming clears the next run, arming again starts from now', async () => {
    const s = setup()
    await run(s, 'disarm', { id: 'check-inbox', confirmed: true })
    expect(await row(s, 'check-inbox')).toMatchObject({ armed: false, next: 'disabled' })
    vi.advanceTimersByTime(2 * 60 * 60 * 1000)
    await run(s, 'arm', { id: 'check-inbox', confirmed: true })
    expect((await row(s, 'check-inbox')).next).toBe('Fri 14:00')
  })
  it('the table says Enabled or Disabled and keeps timing, next run, last run and result in their own columns', async () => {
    const { scheduleRows } = await state(setup())
    const by = (name: string) => scheduleRows.find((r) => r.name === name)!
    expect(by('Smoke test on testing')).toMatchObject({ state: 'Disabled', enabled: false, next: '–', last: 'never', outcome: '–' })
    expect(by('Check inbox')).toMatchObject({ state: 'Enabled', enabled: true, last: '30 min ago', outcome: 'quiet' })
    expect(by('Check inbox').timing).toContain('every 1 h')
    expect(by('Check inbox').timing).not.toContain('armed')
  })
  it('an unknown schedule is refused', async () => {
    expect(await fail(run(setup(), 'arm', { id: 'nope', confirmed: true }))).toBe('404 not_found')
  })
})

describe('Run now and the run history', () => {
  it('adds a run with a markdown report and opens it for the viewer', async () => {
    const s = setup()
    const before = (await state(s)).runs.length
    await run(s, 'run_now', { id: 'check-inbox' })
    const st = await state(s)
    expect(st.runs).toHaveLength(before + 1)
    expect(st.runs[0]).toMatchObject({ schedule: 'check-inbox', result: 'quiet' })
    expect(st.report).toContain('mails')
    expect(st.reportTitle).toContain('Check inbox')
    expect((await row(s, 'check-inbox')).last).toBe('just now, quiet')
  })
  it('refuses an unarmed schedule with a reason', async () => {
    const s = setup()
    expect(await fail(run(s, 'run_now', { id: 'smoke-on-testing' }))).toBe('409 schedule.not_armed')
  })
  it('open_run (viewer) switches the report per viewer', async () => {
    const s = setup('p_tom')
    const st = await state(s)
    const older = st.runs[st.runs.length - 1]
    await run(s, 'open_run', { run: older.id })
    expect((await state(s)).reportTitle).toContain(older.id)
    s.store.setViewer('p_sev')
    expect((await state(s)).reportTitle).not.toContain(older.id)
  })
  it('keeps only the latest runs in the list', async () => {
    const s = setup()
    for (let i = 0; i < 12; i++) await run(s, 'run_now', { id: 'check-inbox' })
    expect((await state(s)).runItems.length).toBeLessThanOrEqual(8)
  })
})

describe('a recurring finding lands on Today', () => {
  it('"Dependency update · Monday: file it?" is a core decision', async () => {
    const s = setup()
    const [d] = await decisions(s)
    expect(d).toMatchObject({ question: 'Dependency update · Monday: file it?', title: 'Weekly dependency update' })
    expect(d.options.map((o) => o.label)).toEqual(['File ticket in backlog', 'Dismiss'])
    expect(d.detail).toContain('Update dependencies, week 41')
    expect((await state(s)).openFindings).toBe(1)
  })
  it('filing creates a backlog ticket through core and closes the decision', async () => {
    const s = setup()
    const [d] = await decisions(s)
    const res = await run(s, 'finding', { id: d.id, confirmed: true, option: 'file' })
    expect(res.message).toMatch(/DEMO-\d{4}/)
    const key = /DEMO-\d{4}/.exec(res.message)![0]
    expect(s.store.ticket(key)).toMatchObject({ title: 'Update dependencies, week 41', status: 'backlog', type: 'chore' })
    expect(await decisions(s)).toEqual([])
    expect(JSON.stringify(await state(s))).toContain(key)
  })
  it('dismiss closes it without a ticket', async () => {
    const s = setup()
    const n = s.store.ticketKeys(s.ws).length
    const [d] = await decisions(s)
    await run(s, 'finding', { id: d.id, confirmed: true, option: 'dismiss' })
    expect(await decisions(s)).toEqual([])
    expect(s.store.ticketKeys(s.ws)).toHaveLength(n)
  })
  it('Run now on the recurring schedule files a new finding; a closed decision does nothing; viewers get none', async () => {
    const s = setup()
    const [d] = await decisions(s)
    await run(s, 'finding', { id: d.id, confirmed: true, option: 'dismiss' })
    expect(await refused(run(s, 'finding', { id: d.id, confirmed: true, option: 'file' }))).toMatchObject({ status: 409, code: 'decision.closed', message: 'That decision is closed.' })
    await run(s, 'run_now', { id: 'deps-weekly' })
    const next = await decisions(s)
    expect(next).toHaveLength(1)
    expect(next[0].question).toBe('Dependency update · Friday: file it?')
    expect(await decisions(setup('p_tom'))).toEqual([])
  })
  it('a filed ticket the viewer cannot see is not named in the run history', async () => {
    const s = setup()
    const [d] = await decisions(s)
    const key = /DEMO-\d{4}/.exec((await run(s, 'finding', { id: d.id, confirmed: true, option: 'file' })).message)![0]
    ;(s.store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get(key)!.visibility = { restricted: ['p_sev'] }
    s.store.setViewer('p_mara')
    const json = JSON.stringify(await state(s))
    expect(json).not.toContain(key)
  })
})

describe('review fixes', () => {
  it('each schedule shows its last run', async () => {
    const { scheduleRows } = await state(setup())
    expect(scheduleRows.find((r) => r.name === 'Check inbox')).toMatchObject({ last: '30 min ago', outcome: 'quiet' })
    expect(scheduleRows.find((r) => r.name === 'Smoke test on testing')).toMatchObject({ last: 'never' })
  })
  it('arm and disarm leave a core record, and the schedule remembers who disarmed', async () => {
    const s = setup('p_mara')
    await run(s, 'disarm', { id: 'check-inbox', confirmed: true })
    const ev = s.store.wsEventsOf(s.ws).filter((e) => e.type === 'addon.action_signed')
    expect(ev).toHaveLength(1)
    expect(ev[0]).toMatchObject({ name: 'schedules', action: 'disarm', args: { id: 'check-inbox' }, actor: { id: 'p_mara' } })
    const sched = (s.store.addonState(s.ws, 'schedules').schedules as { id: string; disarmedBy?: string }[]).find((x) => x.id === 'check-inbox')!
    expect(sched.disarmedBy).toBe('p_mara')
  })
  it('an unknown finding option is a 400 and files nothing', async () => {
    const s = setup()
    const [d] = await decisions(s)
    const n = s.store.ticketKeys(s.ws).length
    expect(await fail(run(s, 'finding', { id: d.id, confirmed: true, option: 'maybe' }))).toBe('400 validation.option')
    expect(s.store.ticketKeys(s.ws)).toHaveLength(n)
    expect(await decisions(s)).toHaveLength(1)
  })
})

