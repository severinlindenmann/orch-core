import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { appendItems, focusApi, type Transcript } from '@/api/focus'
import type { Run } from '@/api/runs'
import fixture from '@/mocks/addons/focus/transcript.json'
import { Chat } from './Chat'

const run = (over: Partial<Run> = {}): Run => ({
  run: 'r_01J0000000000000000000000A', rev: 1, kind: 'agent', ticket: 'DEMO-0043', harness: 'claude-code', mode: 'work', model: null,
  as_person: false, state: 'running', started_at: '2026-10-26T10:00:00Z', viewers: 0, writer: false, cols: 100, rows: 30,
  replay_until: null, label: null, label_hidden: false, cwd: {}, exit: null, ...over,
})
const page = (over: Partial<Transcript> = {}): Transcript => ({ ...(fixture as Transcript), ...over })
function show(props: Partial<Parameters<typeof Chat>[0]> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onSend = vi.fn(() => true)
  const onTake = vi.fn()
  const user = userEvent.setup()
  render(<QueryClientProvider client={client}><Chat ws="w1" run={run()} canSend onSend={onSend} onTake={onTake} {...props} /></QueryClientProvider>)
  return { user, onSend, onTake }
}
afterEach(() => vi.restoreAllMocks())

it('draws messages, tool rows, questions and status as plain text', async () => {
  vi.spyOn(focusApi, 'transcript').mockResolvedValue(page())
  show()
  const log = await screen.findByRole('log')
  expect(await within(log).findByText('Rate-limit the invite endpoint. Keep the API contract.')).toBeInTheDocument()
  expect(within(log).getByText(/Edit src\/api\/invite.py/)).toHaveAttribute('data-kind', 'tool')
  const question = within(log).getByText('Where should the limit apply?').closest('[data-kind=question]')!
  expect(within(question as HTMLElement).getByText('Per user')).toBeInTheDocument()
  expect(within(log).getByText('turn complete')).toBeInTheDocument()
})

it('never renders agent text as markup', async () => {
  const items = [{ id: 'x', role: 'assistant' as const, kind: 'text' as const, text: '<img src=x onerror=alert(1)> **b**', ts: null }]
  vi.spyOn(focusApi, 'transcript').mockResolvedValue(page({ items }))
  show()
  const log = await screen.findByRole('log')
  expect(await within(log).findByText('<img src=x onerror=alert(1)> **b**')).toBeInTheDocument()
  expect(log.querySelector('img')).toBeNull()
})

it('asks again from the cursor and appends only new items', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  const read = vi.spyOn(focusApi, 'transcript')
  read.mockResolvedValueOnce(page({ items: fixture.items.slice(0, 2) as Transcript['items'], next: 'o:10', eof: false }))
  read.mockResolvedValue(page({ items: fixture.items.slice(1, 3) as Transcript['items'], next: 'o:20', eof: true }))
  show()
  await screen.findByText(/I read the handler/)
  await vi.advanceTimersByTimeAsync(1600)
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
  expect(read.mock.calls[0][2]).toEqual({ after: null, child: undefined })
  expect(read.mock.calls[1][2]).toEqual({ after: 'o:10', child: undefined })
  expect(screen.getAllByText(/I read the handler/)).toHaveLength(1)
  expect(await screen.findByText(/Edit src\/api\/invite.py/)).toBeInTheDocument()
  vi.useRealTimers()
})

it('says why there is no chat yet', async () => {
  vi.spyOn(focusApi, 'transcript').mockResolvedValue(page({ available: false, reason: 'transcript not found yet', items: [], next: null }))
  show()
  expect(await screen.findByText('Waiting for the first message...')).toBeInTheDocument()
})

it('sends the message through the terminal and clears the box', async () => {
  vi.spyOn(focusApi, 'transcript').mockResolvedValue(page())
  const { user, onSend } = show()
  await screen.findByRole('log')
  await user.type(screen.getByRole('textbox', { name: 'Message the agent' }), 'use per workspace{Enter}')
  expect(onSend).toHaveBeenCalledWith('use per workspace')
  expect(screen.getByRole('textbox', { name: 'Message the agent' })).toHaveValue('')
})

it('keeps the message and offers control when the terminal is read only', async () => {
  vi.spyOn(focusApi, 'transcript').mockResolvedValue(page())
  const { user, onSend, onTake } = show({ canSend: false })
  await screen.findByRole('log')
  await user.type(screen.getByRole('textbox', { name: 'Message the agent' }), 'hello')
  expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  await user.click(screen.getByRole('button', { name: 'Take control' }))
  expect(onTake).toHaveBeenCalled()
  expect(onSend).not.toHaveBeenCalled()
})

it('keeps the text when the terminal refuses it', async () => {
  vi.spyOn(focusApi, 'transcript').mockResolvedValue(page())
  const { user } = show({ onSend: () => false })
  await screen.findByRole('log')
  await user.type(screen.getByRole('textbox', { name: 'Message the agent' }), 'hello{Enter}')
  expect(screen.getByRole('textbox', { name: 'Message the agent' })).toHaveValue('hello')
  expect(screen.getByRole('alert')).toHaveTextContent('not sent')
})

it('has no composer for an ended run and stops polling', async () => {
  const read = vi.spyOn(focusApi, 'transcript').mockResolvedValue(page())
  show({ run: run({ state: 'exited' }) })
  await screen.findByRole('log')
  expect(screen.getByRole('textbox', { name: 'Message the agent' })).toBeDisabled()
  expect(read).toHaveBeenCalledTimes(1)
})

it('appendItems skips ids that are already shown', () => {
  const a = fixture.items as Transcript['items']
  expect(appendItems(a.slice(0, 2), a.slice(1, 4)).map((i) => i.id)).toEqual(a.slice(0, 4).map((i) => i.id))
  expect(appendItems(a, [])).toBe(a)
})
