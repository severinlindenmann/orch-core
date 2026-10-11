import { act, renderHook, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { groupOf } from './attention'
import { restartToday, useTodayGeneration } from './todayRestart'
import { reloginItems } from '@/api/attention'
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
  expect(count()).toBe(mockStore.needsYou(ws).length + mockStore.addonDecisions(ws).length + reloginItems(mockStore.conn.connections(ws)).length)
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
  await user.click(within(row).getByRole('button', { name: 'Send answer' }))
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
  await user.click(await screen.findByRole('button', { name: /^Severin decides · \d+ open/, expanded: false }))
  expect(await screen.findByTestId('card-question:DEMO-0043:Q2')).toBeInTheDocument()
})

it('new arrivals lead their group with a new marker after Show', async () => {
  const { user } = renderApp('/')
  await screen.findByTestId('card-question:DEMO-0043:Q2')
  mockStore.append('DEMO-0044', { type: 'question.asked', actor: 'p_mara', question: 'Q9', def: { id: 'Q9', to: 'p_sev', text: 'Keep the old role?', options: [{ key: 'a', label: 'Yes' }], blocking: false } })
  // Signing an existing answer refreshes the inbox, as a live arrival does.
  const row = screen.getByTestId('card-question:DEMO-0043:Q2')
  await user.click((await within(row).findAllByRole('radio'))[0])
  await user.click(within(row).getByRole('button', { name: 'Send answer' }))
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
  await user.click(within(row).getByRole('button', { name: 'Send answer' }))
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
  const link = await within(await screen.findByRole('dialog')).findByRole('link', { name: /^waiting on you · Q2, answer it on DEMO-0043$/ })
  expect(link).toHaveAttribute('href', '/w/DEMO/ticket/DEMO-0043#question-Q2')
  await user.click(link)
  expect(await screen.findByRole('tab', { name: /Questions/ })).toHaveAttribute('data-state', 'active')
  expect(document.getElementById('question-Q2')).toBeInTheDocument()
})

it('items new since the last look lead their group with the new dot; the rest carry none', async () => {
  const ws = mockStore.workspaces[0].id
  const ids = [...mockStore.needsYou(ws).map((i) => (i.kind === 'verdict' ? `verdict:${i.ticket}` : `${i.kind}:${i.ticket}:${i.ref}`)), ...mockStore.addonDecisions(ws).map((d) => `addon:${d.id}`)]
  const unseen = ids.filter((id) => id.startsWith('question:')).at(-1)!
  renderApp('/', { storage: { [`orch.today.seen.${ws}:p_sev`]: JSON.stringify(ids.filter((id) => id !== unseen)) } })
  const questions = await screen.findByRole('region', { name: /^Questions ·/ })
  const first = within(questions).getAllByRole('listitem')[0]
  expect(first).toHaveAttribute('data-testid', `card-${unseen}`)
  expect(within(questions).getAllByLabelText('New since your last look')).toHaveLength(1)
  expect(within(first).getByLabelText('New since your last look')).toBeInTheDocument()
})

it('R-d: the Today row being signed is dimmed and busy until the host confirms', async () => {
  let resolve!: (v: Awaited<ReturnType<typeof api.postAction>>) => void
  vi.spyOn(api, 'postAction').mockImplementation(() => new Promise((r) => { resolve = r }))
  const { user } = renderApp('/')
  const row = await screen.findByTestId('card-question:DEMO-0043:Q2')
  await user.click((await within(row).findAllByRole('radio'))[0])
  await user.click(within(row).getByRole('button', { name: 'Send answer' }))
  await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Send answer' }))
  await waitFor(() => expect(row).toHaveAttribute('aria-busy', 'true'))
  expect(within(row).getByText('Signing…')).toBeInTheDocument()
  // Only the row being signed: the others stay as they are.
  for (const other of screen.getAllByTestId(/^card-/)) if (other !== row) expect(other).not.toHaveAttribute('aria-busy')
  await act(async () => resolve({ ok: true } as Awaited<ReturnType<typeof api.postAction>>))
})

it('R-d: the ticket header says Signing… with its actions off until the host confirms', async () => {
  let resolve!: (v: Awaited<ReturnType<typeof api.postAction>>) => void
  vi.spyOn(api, 'postAction').mockImplementation(() => new Promise((r) => { resolve = r }))
  const { user } = renderApp('/ticket/DEMO-0048')
  await user.click(await screen.findByRole('button', { name: 'Approve plan' }))
  const dialog = await screen.findByRole('dialog')
  await user.click(within(dialog).getAllByRole('button', { name: /Approve/ }).at(-1)!)
  const header = screen.getByTestId('ticket-header')
  await waitFor(() => expect(within(header).getByRole('button', { name: 'Signing…', hidden: true })).toBeDisabled())
  expect(within(header).getByRole('button', { name: /^Actions/, hidden: true })).toBeDisabled()
  await act(async () => resolve({ ok: true } as Awaited<ReturnType<typeof api.postAction>>))
})

