import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { api, mockStore } from '@/api/client'
import { installAndGrant } from '@/test/installAddon'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
const ws = () => mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id
const setup = (store: typeof mockStore) => installAndGrant(store, store.workspaces.find((w) => w.prefix === 'DEMO')!.id, 'activity')
const PATH = '/addon/activity/activity'
const rows = () => screen.getAllByRole('listitem').filter((li) => !li.closest('[data-sonner-toaster]'))
const filterRow = async (name: RegExp) => waitFor(() => {
  const hit = screen.getAllByText(name).find((h) => h.closest('li'))
  if (!hit) throw new Error(`no filter ${name}`)
  return hit.closest('li')!
}, T)

describe('activity page', () => {
  it('shows a day heading, the timeline and the by-ticket table', async () => {
    renderApp(PATH, { viewer: 'p_sev', setup })
    expect(await screen.findByText(/^Today ·/, {}, T)).toBeInTheDocument()
    expect(await screen.findByText(/^By ticket/, {}, T)).toBeInTheDocument()
    expect(await screen.findByRole('columnheader', { name: 'Last actor' }, T)).toBeInTheDocument()
  })
  it('filtering to gates leaves only gate rows', async () => {
    const { user } = renderApp(PATH, { viewer: 'p_sev', setup })
    const g = await filterRow(/^Gates\b/)
    await user.click(within(g).getByRole('button', { name: 'Only show' }))
    await waitFor(() => expect(within(g).getByRole('button', { name: 'Remove filter' })).toBeInTheDocument(), T)
    const st = (await api.getAddonState(ws(), 'activity')) as unknown as { timeline: { group: string; title: string }[] }
    expect(st.timeline.length).toBeGreaterThan(0)
    expect(st.timeline.every((r) => r.group === 'gates')).toBe(true)
    await waitFor(() => expect(screen.queryAllByText(/moved to/).length).toBe(0), T)
    expect(screen.getAllByText(/approved|requested changes|invalidated/).length).toBeGreaterThan(0)
  })
  it('a comment made elsewhere appears at the top on the next refresh', async () => {
    const { user } = renderApp(PATH, { viewer: 'p_sev', setup })
    await filterRow(/^Status\b/)
    await api.postAction('DEMO-0043', { action: 'comment', text: 'Ship it' })
    // the live poll is off in tests; any state refetch (here: toggling a filter twice) shows the new row
    const s = await filterRow(/^Status\b/)
    await user.click(within(s).getByRole('button', { name: 'Only show' }))
    await user.click(await within(await filterRow(/^Status\b/)).findByRole('button', { name: 'Remove filter' }, T))
    await waitFor(() => {
      const first = rows().find((li) => /DEMO-0043/.test(li.textContent ?? '') && /logged a note/.test(li.textContent ?? ''))
      if (!first) throw new Error('no new row')
    }, T)
    const top = rows().find((li) => /logged a note/.test(li.textContent ?? ''))!
    expect(within(top).getByText(/Severin/)).toBeInTheDocument()
  })
  it('Show older appears when more than 50 rows exist and reveals more', async () => {
    const big = (store: typeof mockStore) => {
      setup(store)
      for (let i = 0; i < 70; i++) store.append(i % 2 ? 'DEMO-0044' : 'DEMO-0045', { type: 'log.added', text: `n${i}`, actor: i % 3 ? 'p_sev' : 'p_mara' })
    }
    const { user } = renderApp(PATH, { viewer: 'p_sev', setup: big })
    const btn = await screen.findByRole('button', { name: 'Show older' }, T)
    const before = (await api.getAddonState(ws(), 'activity')) as unknown as { timeline: unknown[] }
    expect(before.timeline).toHaveLength(50)
    await user.click(btn)
    await waitFor(async () => expect(((await api.getAddonState(ws(), 'activity')) as unknown as { timeline: unknown[] }).timeline).toHaveLength(100), T)
  })
  it('a viewer sees the filters enabled', async () => {
    renderApp(PATH, { viewer: 'p_tom', setup })
    await filterRow(/^Gates\b/)
    await waitFor(async () => expect(within(await filterRow(/^Gates\b/)).getByRole('button', { name: 'Only show' })).toBeEnabled(), T)
  })
  it('the Today page shows the card with the live counts', async () => {
    renderApp('/', { viewer: 'p_sev', setup })
    const st = (await api.getAddonState(ws(), 'activity')) as unknown as { todaySummary: string }
    expect(await screen.findByText(st.todaySummary, {}, T)).toBeInTheDocument()
    expect(st.todaySummary).toMatch(/^\d+ events, \d+ by agents$/)
  })
})
