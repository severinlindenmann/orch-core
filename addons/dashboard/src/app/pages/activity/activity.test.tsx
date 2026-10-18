// Activity on a real host: day groups in local time, plain sentences, ticket links, filters sent to the server,
// paging, hidden text and the "not served" state. The server's answers are faked; no mock add-on is involved.
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { setFeatures } from '@/api/features'
import type { ActivityPage, ActivityRow } from '@/api/activity'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  setFeatures(null)
})

const NOW = Date.now()
const iso = (daysAgo: number, hour = 10) => {
  const d = new Date(NOW - daysAgo * 86_400_000)
  d.setHours(hour, 5, 0, 0)
  return d.toISOString()
}
const row = (id: string, patch: Partial<ActivityRow> = {}): ActivityRow => ({
  id,
  at: iso(0),
  type: 'log.added',
  group: 'talk',
  actor: { kind: 'person', id: 'p_a', name: 'Ann' },
  did: 'wrote a note',
  ticket: 'DEMO-0001',
  title: 'Fix the login',
  title_hidden: false,
  ...patch,
})
const page = (items: ActivityRow[], next: string | null = null): ActivityPage => ({ available: true, items, next_page: next })

function open(first: ActivityPage, more?: ActivityPage) {
  setFeatures({ features: ['today', 'tickets', 'board', 'ticket', 'activity'] })
  const spy = vi.spyOn(api, 'getActivity').mockImplementation(async (_ws, p) => (p?.page && more ? more : first))
  return { spy, view: renderApp('/activity', { viewer: 'p_sev' }) }
}

describe('Activity page (real host)', () => {
  it('groups by local day, says it in words and links the ticket', async () => {
    open(page([row('1', { actor: { kind: 'agent', id: 'claude-code', for_name: 'Severin' }, did: 'moved the ticket from open to done', type: 'status.changed' }), row('2', { at: iso(1), did: 'created the ticket' })]))
    expect(await screen.findByRole('heading', { name: /^Today ·/ }, T)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /^Yesterday ·/ })).toBeInTheDocument()
    const rows = screen.getAllByTestId('activity-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('Claude Code for Severin moved the ticket from open to done on DEMO-0001')
    expect(within(rows[0]).getByRole('link', { name: 'DEMO-0001' })).toHaveAttribute('href', expect.stringContaining('DEMO-0001'))
    expect(rows[1]).toHaveTextContent('Ann created the ticket')
  })

  it('sends the chosen filters to the server and starts again from the first page', async () => {
    const { spy, view } = open(page([row('1')]))
    await screen.findAllByTestId('activity-row', {}, T)
    await view.user.selectOptions(screen.getByLabelText('What'), 'reviews')
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(expect.any(String), { group: 'reviews' }))
    await view.user.selectOptions(screen.getByLabelText('Who'), 'agents')
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(expect.any(String), { group: 'reviews', who: 'agents' }))
    await view.user.type(screen.getByLabelText('Ticket'), 'demo-0002')
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(expect.any(String), { group: 'reviews', who: 'agents', ticket: 'DEMO-0002' }))
    await view.user.click(screen.getByRole('button', { name: 'Clear filters' }))
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(expect.any(String), { group: 'all' }))
  })

  it('does not ask the server for a ticket that is not a ticket key', async () => {
    const { spy, view } = open(page([row('1')]))
    await screen.findAllByTestId('activity-row', {}, T)
    const calls = spy.mock.calls.length
    await view.user.type(screen.getByLabelText('Ticket'), 'nope')
    expect(await screen.findByRole('alert')).toHaveTextContent('looks like')
    expect(spy.mock.calls.length).toBe(calls)
  })

  it('shows older events by following the next page, without repeating a row', async () => {
    const { spy, view } = open(page([row('1')], 'tok'), page([row('2', { did: 'created the ticket', at: iso(3) })]))
    await screen.findAllByTestId('activity-row', {}, T)
    await view.user.click(screen.getByRole('button', { name: 'Show older' }))
    await waitFor(() => expect(screen.getAllByTestId('activity-row')).toHaveLength(2))
    expect(spy).toHaveBeenLastCalledWith(expect.any(String), { group: 'all', page: 'tok' })
    expect(screen.queryByRole('button', { name: 'Show older' })).toBeNull()
  })

  it('never shows text that has hidden characters', async () => {
    open(page([row('1', { title: null, title_hidden: true, detail: null, detail_hidden: true })]))
    const r = await screen.findByTestId('activity-row', {}, T)
    expect(r).toHaveTextContent('The ticket title has hidden characters')
    expect(r).toHaveTextContent('The text has hidden characters')
  })

  it('says so when the host has no activity feed, and when nothing matches', async () => {
    open({ available: false, items: [], next_page: null })
    expect(await screen.findByTestId('activity-unavailable', {}, T)).toBeInTheDocument()
    cleanup()
    open(page([]))
    expect(await screen.findByTestId('activity-empty', {}, T)).toHaveTextContent('Nothing has happened')
  })
})
