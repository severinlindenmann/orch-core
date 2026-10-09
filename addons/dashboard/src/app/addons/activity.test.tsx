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
const rowTexts = () => rows().map((li) => li.textContent)

describe('activity page', () => {
  it('leads with the timeline: the Timeline heading precedes any table and the period is Today', async () => {
    renderApp(PATH, { viewer: 'p_sev', setup })
    const period = await screen.findByRole('combobox', { name: 'Period' }, T)
    expect(period).toHaveDisplayValue('Today')
    const heading = await screen.findByRole('heading', { name: 'Timeline' }, T)
    expect(await screen.findByText(/^Today ·/, {}, T)).toBeInTheDocument()
    expect(screen.queryByRole('columnheader')).toBeNull()
    expect(heading.compareDocumentPosition(period) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy() // the bar comes first, then the timeline
  })
  it('has one filter bar and no "Only show" buttons', async () => {
    renderApp(PATH, { viewer: 'p_sev', setup })
    await screen.findByRole('combobox', { name: 'Type' }, T)
    expect(screen.getByRole('combobox', { name: 'Person' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Search' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Apply' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Only show' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Clear filters' })).toBeNull()
  })
  it('choosing Status (n) and applying shows exactly n events', async () => {
    const { user } = renderApp(PATH, { viewer: 'p_sev', setup })
    const type = await screen.findByRole('combobox', { name: 'Type' }, T)
    const opt = within(type).getAllByRole('option').find((o) => /^Status \(\d+\)$/.test(o.textContent ?? ''))!
    const n = Number(opt.textContent!.match(/\((\d+)\)/)![1])
    await user.selectOptions(type, opt)
    await user.click(screen.getByRole('button', { name: 'Apply' }))
    expect(await screen.findByText(new RegExp(`of ${n} matching`), {}, T)).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Clear filters' }, T)).toBeInTheDocument()
    const st = (await api.getAddonState(ws(), 'activity')) as unknown as { counts: { matching: number } }
    expect(st.counts.matching).toBe(n)
  })
  it('By ticket swaps the timeline for the table, and back', async () => {
    const { user } = renderApp(PATH, { viewer: 'p_sev', setup })
    await screen.findByRole('heading', { name: 'Timeline' }, T)
    await user.click(screen.getByRole('button', { name: 'By ticket' }))
    expect(await screen.findByRole('columnheader', { name: 'Last actor' }, T)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Timeline' })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Timeline' }))
    expect(await screen.findByRole('heading', { name: 'Timeline' }, T)).toBeInTheDocument()
    expect(screen.queryByRole('columnheader')).toBeNull()
  })
  it('a comment made elsewhere is offered as "Show 1 new event" and no row moves until it is taken', async () => {
    const { user } = renderApp(PATH, { viewer: 'p_sev', setup })
    await screen.findByRole('heading', { name: 'Timeline' }, T)
    // the first navigation action takes the viewer's reading position
    await user.click(screen.getByRole('button', { name: 'By ticket' }))
    await user.click(await screen.findByRole('button', { name: 'Timeline' }, T))
    await waitFor(() => expect(rows().length).toBeGreaterThan(0), T)
    const before = rowTexts()
    await api.postAction('DEMO-0043', { action: 'comment', text: 'Ship it' })
    // the live poll is off in tests; a view switch refetches the state
    await user.click(screen.getByRole('button', { name: 'By ticket' }))
    await user.click(await screen.findByRole('button', { name: 'Timeline' }, T))
    const btn = await screen.findByRole('button', { name: /Show \d+ new events?/ }, T)
    expect(rowTexts()).toEqual(before)
    await user.click(btn)
    await waitFor(() => {
      const top = rows().find((li) => /DEMO-0043/.test(li.textContent ?? '') && /logged a note/.test(li.textContent ?? ''))
      if (!top) throw new Error('no new row')
    }, T)
    expect(screen.queryByRole('button', { name: /Show \d+ new events?/ })).toBeNull()
  })
  it('Show older appears when more than 30 rows exist and reveals more', async () => {
    const big = (store: typeof mockStore) => {
      setup(store)
      for (let i = 0; i < 70; i++) store.append(i % 2 ? 'DEMO-0044' : 'DEMO-0045', { type: 'log.added', text: `n${i}`, actor: i % 3 ? 'p_sev' : 'p_mara' })
    }
    const { user } = renderApp(PATH, { viewer: 'p_sev', setup: big })
    const btn = await screen.findByRole('button', { name: 'Show older' }, T)
    const before = (await api.getAddonState(ws(), 'activity')) as unknown as { timeline: unknown[] }
    expect(before.timeline).toHaveLength(30)
    await user.click(btn)
    await waitFor(async () => expect(((await api.getAddonState(ws(), 'activity')) as unknown as { timeline: unknown[] }).timeline).toHaveLength(60), T)
  })
  it('a viewer sees the filter bar enabled', async () => {
    renderApp(PATH, { viewer: 'p_tom', setup })
    await waitFor(async () => expect(await screen.findByRole('button', { name: 'Apply' })).toBeEnabled(), T)
    expect(screen.getByRole('combobox', { name: 'Period' })).toBeEnabled()
  })
  it('the Today page shows the card with the live counts', async () => {
    renderApp('/', { viewer: 'p_sev', setup })
    const st = (await api.getAddonState(ws(), 'activity')) as unknown as { todaySummary: string }
    expect(await screen.findByText(st.todaySummary, {}, T)).toBeInTheDocument()
    expect(st.todaySummary).toMatch(/^\d+ events, \d+ by agents$/)
  })
})
