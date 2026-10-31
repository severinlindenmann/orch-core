import { screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { mockStore } from '@/api/client'
import * as features from '@/api/features'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'

const setup = (s: typeof mockStore) => installAndGrant(s, s.workspaces[0].id, 'focus')

afterEach(() => vi.restoreAllMocks())

it('Work tab: summary, sessions by day, the live one open with Open focus, filters narrow the rows', async () => {
  const { user } = renderApp('/ticket/DEMO-0043?tab=work', { setup })
  const tab = await screen.findByTestId('work-tab')
  expect(within(tab).getByTestId('work-summary')).toHaveTextContent('4 sessions over 3 days')
  expect(within(tab).getByText('Today')).toBeInTheDocument()
  const sessions = within(tab).getAllByTestId('work-session')
  expect(sessions).toHaveLength(4)
  expect(sessions[0]).toHaveAttribute('open')
  expect(within(sessions[0]).getByRole('link', { name: 'Open focus' })).toHaveAttribute('href', expect.stringContaining('/focus/DEMO-0043'))
  expect(within(sessions[2]).getByRole('button', { name: 'Read the chat' })).toBeInTheDocument()
  const all = within(tab).getAllByTestId('work-row').length
  await user.click(within(tab).getByRole('button', { name: 'Your decisions' }))
  const rows = within(tab).getAllByTestId('work-row')
  expect(rows.length).toBeLessThan(all)
  for (const r of rows) expect(r.getAttribute('data-tags')).toContain('you')
  await user.click(within(tab).getByRole('button', { name: 'Problems' }))
  expect(within(tab).getAllByTestId('work-row')).toHaveLength(1)
  expect(within(tab).getAllByTestId('work-session')).toHaveLength(1)
})

it('Overview has a Last work card that opens the Work tab', async () => {
  const { user } = renderApp('/ticket/DEMO-0043', { setup })
  const card = await screen.findByTestId('last-work')
  expect(card).toHaveTextContent('Last work · now')
  await user.click(within(card).getByRole('button', { name: 'See all work' }))
  expect(await screen.findByTestId('work-tab')).toBeInTheDocument()
})

it('a ticket without recorded work shows a plain empty state', async () => {
  renderApp('/ticket/DEMO-0042?tab=work', { setup })
  expect(await screen.findByTestId('work-empty')).toHaveTextContent('No agent work recorded on this ticket yet.')
})

it('on a real host ?tab=work falls back to Overview (no Work tab, no blank body)', async () => {
  vi.spyOn(features, 'isRealHost').mockReturnValue(true)
  renderApp('/ticket/DEMO-0043?tab=work', { setup })
  expect(await screen.findByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.queryByRole('tab', { name: 'Work' })).not.toBeInTheDocument()
  expect(screen.queryByTestId('work-tab')).not.toBeInTheDocument()
})
