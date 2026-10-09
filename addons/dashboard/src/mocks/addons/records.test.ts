import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { refused } from '@/test/refused'

const setup = (viewer = 'p_sev', restrict: string[] = []) => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  installAndGrant(store, ws, 'records')
  for (const k of restrict) (store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get(k)!.visibility = { restricted: ['p_sev'] }
  store.setViewer(viewer)
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
type S = ReturnType<typeof setup>
interface Row {
  ticket: string
  title: string
  events: number
}
interface State {
  summary: string
  pendingEvents: number
  pendingTickets: number
  rows: Row[]
  history: { hash: string; at: string; tickets: number; events: number; by: string }[]
  pushAlert: { type: string; tone?: string; title?: string }
  remote: string
  settings: { auto_commit_minutes: number; push: boolean; remote: string }
}
const state = async (s: S) => (await s.api.getAddonState(s.ws, 'records')) as unknown as State
const run = (s: S, id: string, body: Record<string, unknown> = {}) => s.api.runAddonAction(s.ws, 'records', id, body)
const alertOf = async (s: S) => (await state(s)).pushAlert

describe('records seed and derived pending changes', () => {
  it('starts in the catalog, not installed', () => {
    const store = createMockStore({ persist: false })
    expect(store.addons.some((a) => a.name === 'records')).toBe(true)
    const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!
    expect(ws.addons.records).toBeUndefined()
  })
  it('pending is derived from ticket events: a summary, one row per ticket, events summed', async () => {
    const st = await state(setup())
    expect(st.rows.length).toBeGreaterThanOrEqual(3)
    expect(st.pendingTickets).toBe(st.rows.length)
    expect(st.pendingEvents).toBe(st.rows.reduce((n, r) => n + r.events, 0))
    expect(st.summary).toBe(`${st.pendingEvents} events to record · 1 commit waiting to push`)
    expect(st.remote).toBe('git@github.com:acme-energy/energy-records.git')
    expect(st.history.length).toBeGreaterThanOrEqual(2)
    expect(st.history[0].hash).toMatch(/^[0-9a-f]{7}$/)
  })
  it('settings default: auto-commit every 30 minutes, push on, the remote', async () => {
    expect((await state(setup())).settings).toEqual({ auto_commit_minutes: 30, push: true, remote: 'git@github.com:acme-energy/energy-records.git' })
  })
})

describe('commit records', () => {
  it('empties the pending list and adds a history entry', async () => {
    const s = setup()
    const before = JSON.parse(JSON.stringify(await state(s))) as State
    const r = await run(s, 'commit')
    expect(r.changed).toBe(true)
    const after = await state(s)
    expect(after.rows).toEqual([])
    expect(after.summary).toBe('Everything is recorded · 2 commits waiting to push')
    expect(after.history).toHaveLength(before.history.length + 1)
    expect(after.history[0]).toMatchObject({ tickets: before.pendingTickets, events: before.pendingEvents, by: 'Severin' })
    expect(after.history[0].hash).toMatch(/^[0-9a-f]{7}$/)
    expect(after.history[0].hash).not.toBe(before.history[0].hash)
  })
  it('committing with nothing pending adds nothing', async () => {
    const s = setup()
    await run(s, 'commit')
    const n = (await state(s)).history.length
    expect((await run(s, 'commit')).message).toMatch(/nothing new/i)
    expect((await state(s)).history).toHaveLength(n)
  })
  it('new ticket activity makes it pending again', async () => {
    const s = setup()
    await run(s, 'commit')
    s.store.append('DEMO-0044', { type: 'comment.added', actor: 'p_sev', text: 'a note' })
    const st = await state(s)
    expect(st.rows.map((r) => r.ticket)).toEqual(['DEMO-0044'])
    expect(st.rows[0].events).toBe(1)
    expect(st.summary).toBe('1 event to record · 2 commits waiting to push')
  })
  it('a viewer cannot commit, push or pull', async () => {
    const s = setup('p_tom')
    for (const id of ['commit', 'push', 'pull']) await expect(run(s, id)).rejects.toMatchObject({ status: 403 })
  })
})

describe('push and pull', () => {
  it('push records last push; a second push without a pull is rejected; pull then push works', async () => {
    const s = setup()
    await run(s, 'commit')
    expect((await run(s, 'push')).message).toMatch(/pushed/i)
    const ok = await alertOf(s)
    expect(ok.tone).not.toBe('error')
    const st = await state(s)
    expect((st as unknown as { lastPush: { commit: string; remote: string } }).lastPush).toMatchObject({ commit: st.history[0].hash, remote: st.remote })

    const rejected = await refused(run(s, 'push'))
    expect(rejected).toMatchObject({ status: 409, code: 'records.push_rejected' })
    expect(rejected.message).toMatch(/^Push rejected: Remote rejected/)
    expect(await alertOf(s)).toMatchObject({ type: 'alert', tone: 'error', title: 'Remote rejected: non-fast-forward. Pull first.' })

    await run(s, 'pull')
    expect((await alertOf(s)).tone).not.toBe('error')
    await run(s, 'push')
    expect((await alertOf(s)).tone).not.toBe('error')
    expect(JSON.stringify(await alertOf(s))).toMatch(/pushed/i)
  })
  it('push is refused when push is off in settings', async () => {
    const s = setup()
    await run(s, 'save_settings', { formData: { auto_commit_minutes: 30, push: false, remote: 'git@github.com:acme-energy/energy-records.git' } })
    const r = await refused(run(s, 'push'))
    expect(r).toMatchObject({ status: 409, code: 'records.push_off' })
    expect(r.message).toMatch(/push is off/i)
  })
})

describe('records settings', () => {
  it('owner saves; a maintainer cannot; values are sanitised', async () => {
    const s = setup()
    await run(s, 'save_settings', { formData: { auto_commit_minutes: 15, push: true, remote: 'git@github.com:acme-energy/other.git' } })
    expect((await state(s)).settings).toEqual({ auto_commit_minutes: 15, push: true, remote: 'git@github.com:acme-energy/other.git' })
    await run(s, 'save_settings', { formData: { auto_commit_minutes: -4, push: 'yes', remote: 42 } })
    expect((await state(s)).settings).toEqual({ auto_commit_minutes: 30, push: false, remote: 'git@github.com:acme-energy/energy-records.git' })
    await expect(run(setup('p_mara'), 'save_settings', { formData: {} })).rejects.toMatchObject({ status: 403 })
  })
  it('manifest: settings form, owner-level save_settings', () => {
    const pkg = setup().store.addons.find((a) => a.name === 'records')!
    expect(pkg.actions).toMatchObject({ save_settings: { minRole: 'owner' } })
    expect(pkg.contributions.map((c) => c.slot)).toEqual(['nav', 'settings'])
  })
})

describe('records and ticket visibility', () => {
  it('a hidden ticket with pending events is not in the outside member count, rows or text', async () => {
    const all = await state(setup('p_sev'))
    const hiddenKey = all.rows[0].ticket
    const hiddenTitle = all.rows[0].title
    const outside = await state(setup('p_mara', [hiddenKey]))
    expect(outside.rows.some((r) => r.ticket === hiddenKey)).toBe(false)
    expect(outside.pendingTickets).toBe(all.pendingTickets - 1)
    expect(outside.pendingEvents).toBe(all.pendingEvents - all.rows[0].events)
    const json = JSON.stringify(outside)
    expect(json).not.toContain(hiddenKey)
    expect(json).not.toContain(hiddenTitle)
    expect(JSON.stringify(await state(setup('p_sev', [hiddenKey])))).toContain(hiddenKey)
  })

  it('a commit by an outside member shows them only their visible counts; the owner sees the full entry', async () => {
    const all = await state(setup('p_sev'))
    const hidden = all.rows[0] // first pending ticket is the one restricted below
    const s = setup('p_mara', [hidden.ticket])
    const mine = await state(s)
    const r = await run(s, 'commit')
    expect(r.message).toContain(`${mine.pendingEvents} events on ${mine.pendingTickets} tickets`)
    expect(r.message).not.toContain(String(all.pendingEvents))
    const seen = (await state(s)).history[0]
    expect(seen).toMatchObject({ tickets: mine.pendingTickets, events: mine.pendingEvents, by: 'Mara' })
    expect(JSON.stringify(await state(s))).not.toContain(hidden.ticket)
    s.store.setViewer('p_sev')
    expect((await state(s)).history[0]).toMatchObject({ hash: seen.hash, tickets: all.pendingTickets, events: all.pendingEvents })
  })
  it('when only hidden tickets are pending, an outside member commit is a no-op', async () => {
    const s = setup('p_sev')
    await run(s, 'commit')
    s.store.append('DEMO-0041', { type: 'comment.added', actor: 'p_sev', text: 'secret' })
    ;(s.store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get('DEMO-0041')!.visibility = { restricted: ['p_sev'] }
    s.store.setViewer('p_mara')
    const n = (await state(s)).history.length
    expect((await run(s, 'commit')).message).toBe('Already recorded: nothing new since the last commit.')
    expect((await state(s)).history).toHaveLength(n)
    s.store.setViewer('p_sev')
    expect((await state(s)).pendingEvents).toBe(1)
  })
})
