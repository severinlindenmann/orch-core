import { cleanup, screen, within } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { resetLayoutPreview } from './layoutPreview'

const original = { ...api }
afterEach(() => {
  cleanup()
  resetLayoutPreview()
  expect({ ...api }).toEqual(original)
})

it('preview swaps are reversible, including after a second installation', async () => {
  const first = renderApp('/__layout/records-ok')
  await screen.findByTestId('records-export-warning')
  expect(api.getRecords).not.toBe(original.getRecords)
  first.unmount()
  resetLayoutPreview()
  expect({ ...api }).toEqual(original)
  renderApp('/__layout/records-first')
  await screen.findByTestId('records-not-configured')
  expect(api.getRecords).not.toBe(original.getRecords)
})

it('the real-host terminal scenarios draw the real dock from fixtures and undo their swaps', async () => {
  const { runsApi } = await import('@/api/runs')
  const { termTransport } = await import('@/app/terminal/real/termProtocol')
  const runs = { ...runsApi }
  const connect = termTransport.connect
  const view = renderApp('/__layout/terminal-take')
  expect(await screen.findByText('Another tab or device is controlling this terminal.')).toBeInTheDocument()
  view.unmount()
  resetLayoutPreview()
  renderApp('/__layout/terminal-exited')
  expect(await screen.findByText('Output no longer kept')).toBeInTheDocument()
  cleanup()
  resetLayoutPreview()
  renderApp('/__layout/terminal-stop')
  expect(await screen.findByText('Some processes may still be running.')).toBeInTheDocument()
  cleanup()
  resetLayoutPreview()
  expect({ ...runsApi }).toEqual(runs)
  expect(termTransport.connect).toBe(connect)
})

it.each(['records-push-failed', 'records-push-mismatch'])('preview supplies the current destination for %s', async (scenario) => {
  const { user } = renderApp(`/__layout/${scenario}`)
  await user.click(await screen.findByRole('button', { name: 'Commit and push' }))
  const dialog = await screen.findByRole('dialog')
  const warning = await within(dialog).findByTestId('records-export-warning')
  expect(warning).not.toHaveTextContent('last known')
  expect(dialog).toHaveTextContent('orch trusts your word that only you read')
  if (scenario === 'records-push-mismatch') expect(within(dialog).getByTestId('records-push-mismatch')).toHaveTextContent('This is not where the last push went')
})

it('the Start agent scenarios draw core\'s dialog from fixtures and undo their swaps', async () => {
  const { agentsApi } = await import('@/api/agents')
  const agents = { ...agentsApi }
  renderApp('/__layout/agent-dialog')
  expect(await screen.findByTestId('agent-preview')).toBeInTheDocument()
  expect(await screen.findByTestId('agent-risk-full')).toBeInTheDocument()
  expect(screen.queryByTestId('agent-access')).toBeNull() // access is valid: no select
  cleanup()
  resetLayoutPreview()
  renderApp('/__layout/agent-dialog-acked')
  expect(await screen.findByTestId('agent-preview')).toBeInTheDocument()
  expect(screen.queryByTestId('agent-risk-full')).toBeNull()
  cleanup()
  resetLayoutPreview()
  renderApp('/__layout/agent-blocked')
  expect(await screen.findByTestId('agent-runner-off')).toHaveTextContent('Dashboard execution is off')
  cleanup()
  resetLayoutPreview()
  renderApp('/__layout/agent-grant')
  expect(await screen.findByTestId('agent-access')).toBeInTheDocument()
  expect(await screen.findByTestId('agent-start')).toHaveTextContent('Sign and start agent')
  cleanup()
  resetLayoutPreview()
  expect({ ...agentsApi }).toEqual(agents)
})

it('the New ticket start scenarios draw the real form with and without an agent access', async () => {
  renderApp('/__layout/new-sequence-start')
  expect(await screen.findByRole('button', { name: 'Create and start' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'More ways to create' })).toBeInTheDocument()
  expect(screen.queryByTestId('agent-access')).toBeNull()
  cleanup()
  resetLayoutPreview()
  renderApp('/__layout/new-sequence-start-nogrant')
  expect(await screen.findByRole('button', { name: 'Create and start' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'More ways to create' })).toBeInTheDocument()
  expect(screen.queryByTestId('agent-access')).toBeNull() // asked in the review popover, not in the form
})
