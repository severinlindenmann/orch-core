import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { refused } from '@/test/refused'
import type { ShellCtx } from '@/app/terminal/fakePty'

const setup = (viewer?: string) => {
  const store = createMockStore({ persist: false })
  if (viewer) store.setViewer(viewer)
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws }
}
type S = ReturnType<typeof setup>
interface Sess {
  id: string
  label: string
  kind: 'person' | 'agent'
  owner: string
  ticket: string | null
  status: 'running' | 'stopped'
  interactive: boolean
  ctx: ShellCtx
  transcript: string[]
}
type State = { sessions: Sess[]; current: { id: string }; sessionByTicket: Record<string, string>; settings: { shell: string; font_size: number }; items: { title: string; badge?: string; actions?: { action: string; label: string }[] }[] }
const state = async (s: S) => (await s.api.getAddonState(s.ws, 'terminals')) as unknown as State
const run = (s: S, id: string, body: Record<string, unknown> = {}) => s.api.runAddonAction(s.ws, 'terminals', id, body)
const status = (p: Promise<unknown>) => p.then(() => 200, (e: { status: number }) => e.status)
/** The same store seen as another person. */
const as = (s: S, viewer: string): S => {
  s.store.setViewer(viewer)
  return s
}

describe('agent transcript', () => {
  it('is built from the session ticket, not hard-coded', async () => {
    const s = setup('p_sev')
    const a = (await state(s)).sessions.find((x) => x.kind === 'agent')!
    expect(a.transcript).toContain(`orch approve ${a.ticket} plan`)
    const sessions = s.store.addonState(s.ws, 'terminals').sessions as { id: string; ticket: string | null }[]
    sessions.find((x) => x.id === a.id)!.ticket = 'DEMO-0044'
    const b = (await state(s)).sessions.find((x) => x.id === a.id)!
    expect(b.transcript).toContain('orch approve DEMO-0044 plan')
    expect(b.transcript.join('\n')).not.toContain('DEMO-0043')
  })
  it('a mirror without a ticket has no approve line', async () => {
    const s = setup('p_sev')
    const sessions = s.store.addonState(s.ws, 'terminals').sessions as { kind: string; ticket: string | null }[]
    sessions.find((x) => x.kind === 'agent')!.ticket = null
    expect((await state(s)).sessions.find((x) => x.kind === 'agent')!.transcript.some((c) => c.startsWith('orch approve'))).toBe(false)
  })
})

describe('terminals state', () => {
  it('seeds three sessions: Severin shell in DEMO-0043, an agent mirror and a stopped one', async () => {
    const s = setup('p_sev')
    const st = await state(s)
    expect(st.sessions).toHaveLength(3)
    const [shell, mirror, stopped] = st.sessions
    expect(shell).toMatchObject({ kind: 'person', owner: 'p_sev', ticket: 'DEMO-0043', status: 'running', interactive: true })
    expect(shell.ctx.branch).toBe('feat/billing-join')
    expect(mirror).toMatchObject({ kind: 'agent', interactive: false, label: 'DEMO-0043 · Claude Code' })
    expect(mirror.transcript.some((c) => c.startsWith('orch approve'))).toBe(true)
    expect(stopped).toMatchObject({ status: 'stopped', interactive: false })
    expect(st.settings).toEqual({ shell: '/bin/zsh', font_size: 13 })
  })
  it('live data: the shell context carries the viewer grant, the ticket claim and the workspace cursor', async () => {
    const s = setup('p_sev')
    const ctx = (await state(s)).sessions[0].ctx
    expect(ctx.grant).toMatchObject({ id: 'gr_01J9Z8', scope: 'all', until: '2026-10-09T18:00:00Z' })
    expect(ctx.cursor).toBe(s.store.cursor(s.ws))
    expect(ctx.ticket?.key).toBe('DEMO-0043')
    expect(ctx.ticket?.title).toBeTruthy()
    const before = ctx.cursor
    await run(s, 'new')
    expect((await state(s)).sessions[0].ctx.cursor).toBe(before + 1)
  })
  it('a person sees other people\'s agent mirrors but not their shells; nobody else gets interactive access', async () => {
    const s = as(setup(), 'p_mara')
    const st = await state(s)
    expect(st.sessions.map((x) => x.kind)).toEqual(['agent'])
    expect(st.sessions.every((x) => !x.interactive)).toBe(true)
  })
  it('a viewer owning a session still gets it view-only', async () => {
    const s = setup('p_tom')
    s.store.addonState(s.ws, 'terminals').sessions = [
      { id: 'tom1', label: 'Tom shell', kind: 'person', owner: 'p_tom', ticket: null, branch: 'main', status: 'running', started: '2026-10-09T10:00:00Z' },
    ]
    const st = await state(s)
    expect(st.sessions).toHaveLength(1)
    expect(st.sessions[0].interactive).toBe(false)
  })
})

