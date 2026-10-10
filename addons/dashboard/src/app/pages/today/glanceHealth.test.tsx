import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { parseNode } from '@/addon-ui/nodes'
import { selectContributions } from '@/addon-ui/slots'
import { STORAGE_KEY } from '@/mocks/persist'
import { createMockStore, type MockStore } from '@/mocks/store'
import type { Workspace } from '@/api/types'

const T = { timeout: 8000 }

/** Every Today glance item of every workspace, resolved against the state the store serves, as core parses it. */
function glanceProblems(s: MockStore): string[] {
  const out: string[] = []
  for (const w of s.workspaceList() as Workspace[]) {
    for (const a of s.addons) {
      const addon = s.addonStateView(w.id, a.name) ?? undefined
      for (const c of selectContributions([a], 'today.card', { workspace: w, addon })) {
        const p = parseNode(c.node)
        if (!p.ok) out.push(`${w.prefix} ${a.name}/${c.id}: ${p.error}`)
      }
    }
  }
  return out
}

afterEach(() => localStorage.clear())

// Owner bug report G1 #1: "Agent spend" on Today said "This addon panel could not be shown".
describe('Today Glance: every item can be shown', () => {
  it('in the normal demo and on the busy day, in every workspace', () => {
    expect(glanceProblems(createMockStore({ persist: false }))).toEqual([])
    expect(glanceProblems(createMockStore({ persist: false, dataset: 'busy' }))).toEqual([])
  })

  it('after a reload with usage state saved by an older demo (models opus/sonnet/haiku)', () => {
    // What a browser that opened the hosted preview before the model names changed still has in storage.
    const old = createMockStore({ persist: false })
    const ws = old.workspaces[0].id
    const days = (old.addonState(ws, 'usage').days as { date: string }[]).map((d) => ({
      date: d.date,
      cents: { opus: 180, sonnet: 90, haiku: 20 },
      tokens: { opus: 45_000, sonnet: 81_000, haiku: 60_000 },
    }))
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ v: 2, ticketEvents: {}, created: {}, wsEvents: {}, addonState: { [`${ws}/usage`]: { ...old.addonState(ws, 'usage'), days } } }))
    const s = createMockStore({ persist: true })
    expect(glanceProblems(s)).toEqual([])
    expect((s.addonStateView(ws, 'usage')!.weekTrend as number[]).every(Number.isFinite)).toBe(true)
  })

  it('a trend bound to state that is not there draws no sparkline instead of failing the item', () => {
    expect(parseNode({ type: 'stat', label: 'x', value: 1, trend: null }).ok).toBe(true)
    expect(parseNode({ type: 'stat', label: 'x', value: 1, trend: [Number.NaN] }).ok).toBe(false)
  })

  it('renders without "could not be shown" in the normal demo and on the busy day', async () => {
    for (const dataset of ['normal', 'busy'] as const) {
      const r = renderApp('/', { viewer: 'p_sev', setup: (s) => dataset === 'busy' && s.reset('busy') })
      const glance = await screen.findByRole('region', { name: 'Glance' }, T)
      await within(glance).findByText(/this month/, {}, T)
      expect(within(glance).queryByText(/could not be shown/)).toBeNull()
      r.unmount()
    }
  }, 30_000)
})
