import { screen, waitFor, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'

const setup = (s: typeof mockStore) => installAndGrant(s, s.workspaces[0].id, 'focus')

// The mock host has a RUNNING agent on DEMO-0043 (mocks/addons/focus/demo.ts): every focus screen is reachable without a host.
it('mock mode: chat opens with a question and a tool row, and the tray is a one-row reminder', async () => {
  const { user } = renderApp('/focus/DEMO-0043', { setup })
  const pane = await screen.findByTestId('focus-conversation')
  await within(pane).findByTestId('focus-demo-terminal')
  const chat = within(pane).getByRole('button', { name: 'Chat' })
  expect(chat).not.toHaveAttribute('aria-disabled')
  await user.click(chat)
  const log = await screen.findByRole('log')
  expect(await within(log).findByText(/Edit seeds\/t1_load.csv/)).toHaveAttribute('data-kind', 'tool')
  expect(within(log).getByText(/DATE or a TIMESTAMP/).closest('[data-kind=question]')).not.toBeNull()
  expect(screen.getByRole('region', { name: 'Needs you' })).toHaveTextContent('Needs you · 1')
})

it('mock mode: tasks (agent-spawned and human-requested, one needing you), Delegate a task, live changes with overlap, research', async () => {
  const { user } = renderApp('/focus/DEMO-0043', { setup })
  const chip = await screen.findByRole('button', { name: '2 tasks, 1 needs you' })
  await user.click(chip)
  const list = await screen.findByTestId('focus-tasks-list')
  expect(await within(list).findByText('Spawned by main agent')).toBeInTheDocument()
  expect(within(list).getByText('Requested by you')).toBeInTheDocument()
  expect(within(list).getByText('needs you')).toBeInTheDocument()
  await user.click(within(list).getByRole('button', { name: 'Delegate a task' }))
  expect(await screen.findByTestId('focus-delegate-sheet')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Cancel' }))
  const live = await screen.findByTestId('focus-live-changes')
  const rows = await within(live).findAllByTestId('live-file')
  expect(within(rows[0]).getByRole('alert')).toHaveTextContent('Overlap: also changed by Write boundary tests')
  expect(within(rows[0]).getByTestId('live-author')).toHaveTextContent('by main, Write boundary tests')
  await user.click(screen.getByRole('tab', { name: 'Research' }))
  await waitFor(() => expect(screen.getByTestId('focus-panel')).toHaveTextContent('RFC 6585'))
})

// Regression: "Start an agent" crashed the page (reading `find` of undefined in the start dialog): the mock host served
// no /model-routing and the dev server's HTML page was taken as data.
it('mock mode: Start an agent opens the dialog without crashing the page', async () => {
  const { user } = renderApp('/focus/DEMO-0042', { setup })
  const pane = await screen.findByTestId('focus-conversation')
  await user.click(await within(pane).findByRole('button', { name: 'Start an agent' }))
  expect(await screen.findByRole('dialog')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Reload' })).toBeNull()
  expect(screen.getByTestId('focus-conversation')).toBeInTheDocument()
})
