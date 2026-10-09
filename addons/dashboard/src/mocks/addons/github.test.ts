import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { refused } from '@/test/refused'

const setup = () => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws }
}
type Check = { name: string; status: 'pass' | 'fail' | 'pending' }
type Pr = { id: string; repo: string; number: number; title: string; ticket: string; checks: Check[]; review: string; author: { kind: string; name: string }; additions: number; deletions: number; state: string }
type Issue = { id: string; repo: string; number: number; title: string; label: string }
type State = {
  prs: Pr[]
  issues: Issue[]
  settings: Record<string, unknown>
  prByTicket: Record<string, { number: number; checks: string; review: string; url: string; checkPairs: { label: string; value: string }[] }>
  issueItems: { title: string; subtitle: string; actions: { action: string; args: Record<string, string> }[] }[]
  prRows: { id: string; checks: string; review: string }[]
}
const state = async (s: ReturnType<typeof setup>) => (await s.api.getAddonState(s.ws, 'github')) as unknown as State
const prOf = (st: State, id: string) => st.prs.find((p) => p.id === id)!

describe('github times', () => {
  it('a PR updated this minute reads "just now", not "0 min ago"', async () => {
    const s = setup()
    await state(s) // seeds
    const prs = (s.store.addonState(s.ws, 'github') as unknown as { prs: { id: string; updated_at: string }[] }).prs
    prs[0].updated_at = s.store.now()
    const rows = ((await s.api.getAddonState(s.ws, 'github')) as unknown as { prRows: { id: string; updated: string }[] }).prRows
    expect(rows.find((r) => r.id === prs[0].id)?.updated).toBe('just now')
    expect(rows.some((r) => /^0 min/.test(r.updated))).toBe(false)
  })
})

describe('github seed', () => {
  it('has six pull requests across two acme-energy repos with checks, reviews, authors and diff stats', async () => {
    const st = await state(setup())
    expect(st.prs).toHaveLength(6)
    expect(new Set(st.prs.map((p) => p.repo))).toEqual(new Set(['acme-energy/energy-dbt', 'acme-energy/billing-api']))
    expect(new Set(st.prs.flatMap((p) => p.checks.map((c) => c.status)))).toEqual(new Set(['pass', 'fail', 'pending']))
    expect(new Set(st.prs.map((p) => p.author.kind))).toEqual(new Set(['agent', 'person']))
    expect(st.prs.every((p) => p.ticket && p.additions >= 0 && p.deletions >= 0 && p.review)).toBe(true)
  })
  it('has eight open issues and settings', async () => {
    const st = await state(setup())
    expect(st.issues).toHaveLength(8)
    expect(st.issueItems).toHaveLength(8)
    expect(st.settings).toMatchObject({ org: 'acme-energy', link_prs: true })
    expect(st.settings.repos).toBeTruthy()
    expect(st.settings.poll_minutes).toBeTruthy()
  })
  it('exposes a per-ticket PR map and no PR for a ticket without one', async () => {
    const st = await state(setup())
    expect(st.prByTicket['DEMO-0041']).toMatchObject({ number: 29, checks: 'pass' })
    expect(st.prByTicket['DEMO-0041'].url).toMatch(/^https:\/\/github\.com\/acme-energy\//)
    expect(st.prByTicket['DEMO-0041'].checkPairs.length).toBeGreaterThan(1)
    expect(st.prByTicket['DEMO-0045']).toBeUndefined()
  })
})

describe('github refresh', () => {
  it('flips the pending checks of the ticket PR to pass', async () => {
    const s = setup()
    expect(prOf(await state(s), 'acme-energy/energy-dbt#31').checks.some((c) => c.status === 'pending')).toBe(true)
    const r = await s.api.runAddonAction(s.ws, 'github', 'refresh', { ticket: 'DEMO-0043' })
    expect(r.changed).toBe(true)
    const st = await state(s)
    expect(prOf(st, 'acme-energy/energy-dbt#31').checks.every((c) => c.status === 'pass')).toBe(true)
    expect(st.prByTicket['DEMO-0043'].checks).toBe('pass')
  })
  it('without a ticket it refreshes the first PR that has pending checks, and only that one', async () => {
    const s = setup()
    const before = (await state(s)).prs.filter((p) => p.checks.some((c) => c.status === 'pending')).length
    await s.api.runAddonAction(s.ws, 'github', 'refresh', {})
    const after = (await state(s)).prs.filter((p) => p.checks.some((c) => c.status === 'pending')).length
    expect(after).toBe(before - 1)
  })
  it('says so and changes nothing when no check is pending', async () => {
    const s = setup()
    const r = await s.api.runAddonAction(s.ws, 'github', 'refresh', { ticket: 'DEMO-0041' })
    expect(r.changed).toBeFalsy()
    expect(r.message).toMatch(/no pending checks/i)
  })
})

describe('github approve and open', () => {
  it('approve marks the review approved', async () => {
    const s = setup()
    const r = await s.api.runAddonAction(s.ws, 'github', 'approve', { id: 'acme-energy/energy-dbt#29' })
    expect(r.changed).toBe(true)
    const st = await state(s)
    expect(prOf(st, 'acme-energy/energy-dbt#29').review).toBe('approved')
    expect(st.prRows.find((x) => x.id === 'acme-energy/energy-dbt#29')!.review).toBe('approved')
  })
  it('approve refuses a merged PR and an unknown one', async () => {
    const s = setup()
    expect(await refused(s.api.runAddonAction(s.ws, 'github', 'approve', { id: 'acme-energy/energy-dbt#27' }))).toMatchObject({ status: 409, code: 'github.merged' })
    expect(await refused(s.api.runAddonAction(s.ws, 'github', 'approve', { id: 'nope' }))).toMatchObject({ status: 404, code: 'not_found' })
  })
  it('open returns the github.com url and changes nothing', async () => {
    const s = setup()
    const r = await s.api.runAddonAction(s.ws, 'github', 'open', { id: 'acme-energy/energy-dbt#29' })
    expect(r.url).toBe('https://github.com/acme-energy/energy-dbt/pull/29')
    expect(r.changed).toBeFalsy()
  })
})

describe('github import (list item action)', () => {
  it('creates a backlog ticket linked to the issue and removes the issue from the lane', async () => {
    const s = setup()
    const st0 = await state(s)
    const item = st0.issueItems[0]
    expect(item.actions[0].action).toBe('import')
    const issue = st0.issues[0]
    const r = await s.api.runAddonAction(s.ws, 'github', 'import', item.actions[0].args)
    expect(r).toMatchObject({ ok: true, changed: true })
    const key = /as (DEMO-\d+)/.exec(r.message)![1]
    const t = await s.api.getTicket(key)
    expect(t).toMatchObject({ status: 'backlog', title: issue.title })
    expect(t.links.external[0]).toMatchObject({ label: `GH-${issue.number}`, url: `https://github.com/${issue.repo}/issues/${issue.number}` })
    const st = await state(s)
    expect(st.issues).toHaveLength(7)
    expect(st.issueItems).toHaveLength(7)
    expect(st.issues.some((i) => i.id === issue.id)).toBe(false)
  })
  it('importing an issue that is gone does nothing', async () => {
    const s = setup()
    expect(await refused(s.api.runAddonAction(s.ws, 'github', 'import', { id: 'nope' }))).toMatchObject({ status: 404 })
  })
})
