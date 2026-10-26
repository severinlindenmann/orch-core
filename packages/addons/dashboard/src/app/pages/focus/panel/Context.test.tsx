import { screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'
const setup = (s: typeof mockStore) => installAndGrant(s, s.workspaces[0].id, 'focus')
it('shows title, acceptance checks, branch and counts on every tab', async () => {
  const { user } = renderApp('/focus/DEMO-0042', { setup })
  await user.click(await screen.findByRole('tab', { name: 'Context' }))
  const panel = await screen.findByTestId('focus-panel-context')
  const ticket = mockStore.ticket('DEMO-0042')!
  expect(within(panel).getByText(ticket.title)).toBeInTheDocument()
  expect(within(panel).getByText(ticket.branch.name)).toBeInTheDocument()
  for (const ac of ticket.acceptance_state) expect(within(panel).getByText(ac.text)).toBeInTheDocument()
  for (const name of ['Decisions', 'Context']) expect(within(screen.getByRole('tab', { name })).getByLabelText(/\d+ items/)).toBeInTheDocument()
})
it('shows an empty state and no zero badges for scratch', async () => {
  const { user } = renderApp('/focus?scratch=1', { setup })
  await user.click(await screen.findByRole('tab', { name: 'Context' }))
  expect(screen.getByText('No ticket yet. Context will appear here.')).toBeInTheDocument()
  for (const name of ['Artifacts', 'Context']) expect(within(screen.getByRole('tab', { name })).queryByLabelText(/items/)).toBeNull()
})
it('lists linked wiki pages when the addon is enabled', async () => {
  const { user } = renderApp('/focus/DEMO-0043', { setup })
  await user.click(await screen.findByRole('tab', { name: 'Context' }))
  expect(await screen.findByText('Linked wiki pages')).toBeInTheDocument()
  const panel = screen.getByTestId('focus-panel-context')
  expect(within(panel).getAllByRole('link').length).toBeGreaterThan(0)
})
