import { screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { mockStore } from '@/api/client'
import * as features from '@/api/features'
import { focusApi } from '@/api/focus'
import { runsApi, type Run } from '@/api/runs'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'
const setup = (s: typeof mockStore) => installAndGrant(s, s.workspaces[0].id, 'focus')
afterEach(() => vi.restoreAllMocks())

it('lists what the agent read and opens the text of a web source', async () => {
  const { user } = renderApp('/focus/DEMO-0042', { setup })
  await user.click(await screen.findByRole('tab', { name: /Research/ }))
  const list = await screen.findByRole('list', { name: 'Sources the agent read' })
  expect(within(list).getByText('Searched: redis rate limit counter')).toBeInTheDocument()
  expect(within(list).getByText('docs/api/invites.md')).toBeInTheDocument()
  await user.click(within(list).getByRole('button', { name: /RFC 6585/ }))
  expect(await screen.findByText(/429 Too Many Requests/)).toBeInTheDocument()
  const link = screen.getByRole('link', { name: 'Open in browser' })
  expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  expect(document.querySelector('iframe')).toBeNull()
})
it('shows an empty state for scratch', async () => {
  const { user } = renderApp('/focus?scratch=1', { setup })
  await user.click(await screen.findByRole('tab', { name: /Research/ }))
  expect(screen.getByText('No ticket yet. Research will appear here.')).toBeInTheDocument()
})
it('on a real host reads the sources of the ticket agent run and shows unavailability', async () => {
  vi.spyOn(features, 'isRealHost').mockReturnValue(true)
  const run = { run: 'r_01J0000000000000000000000A', rev: 1, kind: 'agent', ticket: 'DEMO-0042', harness: 'claude-code', state: 'running', started_at: '2026-10-26T10:00:00Z' } as Run
  vi.spyOn(runsApi, 'list').mockResolvedValue({ runs: [run], runner: null, truncated: false })
  const spy = vi.spyOn(focusApi, 'sources').mockResolvedValue({ run: run.run, available: false, reason: 'transcript not found yet', sources: [] })
  const { user } = renderApp('/focus/DEMO-0042', { setup })
  await user.click(await screen.findByRole('tab', { name: /Research/ }))
  expect(await screen.findByText('Waiting for the first message...')).toBeInTheDocument()
  expect(spy).toHaveBeenCalledWith(expect.any(String), run.run)
})
