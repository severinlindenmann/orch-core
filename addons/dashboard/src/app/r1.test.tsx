import { act, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { groupOf } from './attention'
import type { AgentSession } from '@/api/types'

beforeEach(() => sessionStorage.clear())
afterEach(() => vi.restoreAllMocks())

it('R-a: every expanded lane cell caps at five and expands independently', async () => {
  const { user } = renderApp('/board', { setup: s => s.reset('busy', true) })
  const cell = await screen.findByRole('group', { name: 'No epic · In progress' })
  expect(within(cell).getAllByTestId(/^card-/)).toHaveLength(5)
  const more = within(cell).getByRole('button', { name: /^\+\d+ more$/ })
  await user.click(more)
  expect(within(cell).getAllByTestId(/^card-/).length).toBeGreaterThan(5)
  expect(within(screen.getByRole('group', { name: 'No epic · Waiting' })).getAllByTestId(/^card-/).length).toBeLessThanOrEqual(5)
})

it('R-b: card fields and sums are neutral, Display credits Estimate once', async () => {
  const { user } = renderApp('/board')
  const card = await screen.findByTestId('card-DEMO-0043')
  await waitFor(() => expect(card).toHaveTextContent(/5\s*pt/))
  expect(within(card).queryByLabelText(/addon/i)).toBeNull()
  expect(card.querySelector('[data-addon="estimate"]')).not.toHaveClass('border-addon/50')
  const sum = within(screen.getByRole('region', { name: 'In progress' })).getByLabelText(/^Sum of/)
  expect(sum).toHaveTextContent(/\d+ pts?$/)
  expect(within(sum).queryByText('A')).toBeNull()
  await user.click(screen.getByRole('button', { name: 'Display' }))
  expect(await screen.findByText('Points from Estimate')).toBeInTheDocument()
})

it('R-c: connections are counted and fixing one updates header and badge together', async () => {
  const { user } = renderApp('/')
  const line = await screen.findByText(/· \d+ need you ·/)
  const count = () => Number(/· (\d+) need you/.exec(line.textContent!)![1])
  const ws = mockStore.workspaces[0].id
  expect(count()).toBe(mockStore.needsYou(ws).length + mockStore.addonDecisions(ws).length + 2)
  const old = count()
  await user.click(within(screen.getByTestId('relogin-databricks-prod')).getByRole('button', { name: 'Run check again' }))
  await waitFor(() => expect(count()).toBe(old - 1))
  expect(screen.getByRole('link', { name: `Today, ${old - 1} need you` })).toBeInTheDocument()
})

it('waiting for another person and idle sessions never count as working', () => {
  const s = { session: 'a', state: 'waiting', for: 'p_mara' } as AgentSession
  expect(groupOf(s, [s], 'p_sev')).toBe('waiting-others')
  expect(groupOf({ ...s, state: 'idle' }, [], 'p_sev')).toBe('idle')
})

it('expanded questions have no duplicate Answer toggle', async () => {
  renderApp('/')
  const row = await screen.findByTestId('card-question:DEMO-0043:Q2')
  await within(row).findAllByRole('radio')
  expect(within(row).queryByRole('button', { name: 'Answer' })).toBeNull()
})

it('R-d: core keeps Signing… visible with controls disabled until the host confirms', async () => {
  let resolve!: (v: Awaited<ReturnType<typeof api.postAction>>) => void
  const post = vi.spyOn(api, 'postAction').mockImplementation(() => new Promise(r => { resolve = r }))
  const { user } = renderApp('/')
  const row = await screen.findByTestId('card-question:DEMO-0043:Q2')
  await user.click((await within(row).findAllByRole('radio'))[0])
  await user.click(within(row).getByRole('button', { name: 'Send answer…' }))
  const dialog = await screen.findByRole('dialog')
  await user.click(within(dialog).getByRole('button', { name: 'Send answer' }))
  await waitFor(() => expect(post).toHaveBeenCalled())
  expect(within(dialog).getByText('Signing…')).toBeInTheDocument()
  expect(within(dialog).getByRole('button', { name: 'Send answer' })).toBeDisabled()
  await act(async () => resolve({ ok: true } as Awaited<ReturnType<typeof api.postAction>>))
})

it('viewer sees collapsed per-person summaries before individual decisions', async () => {
  const { user } = renderApp('/', { viewer: 'p_tom' })
  await screen.findByText(/Nothing needs you/)
  expect(screen.queryByTestId('card-question:DEMO-0043:Q2')).toBeNull()
  await user.click(await screen.findByRole('button', { name: /^Severin · \d+ decisions/, expanded: false }))
  expect(await screen.findByTestId('card-question:DEMO-0043:Q2')).toBeInTheDocument()
})

it('new arrivals lead their group with a new marker after Show', async () => {
  const { user } = renderApp('/')
  await screen.findByTestId('card-question:DEMO-0043:Q2')
  mockStore.append('DEMO-0044', { type: 'question.asked', actor: 'p_mara', question: 'Q9', def: { id: 'Q9', to: 'p_sev', text: 'Keep the old role?', options: [{ key: 'a', label: 'Yes' }], blocking: false } })
  // Signing an existing answer refreshes the inbox, as a live arrival does.
  const row = screen.getByTestId('card-question:DEMO-0043:Q2')
  await user.click((await within(row).findAllByRole('radio'))[0])
  await user.click(within(row).getByRole('button', { name: 'Send answer…' }))
  await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Send answer' }))
  await user.click(await screen.findByRole('button', { name: '1 new · Show' }))
  const questions = screen.getByRole('region', { name: /^Questions ·/ })
  expect(within(questions).getAllByRole('listitem')[0]).toHaveAttribute('data-testid', 'card-question:DEMO-0044:Q9')
  expect(within(questions).getByLabelText('New since your last look')).toBeInTheDocument()
  expect(within(questions).queryByText('blocking')).toBeNull()
})

it('Reset restores all questions directly, with no new-items buffer', async () => {
  const { user } = renderApp('/')
  const row = await screen.findByTestId('card-question:DEMO-0043:Q2')
  await user.click((await within(row).findAllByRole('radio'))[0])
  await user.click(within(row).getByRole('button', { name: 'Send answer…' }))
  await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Send answer' }))
  await waitFor(() => expect(screen.queryByTestId('card-question:DEMO-0043:Q2')).toBeNull())
  await user.click(screen.getByRole('button', { name: 'Reset demo' }))
  await user.click(await screen.findByRole('button', { name: 'Reset demo data' }))
  expect(await screen.findByTestId('card-question:DEMO-0043:Q2')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /new · Show/ })).toBeNull()
})

it('Agents waiting-on-you link opens the exact question', async () => {
  const { user } = renderApp('/')
  await screen.findByText(/· \d+ need you ·/)
  const bar = screen.getByRole('region', { name: 'Agents' })
  await user.click(within(bar).getByRole('button', { name: 'Show' }))
  const link = await within(await screen.findByRole('dialog')).findByRole('link', { name: /Answer Q2 on DEMO-0043/ })
  expect(link).toHaveAttribute('href', '/ticket/DEMO-0043#question-Q2')
  await user.click(link)
  expect(await screen.findByRole('tab', { name: /Questions/ })).toHaveAttribute('data-state', 'active')
  expect(document.getElementById('question-Q2')).toBeInTheDocument()
})
