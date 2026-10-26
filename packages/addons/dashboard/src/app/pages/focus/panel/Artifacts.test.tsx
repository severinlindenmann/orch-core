import { screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'
const setup = (s: typeof mockStore) => installAndGrant(s, s.workspaces[0].id, 'focus')
it('lists artifacts with a count and opens the existing preview', async () => {
  const { user } = renderApp('/focus/DEMO-0043', { setup })
  const tab = await screen.findByRole('tab', { name: 'Artifacts' })
  await user.click(tab)
  const list = await screen.findByRole('list', { name: 'Ticket artifacts' })
  const artifacts = mockStore.ticket('DEMO-0043')!.artifacts
  expect(tab).toHaveTextContent(String(artifacts.length))
  await user.click(within(list).getAllByRole('button')[0])
  expect(await screen.findByRole('region', { name: `Preview of ${artifacts[0].name}` })).toBeInTheDocument()
})
it('shows an empty state for scratch', async () => {
  const { user } = renderApp('/focus?scratch=1', { setup })
  await user.click(await screen.findByRole('tab', { name: 'Artifacts' }))
  expect(screen.getByText('Create a ticket to collect artifacts here.')).toBeInTheDocument()
})
it('shows an empty artifact list for a ticket with no artifacts', async () => {
  const { user } = renderApp('/focus/DEMO-0042', { setup })
  await user.click(await screen.findByRole('tab', { name: 'Artifacts' }))
  expect(await screen.findByText('No artifacts yet.')).toBeInTheDocument()
})
