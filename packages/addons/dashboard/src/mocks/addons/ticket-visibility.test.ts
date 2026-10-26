import { describe, expect, it } from 'vitest'
import { addonActive } from '@/api/addons'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { refused as refusal } from '@/test/refused'

// Security sweep: an addon's state and actions must not reveal a ticket the viewer cannot see.
// DEMO-0041 and DEMO-0043 are restricted to Severin; Mara (a maintainer) is outside the list.
const HIDDEN = ['DEMO-0041', 'DEMO-0043']
const ADDONS = ['publish', 'usage', 'wiki', 'estimate', 'terminals', 'worktrees', 'quick', 'records', 'activity', 'start-agent', 'factory', 'schedules', 'drop', 'land']

const setup = (viewer: string) => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  for (const name of ADDONS) if (!addonActive(store.workspaces.find((w) => w.id === ws), name)) installAndGrant(store, ws, name)
  for (const k of HIDDEN) (store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get(k)!.visibility = { restricted: ['p_sev'] }
  store.setViewer(viewer)
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
type S = ReturnType<typeof setup>
const stateOf = async (s: S, name: string) => (await s.api.getAddonState(s.ws, name)) as Record<string, unknown>
// Titles and branch names would name a hidden ticket as surely as its key does: the restricted tickets' own titles,
// the branches of their worktrees and sessions, and the titles of their pull requests.
const hiddenText = (s: S) => [
  ...HIDDEN.map((k) => s.store.ticket(k)!.title),
  'feat/billing-join',
  'feat/load-tariff-tables-as-dbt-seeds',
  'feat/add-billing-reconciliation-tests',
  'feat/DEMO-0043-tariff-seeds',
  'feat/DEMO-0041-reconciliation',
  'Load tariff tables as dbt seeds',
  'Add billing reconciliation tests',
]
const run = (s: S, name: string, id: string, body: Record<string, unknown> = {}) => s.api.runAddonAction(s.ws, name, id, body)

describe('addon state hides tickets the viewer cannot see', () => {
  for (const name of ADDONS) {
    it(`${name}: no data for a restricted ticket reaches an outside member`, async () => {
      const s = setup('p_mara')
      const json = JSON.stringify(await stateOf(s, name))
      for (const k of HIDDEN) expect(json).not.toContain(k)
    })
    it(`${name}: no title or branch of a restricted ticket reaches an outside member`, async () => {
      const s = setup('p_mara')
      const json = JSON.stringify(await stateOf(s, name))
      for (const text of hiddenText(s)) expect(json, text).not.toContain(text)
    })
  }
  it('the listed person still sees their data (publish, usage, wiki, terminals)', async () => {
    const s = setup('p_sev')
    for (const name of ['publish', 'usage', 'wiki', 'terminals']) {
      const json = JSON.stringify(await stateOf(s, name))
      expect(HIDDEN.some((k) => json.includes(k)), name).toBe(true)
    }
  })
  it('aggregates that name no ticket stay (usage totals, publish workspace shares)', async () => {
    const s = setup('p_mara')
    expect((await stateOf(s, 'usage')).weekCents).toBe(3140)
    expect(JSON.stringify((await stateOf(s, 'publish')).shareRows)).toContain('Energy data portal')
  })
  it('a hidden ticket drops out of the publish counts too', async () => {
    const hidden = (await stateOf(setup('p_mara'), 'publish')).liveShares as number
    const all = (await stateOf(setup('p_sev'), 'publish')).liveShares as number
    expect(hidden).toBeLessThan(all)
  })
  it('a ticket of another workspace is never shown (no DEMO data in INT)', async () => {
    const store = createMockStore({ persist: false })
    const ow = store.workspaces.find((w) => w.prefix !== 'DEMO')!
    store.setViewer(ow.members[0].person)
    const api = createApi(createMockTransport(store, { latency: false }))
    for (const n of ['publish', 'usage', 'wiki']) {
      if (!addonActive(ow, n)) installAndGrant(store, ow.id, n)
      expect(JSON.stringify(await api.getAddonState(ow.id, n)), n).not.toMatch(/DEMO-/)
    }
  })
})

describe('ticket-scoped actions on a hidden ticket are refused', () => {
  const refused = (p: Promise<unknown>) => expect(p).rejects.toMatchObject({ status: 404 })
  it('publish share, share_once', async () => {
    const s = setup('p_mara')
    await refused(run(s, 'publish', 'share', { ticket: 'DEMO-0041' }))
    await refused(run(s, 'publish', 'share_once', { confirmed: true, ticket: 'DEMO-0041' }))
  })
  it('wiki link, terminals open_ticket, estimate set', async () => {
    const s = setup('p_mara')
    await refused(run(s, 'wiki', 'link', { ticket: 'DEMO-0041', formData: { page: 'oncall-runbook' } }))
    await refused(run(s, 'terminals', 'open_ticket', { ticket: 'DEMO-0043' }))
    await refused(run(s, 'estimate', 'set', { ticket: 'DEMO-0041', formData: { points: 3 } }))
  })
  it('publish: copy_link, extend and revoke by id do nothing for a hidden ticket share', async () => {
    const s = setup('p_mara')
    const before = JSON.stringify(s.store.addonState(s.ws, 'publish').shares)
    for (const id of ['copy_link', 'extend', 'revoke']) expect(await refusal(run(s, 'publish', id, { confirmed: true, id: 'sh_report' }))).toMatchObject({ status: 404, message: 'That share no longer exists.' })
    expect(JSON.stringify(s.store.addonState(s.ws, 'publish').shares)).toBe(before)
  })
  it('publish: the decision about a hidden ticket is not on Today and cannot be decided by id', async () => {
    const s = setup('p_mara')
    expect((await s.api.getAddonDecisions(s.ws)).some((d) => d.ticket === 'DEMO-0041')).toBe(false)
    expect(await refusal(run(s, 'publish', 'decide', { id: 'dec_publish_report', confirmed: true, option: 'yes' }))).toMatchObject({ status: 409, code: 'decision.closed' })
    expect(s.store.addonState(s.ws, 'publish').shares).toHaveLength(5) // no share was created
    // Severin still sees and can decide it.
    const t = setup('p_sev')
    expect((await t.api.getAddonDecisions(t.ws)).some((d) => d.ticket === 'DEMO-0041')).toBe(true)
  })
  it('terminals: a hidden ticket session cannot be opened or closed by id', async () => {
    const s = setup('p_mara')
    expect(await refusal(run(s, 'terminals', 'open', { session: 'agent1' }))).toMatchObject({ status: 404, message: 'No such terminal session.' })
    expect(await refusal(run(s, 'terminals', 'close', { session: 'agent1' }))).toMatchObject({ status: 404, message: 'No such terminal session.' })
  })
})
