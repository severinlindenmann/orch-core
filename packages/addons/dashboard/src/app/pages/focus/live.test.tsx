import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'
import { resetLive } from '@/mocks/addons/focus/live'
import { cacheView, costKind } from './live/useLive'

const setup = (s: typeof mockStore) => installAndGrant(s, s.workspaces[0].id, 'focus')
beforeEach(() => resetLive())

it('the agent tree renders from the demo data: helpers, jobs, tasks, finished ones folded', async () => {
  renderApp('/focus/DEMO-0043', { setup })
  const tree = await screen.findByTestId('live-tree')
  for (const name of ['Main agent', 'Write seed tests', 'Check the meter join is unchanged', 'Nightly tariff load', 'Tariff chart images', 'dbt data checks', 'Clean up SQL formatting']) expect(within(tree).getByText(name)).toBeInTheDocument()
  expect(within(tree).queryByText('Explore billing models')).toBeNull() // finished: folded
  expect(within(tree).getByText('Show 2 finished')).toBeInTheDocument()
  expect(screen.getByTestId('live-progress')).toHaveTextContent('2 of 5 tasks done')
  expect(within(screen.getByTestId('live-node-j1')).getByRole('progressbar')).toBeInTheDocument()
})

it('Details is off by default: no usage card; on, the selected agent shows model, cost, cache and tokens', async () => {
  const { user } = renderApp('/focus/DEMO-0043', { setup })
  await screen.findByTestId('live-tree')
  const sw = screen.getByRole('switch', { name: 'Details' })
  expect(sw).not.toBeChecked()
  expect(screen.queryByTestId('live-usage')).toBeNull()
  await user.click(sw)
  const usage = await screen.findByTestId('live-usage')
  expect(usage).toHaveTextContent('Covered by subscription')
  expect(usage).toHaveTextContent('not an extra charge')
  expect(usage).toHaveTextContent('Warm')
  expect(usage).toHaveTextContent('Conversation space')
})

it('a request opens one plain-words sheet; Allow fixes sets the Codex task working', async () => {
  const { user } = renderApp('/focus/DEMO-0043', { setup })
  const banner = await screen.findByTestId('live-request-banner')
  expect(banner).toHaveTextContent('1 request needs your review')
  expect(screen.getByTestId('live-node-c1')).toHaveAttribute('data-state', 'blocked')
  await user.click(within(banner).getByRole('button', { name: 'Review' }))
  const sheet = await screen.findByTestId('live-request-sheet')
  expect(within(sheet).getByText('Allow automatic code fixes?')).toBeInTheDocument()
  expect(within(sheet).getByText('Technical details')).toBeInTheDocument()
  await user.click(within(sheet).getByRole('button', { name: 'Allow fixes' }))
  await waitFor(() => expect(screen.getByTestId('live-node-c1')).toHaveAttribute('data-state', 'working'))
  expect(screen.queryByTestId('live-request-banner')).toBeNull()
})

it('a failed job shows its table and asks the main agent to fix', async () => {
  const { user } = renderApp('/focus/DEMO-0043', { setup })
  await user.click(await within(await screen.findByTestId('live-node-j3')).findByRole('button'))
  const job = await screen.findByTestId('live-job')
  expect(job).toHaveTextContent('Failed')
  expect(job).toHaveTextContent('4 empty valid_from dates')
  await user.click(within(job).getByRole('button', { name: 'Ask the main agent to fix' }))
  expect(await within(screen.getByTestId('live-job')).findByText('You asked the main agent to fix this.')).toBeInTheDocument()
})

it('Commands: scope and status filters, secrets shown as dots', async () => {
  const { user } = renderApp('/focus/DEMO-0043', { setup })
  await screen.findByTestId('live-tree')
  await user.click(screen.getByRole('tab', { name: 'Commands' }))
  const cmds = await screen.findByTestId('live-commands')
  expect(within(cmds).getAllByTestId('live-command').length).toBe(3) // main agent only
  await user.click(within(cmds).getByRole('button', { name: 'All agents on this ticket' }))
  expect(within(cmds).getAllByTestId('live-command').length).toBe(8)
  expect(cmds).toHaveTextContent('Bearer ••••')
  await user.click(within(cmds).getByRole('button', { name: 'Failed' }))
  expect(within(cmds).getAllByTestId('live-command')).toHaveLength(1)
  await user.click(within(cmds).getByRole('button', { name: 'Waiting' }))
  expect(within(cmds).getAllByTestId('live-command')).toHaveLength(1)
  expect(cmds).toHaveTextContent('sqlfluff fix models/')
})

it('cost kinds and cache warmth', () => {
  expect(costKind({ tool: 'local', account: 'Local · office server' })).toBe('local')
  expect(costKind({ tool: 'claude', account: 'Claude · subscription' })).toBe('subscription')
  expect(costKind({ tool: 'claude', account: 'Claude · API key' })).toBe('api')
  const now = Date.parse('2026-10-31T10:00:00Z')
  const at = (ago: number) => ({ lastActivity: new Date(now - ago * 1000).toISOString(), ttl: 300, tokens: 12000, missUsd: 0.03 })
  expect(cacheView(at(20), now)?.kind).toBe('warm')
  expect(cacheView(at(250), now)).toMatchObject({ kind: 'cooling', text: 'Going cold in 0:50' })
  expect(cacheView(at(400), now)?.kind).toBe('cold')
  expect(cacheView(null, now)).toBeNull()
})
