import { screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'
import diffFixture from '@/mocks/addons/focus/worktree-diff.json'
import { authorsOf, blockOf } from './authors'
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

// P2-5: the live worktree diff, author labels and the overlap warning
const authors = {
  sessions: [{ files: ['src/api/invite.py', '/work/tree/notes.txt'], children: [
    { title: 'Write boundary tests', files: ['tests/test_invite.py', 'src/api/invite.py'] },
  ] }],
}
it('maps files to authors and splits the unified text per file', () => {
  expect(authorsOf('src/api/invite.py', authors)).toEqual(['main', 'Write boundary tests'])
  expect(authorsOf('notes.txt', authors)).toEqual(['main'])
  expect(authorsOf('other.txt', authors)).toEqual([])
  expect(authorsOf('x', null)).toEqual([])
  const u = (diffFixture as { unified: string }).unified
  expect(blockOf(u, 'tests/test_invite.py')).toContain('+def test_a():')
  expect(blockOf(u, 'tests/test_invite.py')).not.toContain('scratch')
  expect(blockOf(u, 'missing.py')).toBeNull()
})

it('shows live files with authors, warns on overlap and opens a hunk', async () => {
  const { client, user } = renderApp('/focus/DEMO-0042', { setup })
  const ws = mockStore.workspaces[0].id
  client.setQueryData(['focus', 'worktree-diff', ws, 'DEMO-0042'], diffFixture)
  client.setQueryData(['focus', 'worktree-diff-authors', ws, 'DEMO-0042'], authors)
  const live = await screen.findByTestId('focus-live-changes')
  const rows = within(live).getAllByTestId('live-file')
  expect(rows).toHaveLength(3)
  expect(within(rows[0]).getByRole('alert')).toHaveTextContent('Overlap: also changed by Write boundary tests. Resolve before integration.')
  expect(within(rows[0]).getByTestId('live-author')).toHaveTextContent('by main, Write boundary tests')
  expect(within(rows[1]).getByTestId('live-author')).toHaveTextContent('by Write boundary tests')
  expect(within(rows[1]).queryByRole('alert')).toBeNull()
  expect(within(rows[2]).getByTestId('live-author')).toHaveTextContent('by main')
  await user.click(within(rows[0]).getByRole('button'))
  expect(within(rows[0]).getByText(/\+TWO/)).toBeInTheDocument()
})

it('says author unknown when the delegation list names nobody, and shows an unavailable reason', async () => {
  const { client } = renderApp('/focus/DEMO-0042', { setup })
  const ws = mockStore.workspaces[0].id
  client.setQueryData(['focus', 'worktree-diff', ws, 'DEMO-0042'], diffFixture)
  client.setQueryData(['focus', 'worktree-diff-authors', ws, 'DEMO-0042'], { sessions: [] })
  const live = await screen.findByTestId('focus-live-changes')
  expect(within(live).getAllByTestId('live-author')[0]).toHaveTextContent('author unknown')
  client.setQueryData(['focus', 'worktree-diff', ws, 'DEMO-0042'], { ...diffFixture, available: false, reason: 'no worktree is registered for this ticket', files: [] })
  expect(await within(live).findByText('no worktree is registered for this ticket')).toBeInTheDocument()
})

it('has a Live switch that pauses reading', async () => {
  const { client, user } = renderApp('/focus/DEMO-0042', { setup })
  client.setQueryData(['focus', 'worktree-diff', mockStore.workspaces[0].id, 'DEMO-0042'], diffFixture)
  const live = await screen.findByTestId('focus-live-changes')
  const sw = within(live).getByRole('switch', { name: 'Live' })
  expect(sw).toHaveAttribute('aria-checked', 'false')  // no agent run in the mock, so it starts paused
  await user.click(sw)
  expect(sw).toHaveAttribute('aria-checked', 'true')
})
