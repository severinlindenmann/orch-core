import { screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'
const setup = (s: typeof mockStore) => installAndGrant(s, s.workspaces[0].id, 'focus')
it('lists files and opens an inline diff', async () => {
  const { user } = renderApp('/focus/DEMO-0042', { setup })
  const panel = await screen.findByTestId('focus-panel-changes')
  const files = within(panel).getAllByRole('button')
  expect(files.length).toBeGreaterThan(0)
  await user.click(files[0])
  expect(files[0]).toHaveAttribute('aria-expanded', 'true')
  expect(panel.querySelector('pre')).toBeInTheDocument()
})
it('shows a scratch empty state', async () => {
  renderApp('/focus?scratch=1', { setup })
  expect(await screen.findByText('No ticket yet. Changes will appear here.')).toBeInTheDocument()
})
it('shows an available dirty worktree count', async () => {
  const { client } = renderApp('/focus/DEMO-0042', { setup })
  client.setQueryData(['worktrees', mockStore.workspaces[0].id, 'DEMO-0042'], { available: true, rows: [{ ticket: 'DEMO-0042', dirty: 3 }] })
  expect(await screen.findByText('3 uncommitted files in the worktree')).toBeInTheDocument()
})
