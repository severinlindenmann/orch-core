import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import type { LaunchPreview } from '@/api/types'
import { describeEvent } from '@/mocks/derive'
import { createMockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'

// start-agent: a member starts an agent session on a ticket. Core confirms (and signs a grant when none is active),
// core starts the session under the viewer's grant and plays a simulated run on the ticket.
const setup = (viewer = 'p_sev') => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  store.setViewer(viewer)
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
type S = ReturnType<typeof setup>
interface State {
  previews: Record<string, LaunchPreview>
  runs: { session: string; ticket: string; state: string; mode: string; harness: string }[]
  byTicket: Record<string, { running: boolean; session?: string }>
}
const state = async (s: S) => (await s.api.getAddonState(s.ws, 'start-agent')) as unknown as State
const run = (s: S, id: string, body: Record<string, unknown> = {}) => s.api.runAddonAction(s.ws, 'start-agent', id, body)
const fail = (p: Promise<unknown>) => p.then(() => 'ok', (e: { status: number; code: string }) => `${e.status} ${e.code}`)
const LAUNCH = { mode: 'work', harness: 'claude-code', where: 'background' }
const start = (s: S, ticket = 'DEMO-0044', launch: Record<string, string> = LAUNCH) => run(s, 'start', { ticket, confirmed: true, launch })
const sessionOn = (s: S, ticket: string) => s.store.wsEventsOf(s.ws).filter((e) => e.type === 'agent.started' && e.ticket === ticket).at(-1)?.session as string

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('start-agent package', () => {
  it('starts installed in DEMO with spawn_agent granted; start/stop are member-level, settings owner-only', () => {
    const { store, ws } = setup()
    const pkg = store.addons.find((a) => a.name === 'start-agent')!
    expect(pkg.capabilities).toEqual(['spawn_agent'])
    expect(pkg.actions).toMatchObject({ start: { minRole: 'member', confirm: 'spawn_agent' }, stop: { minRole: 'member' }, save_settings: { minRole: 'owner' } })
    const inst = store.workspaces.find((w) => w.id === ws)!.addons['start-agent']
    expect(inst).toMatchObject({ enabled: true, status: 'active' })
    expect(inst.granted?.capabilities).toEqual(['spawn_agent'])
  })
  it('previews the exact command for the viewer\'s choice (default: work on ticket, Claude Code, background)', async () => {
    const p = (await state(setup())).previews['DEMO-0044']
    expect(p).toMatchObject({ ticket: 'DEMO-0044', mode: 'Work on ticket', harness: 'Claude Code', where: 'Background' })
    expect(p.command).toBe("orch session start --in background DEMO-0044 -- claude '/orch:work DEMO-0044'")
    expect(p.blocked).toBeUndefined()
  })
  it('configure stores the choice per viewer and per ticket', async () => {
    const s = setup()
    await run(s, 'configure', { ticket: 'DEMO-0044', formData: { mode: 'fix', harness: 'codex', where: 'terminals' } })
    const p = (await state(s)).previews['DEMO-0044']
    expect(p).toMatchObject({ mode: 'Fix failing checks', harness: 'Codex', where: 'Terminals' })
    expect(p.command).toBe("orch session start --in terminals DEMO-0044 -- codex '/orch:fix DEMO-0044'")
    expect((await state(s)).previews['DEMO-0048'].mode).toBe('Work on ticket')
    s.store.setViewer('p_mara')
    expect((await state(s)).previews['DEMO-0044'].mode).toBe('Work on ticket')
  })
})

describe('starting a run', () => {
  it('without core\'s confirmation the action is refused', async () => {
    const s = setup()
    expect(await fail(run(s, 'start', { ticket: 'DEMO-0044' }))).toBe('409 confirm.required')
    expect(await fail(run(s, 'start', { ticket: 'DEMO-0044', confirmed: true }))).toBe('409 confirm.required') // no core-validated launch
    expect(s.store.wsEventsOf(s.ws).some((e) => e.type === 'agent.started')).toBe(false)
  })
  it('plays the run live: claim, T1 started, a log line, T1 done with a receipt, then a blocking question to the viewer', async () => {
    const s = setup()
    const res = await start(s)
    expect(res.message).toMatch(/^Started Claude Code on DEMO-0044/)
    const session = sessionOn(s, 'DEMO-0044')
    expect(session).toMatch(/^s_[0-9a-f]{8}$/) // 8 hex: no clash with seeded ids such as s_9e3f
    expect(s.store.ticket('DEMO-0044')!.claim).toBeNull() // nothing happens before the clock moves

    vi.advanceTimersByTime(1500)
    const claimed = s.store.ticket('DEMO-0044')!
    expect(claimed.claim).toMatchObject({ agent: 'claude-code', session, for: 'p_sev', grant: 'gr_01J9Z8' })
    expect(claimed.status).toBe('in-progress')
    expect((await s.api.getToday(s.ws)).working.map((t) => t.key)).toContain('DEMO-0044')

    vi.advanceTimersByTime(2000)
    expect(s.store.ticket('DEMO-0044')!.tasks_state[0]).toMatchObject({ id: 'T1', state: 'doing', lease: { session } })
    vi.advanceTimersByTime(3000)
    expect(s.store.eventsOf('DEMO-0044').at(-1)).toMatchObject({ type: 'log.added', actor: { kind: 'agent', session } })
    vi.advanceTimersByTime(4000)
    expect(s.store.ticket('DEMO-0044')!.tasks_state[0]).toMatchObject({ state: 'done', receipt: { exit: 0 } })
    vi.advanceTimersByTime(2000)
    const q = s.store.ticket('DEMO-0044')!.questions_state.at(-1)!
    expect(q).toMatchObject({ to: 'p_sev', blocking: true, state: 'open' })
    const today = await s.api.getToday(s.ws)
    expect(today.needs_you.find((i) => i.ticket === 'DEMO-0044' && i.kind === 'question')).toMatchObject({ ref: q.id, blocking: true })

    // ...and then it waits: nothing more is played.
    const n = s.store.eventsOf('DEMO-0044').length
    vi.advanceTimersByTime(60_000)
    expect(s.store.eventsOf('DEMO-0044').length).toBe(n)
    expect(s.store.sim.running()).not.toContain(session)
  })
  it('the session shows on Agents (waiting on the viewer once it asked) and its activity is the agent\'s', async () => {
    const s = setup()
    await start(s)
    const session = sessionOn(s, 'DEMO-0044')
    vi.advanceTimersByTime(1500)
    const working = (await s.api.getAgents(s.ws)).find((a) => a.session === session)!
    expect(working).toMatchObject({ id: 'claude-code', for: 'p_sev', state: 'working', harness: 'claude-code', grant: { id: 'gr_01J9Z8' }, claims: [{ ticket: 'DEMO-0044' }] })
    vi.advanceTimersByTime(20_000)
    const waiting = (await s.api.getAgents(s.ws)).find((a) => a.session === session)!
    expect(waiting).toMatchObject({ state: 'waiting', waiting_on: { kind: 'question', ticket: 'DEMO-0044' } })
    const acts = (await s.api.getAgentActivity(s.ws)).filter((a) => a.session === session).map((a) => a.summary)
    expect(acts).toEqual(expect.arrayContaining(['took the claim', 'started T1', 'finished T1']))
  })
  it('Activity shows the run as the agent\'s rows on the ticket, and the start in the workspace log', async () => {
    const s = setup()
    installAndGrant(s.store, s.ws, 'activity')
    await start(s)
    vi.advanceTimersByTime(15_000)
    const tl = ((await s.api.getAddonState(s.ws, 'activity')) as unknown as { timeline: { actor: string; ticket?: string; summary: string; title: string; subtitle: string }[] }).timeline
    expect(tl.some((r) => r.actor === 'claude-code' && r.ticket === 'DEMO-0044')).toBe(true)
    // The start is in the workspace log (collapsed with the activity install just before it, so it is the latest line).
    expect(tl.some((r) => r.ticket === undefined && /started an agent session \(Claude Code\)/.test(`${r.summary} ${r.subtitle}`))).toBe(true)
  })
  it('registers the session on the viewer\'s grant', async () => {
    const s = setup()
    await start(s)
    const session = sessionOn(s, 'DEMO-0044')
    expect(s.store.grants(s.ws).find((g) => g.id === 'gr_01J9Z8')!.sessions).toContain(session)
  })
  it('a ticket that is already claimed is refused', async () => {
    const s = setup()
    expect(await fail(start(s, 'DEMO-0043'))).toBe('409 claim.held')
  })
  it('runs in the ticket\'s own workspace only, and only on tickets the viewer can see', async () => {
    const s = setup('p_tom')
    expect(await fail(start(s))).toBe('404 not_found') // Tom is not on DEMO-0044's list
    const mara = setup('p_mara')
    ;(mara.store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get('DEMO-0044')!.visibility = { restricted: ['p_sev'] }
    expect(await fail(start(mara))).toBe('404 not_found')
    const sev = setup()
    const other = sev.store.workspaces.find((w) => w.prefix === 'INT')!.id
    expect(await fail(sev.api.runAddonAction(other, 'start-agent', 'start', { ticket: 'DEMO-0044', confirmed: true, launch: LAUNCH }))).toMatch(/^(409 addon.inactive|409 ticket.other_workspace)$/)
    expect(await fail(run(sev, 'start', { confirmed: true, launch: LAUNCH }))).toBe('400 validation')
  })
  it('a viewer cannot start (member-level)', async () => {
    const s = setup('p_tom')
    expect(await fail(start(s, 'DEMO-0048'))).toBe('403 forbidden')
  })
  it('agents never start agents: core refuses an agent actor', () => {
    const { store, ws } = setup()
    const res = store.startSession(ws, { addon: 'start-agent', ticket: 'DEMO-0048', mode: 'work', harness: 'claude-code', where: 'background' }, { kind: 'agent', id: 'claude-code', session: 's_77c2', for: 'p_sev' })
    expect(res).toMatchObject({ ok: false, status: 403, code: 'human_only' })
  })
  it('an addon without a spawn_agent grant cannot start a session', () => {
    const { store, ws } = setup()
    const res = store.startSession(ws, { addon: 'wiki', ticket: 'DEMO-0048', mode: 'work', harness: 'claude-code', where: 'background' }, { kind: 'person', id: 'p_sev' })
    expect(res).toMatchObject({ ok: false, status: 403, code: 'capability.missing' })
  })
})

describe('grants', () => {
  it('with no active grant the start is refused; a grant the person signs makes it work, and the session lands on that grant', async () => {
    const s = setup()
    await s.api.revokeGrant(s.ws, 'gr_01J9Z8')
    expect(await fail(start(s))).toBe('409 grant.none')
    const g = await s.api.issueGrant(s.ws, { hours: 8, scope: 'all' })
    expect(s.store.wsEventsOf(s.ws).at(-1)).toMatchObject({ type: 'grant.issued', presence: 'touchid', actor: { kind: 'person', id: 'p_sev' } })
    await start(s)
    const session = sessionOn(s, 'DEMO-0044')
    expect(s.store.grants(s.ws).find((x) => x.id === g.id)!.sessions).toEqual([session])
  })
  it('revoking the grant stops the run and releases its claim and lease', async () => {
    const s = setup()
    await start(s)
    const session = sessionOn(s, 'DEMO-0044')
    vi.advanceTimersByTime(3500) // claim + T1 lease
    expect(s.store.ticket('DEMO-0044')!.tasks_state[0].lease?.session).toBe(session)
    await s.api.revokeGrant(s.ws, 'gr_01J9Z8')
    const doc = s.store.ticket('DEMO-0044')!
    expect(doc.claim).toBeNull()
    expect(doc.tasks_state[0]).toMatchObject({ state: 'todo', lease: null })
    expect(s.store.sim.running()).not.toContain(session)
    const n = s.store.eventsOf('DEMO-0044').length
    vi.advanceTimersByTime(60_000)
    expect(s.store.eventsOf('DEMO-0044').length).toBe(n) // the script no longer runs
    expect((await s.api.getAgents(s.ws)).find((a) => a.session === session)!.state).toBe('stopped')
  })
})

describe('stop', () => {
  it('ends the script and releases the claim and lease', async () => {
    const s = setup()
    await start(s)
    const session = sessionOn(s, 'DEMO-0044')
    vi.advanceTimersByTime(3500)
    const res = await run(s, 'stop', { ticket: 'DEMO-0044' })
    expect(res.message).toBe(`Stopped ${session} on DEMO-0044.`)
    const doc = s.store.ticket('DEMO-0044')!
    expect(doc.claim).toBeNull()
    expect(doc.tasks_state[0].lease).toBeNull()
    const n = s.store.eventsOf('DEMO-0044').length
    vi.advanceTimersByTime(60_000)
    expect(s.store.eventsOf('DEMO-0044').length).toBe(n)
    expect((await s.api.getAgents(s.ws)).find((a) => a.session === session)!.state).toBe('stopped')
    expect((await state(s)).byTicket['DEMO-0044'].running).toBe(false)
  })
  it('stop by session id from the runs table works too; a viewer cannot stop', async () => {
    const s = setup()
    await start(s)
    const session = sessionOn(s, 'DEMO-0044')
    s.store.setViewer('p_tom')
    expect(await fail(run(s, 'stop', { session }))).toBe('403 forbidden')
    s.store.setViewer('p_sev')
    expect((await run(s, 'stop', { session })).message).toBe(`Stopped ${session} on DEMO-0044.`)
  })
  it('the runs list and per-ticket map show only tickets the viewer can see', async () => {
    const s = setup()
    await start(s)
    expect((await state(s)).runs.map((r) => r.ticket)).toEqual(['DEMO-0044'])
    expect((await state(s)).byTicket['DEMO-0044'].running).toBe(true)
    s.store.setViewer('p_tom')
    const json = JSON.stringify(await state(s))
    expect(json).not.toContain('DEMO-0044')
    expect(json).not.toContain(s.store.ticket('DEMO-0044')!.title)
  })
})

describe('the workspace log reads plainly', () => {
  it('agent.started and agent.stopped have one-line summaries that name no ticket or session', () => {
    expect(describeEvent({ type: 'agent.started', agent: 'claude-code', session: 's_1234', ticket: 'DEMO-0044' })).toBe('started an agent session (Claude Code)')
    expect(describeEvent({ type: 'agent.started', agent: 'codex' })).toBe('started an agent session (Codex)')
    expect(describeEvent({ type: 'agent.started' })).toBe('started an agent session')
    expect(describeEvent({ type: 'agent.stopped', reason: 'grant revoked', session: 's_1234' })).toBe('stopped an agent session (grant revoked)')
    expect(describeEvent({ type: 'agent.stopped' })).toBe('stopped an agent session')
  })
})

describe('core computes what starts (the addon cannot spoof it)', () => {
  it('the core preview is computed from the request and the store, not from addon state', async () => {
    const s = setup()
    const st = s.store.addonState(s.ws, 'start-agent')
    st.previews = { 'DEMO-0044': { title: 'Approved by owner', mode: 'Approved by owner' } } // raw state an addon could write
    const core = await s.api.previewLaunch(s.ws, { ticket: 'DEMO-0044', mode: 'fix', harness: 'codex', where: 'terminals' })
    expect(core).toMatchObject({ ticket: 'DEMO-0044', title: 'Rotate warehouse service credentials', mode: 'Fix failing checks', harness: 'Codex', where: 'Terminals', workspace: 'Acme energy data' })
    expect(JSON.stringify(core)).not.toContain('Approved by owner')
    expect(await fail(s.api.previewLaunch(s.ws, { ticket: 'DEMO-0044', mode: 'pwn', harness: 'codex', where: 'terminals' }))).toBe('400 validation')
    s.store.setViewer('p_tom')
    expect(await fail(s.api.previewLaunch(s.ws, { ticket: 'DEMO-0044', mode: 'work', harness: 'codex', where: 'terminals' }))).toBe('404 not_found')
  })
  it('the start uses core\'s launch, not the addon\'s stored choice, and validates it again', async () => {
    const s = setup()
    await run(s, 'configure', { ticket: 'DEMO-0044', formData: { mode: 'refine', harness: 'claude-code', where: 'background' } })
    await start(s, 'DEMO-0044', { mode: 'fix', harness: 'codex', where: 'terminals' })
    expect(s.store.wsEventsOf(s.ws).find((e) => e.type === 'agent.started')).toMatchObject({ mode: 'fix', agent: 'codex', where: 'terminals' })
    expect(await fail(start(s, 'DEMO-0048', { mode: 'pwn', harness: 'codex', where: 'terminals' }))).toBe('400 validation')
  })
  it('addon-authored args cannot carry confirmed or launch (core strips them)', async () => {
    const { withoutReservedKeys } = await import('@/addon-ui/actionRuntime')
    expect(withoutReservedKeys({ confirmed: true, launch: LAUNCH, ticket: 'X', ws: 'Y', keep: 1 })).toEqual({ keep: 1 })
  })
})

describe('the launch is structured and checked by core', () => {
  it('launchSpec refuses a ticket key, place or model outside the allowed forms; the prompt is one argv element', async () => {
    const { launchSpec } = await import('@/mocks/sessions')
    const ok = { ticket: 'DEMO-0044', mode: 'work', harness: 'claude-code', where: 'background' } as const
    expect(launchSpec(ok, {}).argv.at(-1)).toBe('/orch:work DEMO-0044')
    expect(() => launchSpec({ ...ok, ticket: 'DEMO-0044; id' }, {})).toThrow()
    expect(() => launchSpec({ ...ok, where: 'x$(id)' as never }, {})).toThrow()
    expect(() => launchSpec(ok, { model: '-x' })).toThrow()
    expect(() => launchSpec(ok, { subagentModel: 'a b' })).toThrow()
  })
})

describe('review fixes: the run stops when it may no longer work', () => {
  it('a grant that expires mid-run stops the run at its next step and releases the claim and lease', async () => {
    const s = setup()
    await s.api.revokeGrant(s.ws, 'gr_01J9Z8')
    const g = await s.api.issueGrant(s.ws, { hours: 1, scope: 'all' })
    await start(s)
    const session = sessionOn(s, 'DEMO-0044')
    vi.advanceTimersByTime(3500) // claim + lease
    expect(s.store.ticket('DEMO-0044')!.claim?.session).toBe(session)
    vi.setSystemTime(Date.now() + 3600_000) // the grant's hour passes; no step has run in between
    vi.advanceTimersByTime(3000)
    const doc = s.store.ticket('DEMO-0044')!
    expect(doc.claim).toBeNull()
    expect(doc.tasks_state[0].lease).toBeNull()
    expect(s.store.eventsOf('DEMO-0044').filter((e) => e.type === 'log.added' && (e.actor as { session?: string }).session === session)).toHaveLength(0)
    expect(s.store.wsEventsOf(s.ws).at(-1)).toMatchObject({ type: 'agent.stopped', session, reason: 'grant expired' })
    expect(s.store.sim.running()).not.toContain(session)
    expect(g.id).toBeTruthy()
  })
  it('if someone else claims the ticket first, the run ends instead of working on it', async () => {
    const s = setup()
    await start(s)
    const session = sessionOn(s, 'DEMO-0044')
    s.store.append('DEMO-0044', { type: 'claim.taken', actor: 'codex:s_other:p_mara', expires: '2026-10-09T17:00:00Z' })
    vi.advanceTimersByTime(20_000)
    const doc = s.store.ticket('DEMO-0044')!
    expect(doc.claim?.session).toBe('s_other')
    expect(s.store.eventsOf('DEMO-0044').some((e) => (e.actor as { session?: string }).session === session)).toBe(false)
    expect(s.store.wsEventsOf(s.ws).at(-1)).toMatchObject({ type: 'agent.stopped', session, reason: 'claim held by another session' })
    expect(s.store.sim.running()).not.toContain(session)
  })
  it('never takes over a task another session holds: it picks the next task to do', async () => {
    const s = setup()
    s.store.append('DEMO-0044', { type: 'lease.taken', actor: 'codex:s_other:p_mara', task: 'T1' })
    await start(s)
    const session = sessionOn(s, 'DEMO-0044')
    vi.advanceTimersByTime(15_000)
    const [t1, t2] = s.store.ticket('DEMO-0044')!.tasks_state
    expect(t1).toMatchObject({ state: 'doing', lease: { session: 's_other' } })
    expect(t2.state).toBe('done')
    expect(s.store.eventsOf('DEMO-0044').filter((e) => e.type === 'task.done').map((e) => [e.task, (e.actor as { session: string }).session])).toEqual([['T2', session]])
  })
})
