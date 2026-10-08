import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'

// The "Demo data" pill in the top bar: shows the mode and switches between the normal demo and the busy day.
const pill = () => screen.getByTestId('demo-mode')
const decisions = async () => {
  const line = await screen.findByText(/decisions? (need you|open)/)
  return Number(/(\d+) decisions?/.exec(line.textContent ?? '')![1])
}

describe('demo mode switch', () => {
  it('shows "Demo data" on the normal demo, with a Normal / Busy day choice', async () => {
    renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    expect(pill()).toHaveTextContent(/^Demo data$/)
    const group = screen.getByRole('group', { name: 'Demo dataset' })
    expect(within(group).getByRole('button', { name: 'Normal' })).toHaveAttribute('aria-pressed', 'true')
    expect(within(group).getByRole('button', { name: 'Busy day' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('asks first, and Cancel keeps the demo as it is', async () => {
    const { user } = renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    await user.click(screen.getByRole('button', { name: 'Busy day' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Switch to the busy day demo?')).toBeInTheDocument()
    expect(within(dialog).getByText('Your demo changes are discarded.')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(mockStore.dataset).toBe('normal')
    expect(pill()).toHaveTextContent(/^Demo data$/)
  })

  it('switches to the busy day: the pill names it and Today is under pressure', async () => {
    const { user } = renderApp('/')
    const before = await decisions()
    await user.click(screen.getByRole('button', { name: 'Busy day' }))
    await user.click(await screen.findByRole('button', { name: 'Switch to busy day' }))
    await waitFor(() => expect(pill()).toHaveTextContent('Demo data · Busy day'))
    expect(mockStore.dataset).toBe('busy')
    expect(screen.getByRole('button', { name: 'Busy day' })).toHaveAttribute('aria-pressed', 'true')
    await waitFor(async () => expect(await decisions()).toBeGreaterThanOrEqual(20))
    expect(before).toBeLessThan(20)
    mockStore.sim.stopAll()
  })

  it('discards the demo changes on a switch, and Reset demo keeps the mode', async () => {
    const { user } = renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    await user.click(screen.getByRole('button', { name: 'Busy day' }))
    await user.click(await screen.findByRole('button', { name: 'Switch to busy day' }))
    await waitFor(() => expect(mockStore.dataset).toBe('busy'))
    mockStore.append('DEMO-0043', { type: 'log.added', actor: 'p_sev', text: 'a change' })
    await user.click(screen.getByRole('button', { name: 'Reset demo' }))
    await waitFor(() => expect(mockStore.eventsOf('DEMO-0043').some((e) => e.text === 'a change')).toBe(false))
    expect(mockStore.dataset).toBe('busy')
    expect(pill()).toHaveTextContent('Demo data · Busy day')
    mockStore.sim.stopAll()
  })

  it('switches back to the normal demo with the same question', async () => {
    const { user } = renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    await user.click(screen.getByRole('button', { name: 'Busy day' }))
    await user.click(await screen.findByRole('button', { name: 'Switch to busy day' }))
    await waitFor(() => expect(pill()).toHaveTextContent('Demo data · Busy day'))
    await user.click(screen.getByRole('button', { name: 'Normal' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Switch to the normal demo?')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Switch to normal' }))
    await waitFor(() => expect(pill()).toHaveTextContent(/^Demo data$/))
    expect(mockStore.dataset).toBe('normal')
  })
})

// The pages still draw with the busy day's data (their density is Phase 3's job; here nothing may break).
describe('busy day renders', () => {
  const busyTicket = (kind: 'artifacts' | 'widgets') => {
    mockStore.reset('busy')
    const ws = mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id
    const docs = mockStore.listTickets(ws).filter((t) => Number(t.key.slice(5)) >= 100)
    const pick = kind === 'artifacts' ? docs.find((t) => t.artifacts.length >= 15) : docs.find((t) => (Object.values(t.body).join('\n').match(/```orch/g) ?? []).length >= 6)
    mockStore.reset('normal')
    return pick!.key
  }
  const paths = () => ['/', '/board', '/tickets', '/agents', `/ticket/${busyTicket('artifacts')}`, `/ticket/${busyTicket('widgets')}`]
  it.each([0, 1, 2, 3, 4, 5])('page %i draws without a problem panel', async (i) => {
    const path = paths()[i]
    renderApp(path, { setup: (s) => s.reset('busy') })
    await waitFor(() => expect(screen.getByTestId('demo-mode')).toHaveTextContent('Busy day'))
    await screen.findByRole('heading', { level: 1 }, { timeout: 8000 })
    await new Promise((r) => setTimeout(r, 150))
    expect(screen.queryByText('This page hit a problem')).toBeNull()
    expect(screen.queryByText(/could not be drawn/)).toBeNull()
    mockStore.sim.stopAll()
  }, 20000)
})