describe('terminals navigation (per viewer)', () => {
  it('open selects a session for this viewer only', async () => {
    const s = setup('p_sev')
    await run(s, 'open', { session: 'agent1' })
    expect((await state(s)).current.id).toBe('agent1')
    expect((await state(as(s, 'p_mara'))).current.id).toBe('agent1') // the only one Mara can see
    expect((await state(as(s, 'p_sev'))).current.id).toBe('agent1')
    await run(s, 'open', { session: 'shell1' })
    expect((await state(s)).current.id).toBe('shell1')
    expect((await state(as(s, 'p_mara'))).current.id).toBe('agent1')
  })
  it('open ignores unknown sessions and other people\'s shells', async () => {
    const s = setup('p_sev')
    expect(await refused(run(s, 'open', { session: 'nope' }))).toMatchObject({ status: 404 })
    await run(s, 'open', { session: 'agent1' })
    const m = as(s, 'p_mara')
    const r = await refused(run(m, 'open', { session: 'shell1' }))
    expect(r).toMatchObject({ status: 404, code: 'not_found' })
    expect(r.message).toMatch(/not found|no such/i)
    expect((await state(m)).current.id).toBe('agent1')
  })
  it('viewers may open (minRole viewer); new, close and open_ticket need member', async () => {
    const s = setup('p_tom')
    expect(await status(run(s, 'open', { session: 'agent1' }))).toBe(200)
    expect(await status(run(s, 'new'))).toBe(403)
    expect(await status(run(s, 'close', { session: 'shell1' }))).toBe(403)
    expect(await status(run(s, 'open_ticket', { ticket: 'DEMO-0043' }))).toBe(403)
  })
})

describe('terminals actions', () => {
  it('new creates a running shell owned by the viewer and opens it', async () => {
    const s = as(setup(), 'p_mara')
    const r = await run(s, 'new')
    expect(r.changed).toBe(true)
    const st = await state(s)
    const mine = st.sessions.filter((x) => x.owner === 'p_mara')
    expect(mine).toHaveLength(1)
    expect(mine[0]).toMatchObject({ status: 'running', interactive: true })
    expect(st.current.id).toBe(mine[0].id)
  })
  it('close stops your own running shell; agent mirrors and other people\'s shells cannot be closed', async () => {
    const s = setup('p_sev')
    await run(s, 'close', { session: 'shell1' })
    expect((await state(s)).sessions.find((x) => x.id === 'shell1')).toMatchObject({ status: 'stopped', interactive: false })
    const r = await refused(run(s, 'close', { session: 'agent1' }))
    expect(r).toMatchObject({ status: 403, code: 'forbidden' })
    expect(r.message).toMatch(/only/i)
    expect((await state(s)).sessions.find((x) => x.id === 'agent1')!.status).toBe('running')
    const m = as(s, 'p_mara')
    await run(m, 'open', { session: 'agent1' })
    expect((await refused(run(m, 'close', { session: 'shell1' }))).message).toMatch(/not found|no such/i)
  })
  it('open_ticket reuses your running shell for that ticket, else creates one, and exposes it by ticket', async () => {
    const s = setup('p_sev')
    await run(s, 'open_ticket', { ticket: 'DEMO-0043' })
    let st = await state(s)
    expect(st.sessions).toHaveLength(3) // reused shell1
    expect(st.sessionByTicket['DEMO-0043']).toBe('shell1')
    expect(st.sessionByTicket['DEMO-0041']).toBeUndefined()
    await run(s, 'open_ticket', { ticket: 'DEMO-0041' })
    st = await state(s)
    expect(st.sessions).toHaveLength(4)
    const id = st.sessionByTicket['DEMO-0041']
    expect(st.sessions.find((x) => x.id === id)).toMatchObject({ ticket: 'DEMO-0041', owner: 'p_sev', interactive: true })
    expect(st.current.id).toBe(id)
  })
  it('save_settings keeps shell and font size', async () => {
    const s = setup('p_sev')
    await run(s, 'save_settings', { formData: { shell: '/bin/bash', font_size: 15 } })
    expect((await state(s)).settings).toEqual({ shell: '/bin/bash', font_size: 15 })
  })
})