it('review: "blocking" agrees on Today and the ticket page: only while an agent waits on that question', async () => {
  // A person asks a blocking question: no agent waits on it, so neither surface calls it blocking.
  const { user } = renderApp('/', {
    setup: (st) => st.append('DEMO-0044', { type: 'question.asked', actor: 'p_mara', question: 'Q9', def: { id: 'Q9', to: 'p_sev', text: 'Keep the old role?', options: [{ key: 'a', label: 'Yes' }], blocking: true } }),
  })
  expect((await api.getTicket('DEMO-0044')).questions_state.find((q) => q.id === 'Q9')!.blocking).toBe(false)
  expect((await api.getTicket('DEMO-0043')).questions_state.find((q) => q.id === 'Q2')!.blocking).toBe(true) // Claude Code waits on Q2
  // Board cards read the list rows: the same rule there (the seed flag alone does not count).
  const ws = mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id
  const rows = await api.listTickets(ws)
  expect(rows.find((t) => t.key === 'DEMO-0044')!.blocking_questions).toBe(0)
  expect(rows.find((t) => t.key === 'DEMO-0043')!.blocking_questions).toBe(1)
  const row = await screen.findByTestId('card-question:DEMO-0044:Q9')
  expect(within(row).queryByText('blocking')).toBeNull()
  expect(within(screen.getByTestId('card-question:DEMO-0043:Q2')).getByText('blocking')).toBeInTheDocument()
  await user.click(within(row).getByRole('link', { name: 'DEMO-0044' }))
  await user.click(await screen.findByRole('tab', { name: /Questions/ }))
  const q9 = await screen.findByText('Keep the old role?')
  const card = q9.closest('[id^="question-"]') as HTMLElement
  expect(within(card).getByText('Open')).toBeInTheDocument()
  expect(within(card).queryByText('Open, blocking')).toBeNull()
})

it('review: Reset still succeeds and restarts Today when browser storage is blocked', async () => {
  const { user } = renderApp('/')
  await screen.findByTestId('card-question:DEMO-0043:Q2')
  const blocked = vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
    throw new DOMException('blocked', 'SecurityError')
  })
  await user.click(screen.getByRole('button', { name: 'Reset demo' }))
  await user.click(await screen.findByRole('button', { name: 'Reset demo data' }))
  expect(await screen.findByText('Demo data reset')).toBeInTheDocument()
  expect(screen.queryByText('Reset failed')).toBeNull()
  blocked.mockRestore()
  expect(await screen.findByTestId('card-question:DEMO-0043:Q2')).toBeInTheDocument()
})

it('review: restartToday moves the generation even when storage throws', () => {
  const blocked = vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
    throw new DOMException('blocked', 'SecurityError')
  })
  const { result } = renderHook(() => useTodayGeneration())
  const before = result.current
  act(() => restartToday())
  expect(result.current).toBe(before + 1)
  blocked.mockRestore()
})

it('review: a card moved into a capped cell shows at once, ahead of "+N more"', async () => {
  vi.spyOn(api, 'postAction').mockImplementation(() => new Promise(() => {}))
  const { user } = renderApp('/board', { setup: (s) => s.reset('busy', true) })
  const target = await screen.findByRole('group', { name: 'No epic · Open' })
  expect(within(target).getByRole('button', { name: /^\+\d+ more$/ })).toBeInTheDocument()
  const from = screen.getByRole('group', { name: 'No epic · Backlog' })
  const card = within(from).getAllByTestId(/^card-/)[0]
  const key = card.getAttribute('data-testid')!.slice('card-'.length)
  card.focus()
  await user.keyboard('m')
  await user.click(within(await screen.findByRole('menu', { name: 'Move to' })).getByRole('menuitem', { name: 'Open' }))
  await waitFor(() => expect(within(screen.getByRole('group', { name: 'No epic · Open' })).getAllByTestId(/^card-/)[0]).toHaveAttribute('data-testid', `card-${key}`))
})

it('review: Show opens only the groups the new items landed in; a closed group elsewhere stays closed', async () => {
  const { user } = renderApp('/')
  const row = await screen.findByTestId('card-question:DEMO-0043:Q2')
  await user.click(screen.getByRole('button', { name: /^Approvals/, expanded: true }))
  mockStore.append('DEMO-0044', { type: 'question.asked', actor: 'p_mara', question: 'Q9', def: { id: 'Q9', to: 'p_sev', text: 'Keep the old role?', options: [{ key: 'a', label: 'Yes' }], blocking: false } })
  // Signing an answer refreshes the inbox, as a live arrival does.
  await user.click((await within(row).findAllByRole('radio'))[0])
  await user.click(within(row).getByRole('button', { name: 'Send answer' }))
  await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Send answer' }))
  await user.click(await screen.findByRole('button', { name: '1 new · Show' }))
  expect(await screen.findByTestId('card-question:DEMO-0044:Q9')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /^Approvals/ })).toHaveAttribute('aria-expanded', 'false')
})
