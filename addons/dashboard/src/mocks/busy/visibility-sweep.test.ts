import { describe, expect, it } from 'vitest'
import { addonActive } from '@/api/addons'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '../store'

// Busy day, every workspace, every member: nothing a person reads (every active addon's state, Today, the addon
// decisions, the agents list and agent activity, the ticket list) names a ticket they cannot see, by key or by title.
const store = createMockStore({ persist: false, dataset: 'busy' })
const api = createApi(createMockTransport(store, { latency: false }))
const ADDONS = store.addons.map((a) => a.name)
const cases = store.workspaces.flatMap((w) => w.members.map((m) => [w.prefix, m.person] as const))

describe('busy day visibility sweep: all workspaces, all members', () => {
  it('has restricted tickets to hide, so the sweep tests something', () => {
    for (const w of store.workspaces) expect(store.listTickets(w.id, 'p_sev').some((t) => t.restricted), w.prefix).toBe(true)
  })
  it.each(cases)('%s · %s: 0 leaks', async (prefix, person) => {
    const w = store.workspaces.find((x) => x.prefix === prefix)!
    store.setViewer(person)
    const all = store.ticketKeys(w.id).map((k) => store.ticket(k)!)
    const hidden = all.filter((t) => !store.isVisible(t.key, person))
    const visibleTitles = new Set(all.filter((t) => store.isVisible(t.key, person)).map((t) => t.title))
    const hiddenTitles = hidden.map((t) => t.title).filter((t) => !visibleTitles.has(t))
    const reads: [string, Promise<unknown>][] = [
      ['today', api.getToday(w.id)],
      ['decisions', api.getAddonDecisions(w.id)],
      ['agents', api.getAgents(w.id)],
      ['agent activity', api.getAgentActivity(w.id)],
      ['tickets', api.listTickets(w.id)],
      ...ADDONS.filter((n) => addonActive(w, n)).map((n): [string, Promise<unknown>] => [n, api.getAddonState(w.id, n)]),
    ]
    const leaks: string[] = []
    for (const [what, p] of reads) {
      const json = JSON.stringify(await p)
      for (const t of hidden) if (json.includes(t.key)) leaks.push(`${what}: ${t.key}`)
      for (const title of hiddenTitles) if (json.includes(title)) leaks.push(`${what}: "${title}"`)
    }
    expect(leaks).toEqual([])
  })
})