describe('terminals are scoped to their workspace', () => {
  it('CLI (terminals installed and granted) starts with no sessions and carries no DEMO ticket data', async () => {
    const s = setup('p_sev')
    const cli = s.store.workspaces.find((w) => w.prefix === 'CLI')!.id
    const st = (await s.api.getAddonState(cli, 'terminals')) as unknown as State
    expect(st.sessions).toEqual([])
    expect(st.current.id).toBe('none')
    const json = JSON.stringify(st)
    expect(json).not.toMatch(/DEMO-|Tariff|claude-code|billing-join/)
  })
  it('a new shell in CLI resolves no DEMO ticket even when asked for one by key', async () => {
    const s = setup('p_sev')
    const cli = s.store.workspaces.find((w) => w.prefix === 'CLI')!.id
    await s.api.runAddonAction(cli, 'terminals', 'new', {})
    const st = (await s.api.getAddonState(cli, 'terminals')) as unknown as State
    expect(st.sessions).toHaveLength(1)
    expect(st.sessions[0].ctx.ticket).toBeNull()
    const state = s.store.addonState(cli, 'terminals') as { sessions: { ticket: string | null }[] }
    state.sessions[0].ticket = 'DEMO-0043' // a tampered/stale session pointing across workspaces
    const again = (await s.api.getAddonState(cli, 'terminals')) as unknown as State
    expect(again.sessions).toHaveLength(0) // a session naming a ticket of another workspace is not shown at all
    expect(JSON.stringify(again)).not.toMatch(/Tariff|DEMO-0043/)
  })
  it('a ticket the viewer may not see is not resolved into the shell context', async () => {
    const s = setup('p_sev')
    s.store.isVisible = (key: string) => key !== 'DEMO-0043'
    expect((await state(s)).sessions[0].ctx.ticket).toBeNull()
  })
  it('the ctx carries move, gates, questions and tasks for the status command', async () => {
    const t = (await state(setup('p_sev'))).sessions[0].ctx.ticket!
    expect(t.move.who).toBeTruthy()
    expect(t.gates.map((g) => g.name)).toEqual(['requirements', 'plan', 'verify'])
    expect(t.tasks.total).toBeGreaterThan(0)
    expect(t.questions.total).toBeGreaterThanOrEqual(t.questions.open)
  })
})

describe('busy session times', () => {
  it('pads hours to exactly two digits', async () => {
    const s = setup('p_sev')
    s.store.reset('busy')
    const st = await state(s)
    expect(st.sessions.length).toBeGreaterThan(3)
    expect(JSON.stringify(st)).not.toMatch(/\b0\d{2}:/)
    const sessions = s.store.addonState(s.ws, 'terminals').sessions as { started: string }[]
    for (const session of sessions) expect(session.started).toMatch(/T\d{2}:\d{2}:\d{2}Z$/)
  })
})
