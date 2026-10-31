// P14 (UX audit W1c): an empty Today and an empty Board show one big "Give your AI helper a task" button that opens
// the new-ticket flow.
import { cleanup, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 8000 }

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function emptyWorkspace() {
  const real = api.getToday.bind(api)
  vi.spyOn(api, 'getToday').mockImplementation(async (ws) => ({ ...(await real(ws)), needs_you: [], read_only_open: [], waiting_on_others: { count: 0, people: [] }, working: [], recent: [], counts: {} }))
  vi.spyOn(api, 'getAgents').mockResolvedValue([])
  vi.spyOn(api, 'getAddonDecisions').mockResolvedValue([])
  vi.spyOn(api, 'listBoard').mockResolvedValue([])
}

describe('the first step on an empty dashboard', () => {
  it('Today: one big button that opens the new ticket flow', async () => {
    emptyWorkspace()
    const { user } = renderApp('/')
    await screen.findByText('Nothing needs you right now', {}, T)
    await user.click(await screen.findByTestId('first-task', {}, T))
    expect(await screen.findByRole('dialog', { name: 'New ticket' }, T)).toBeInTheDocument()
  })

  it('Board: no empty columns, one big button that opens the new ticket flow', async () => {
    emptyWorkspace()
    const { user } = renderApp('/board')
    const button = await screen.findByTestId('first-task', {}, T)
    expect(button).toHaveTextContent('Give your AI helper a task')
    expect(screen.queryByText('Nothing here')).toBeNull()
    await user.click(button)
    expect(await screen.findByRole('dialog', { name: 'New ticket' }, T)).toBeInTheDocument()
  })

  it('a viewer, who cannot create tickets, gets no button', async () => {
    emptyWorkspace()
    renderApp('/', { viewer: 'p_tom' })
    await screen.findByText('Nothing is open in the workspace', {}, T)
    expect(screen.queryByTestId('first-task')).toBeNull()
  })
})
