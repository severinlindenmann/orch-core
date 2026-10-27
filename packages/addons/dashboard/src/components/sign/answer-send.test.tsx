import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import { useState, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore, resetMockStoreForTests } from '@/api/client'
import type { ClickCard } from '@/api/click'
import { setFeatures } from '@/api/features'
import type { QuestionStatus, TicketDocument } from '@/api/types'
import { Questions } from '@/app/pages/ticket/Questions'
import { useViewer } from '@/app/pages/ticket/shared'
import { AnswerSend } from './AnswerSend'

// The card never prepares: a pick or a note is not a trusted press of an outcome button. Send answer opens the
// review dialog, which prepares on its own outcome press and signs only after its 1 s arm.
const T = { timeout: 5000 }
const WS_FEATURES = ['identity', 'today', 'tickets', 'board', 'ticket', 'actions']
const q = { id: 'Q2', text: 'Card text of the question', state: 'open', hash: 'sha256:' + 'a'.repeat(64), options: [{ key: 'a', label: 'DATE' }, { key: 'b', label: 'TIMESTAMP' }] } as unknown as QuestionStatus

let prepare: ReturnType<typeof vi.spyOn>
let viewClick: ReturnType<typeof vi.spyOn>
let sign: ReturnType<typeof vi.spyOn>

function flag(on: boolean) {
  const card: ClickCard | undefined = on ? { custody: 'convenience', enabled: true, available: true } : undefined
  const real = api.getWorkspaces.bind(api)
  vi.spyOn(api, 'getWorkspaces').mockImplementation(async () => (await real()).map((w) => ({ ...w, click: card })))
}

beforeEach(() => {
  resetMockStoreForTests()
  mockStore.setViewer('p_sev')
  prepare = vi.spyOn(api, 'prepareClick')
  viewClick = vi.spyOn(api, 'viewClick')
  sign = vi.spyOn(api, 'signClick')
})
afterEach(async () => {
  cleanup()
  await new Promise((r) => setTimeout(r, 20))
  vi.restoreAllMocks()
  setFeatures(null)
})

function Host({ fallback }: { fallback: () => void }) {
  const [option, setOption] = useState<string | null>(null)
  const [note, setNote] = useState('')
  return (
    <>
      <button onClick={() => setOption('a')}>pick a</button>
      <button onClick={() => setOption('b')}>pick b</button>
      <button onClick={() => setNote('because')}>type note</button>
      <AnswerSend ticket="DEMO-0043" question={q} option={option} text={note} canSend={!!option || !!note} fallback={fallback} />
    </>
  )
}

async function mountHost(fallback = vi.fn(), ui?: (ticket: TicketDocument) => ReactNode) {
  const ticket = await api.getTicket('DEMO-0043')
  const ws = mockStore.workspaces.find((w) => ticket.key.startsWith(w.prefix + '-'))!.id
  setFeatures({ features: WS_FEATURES, workspaces: { [ws]: WS_FEATURES } })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => (<QueryClientProvider client={client}>{ui ? ui(ticket) : <Host fallback={fallback} />}</QueryClientProvider>) })
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ['/'] }) })
  render(<RouterProvider router={router} />)
  if (!ui) await screen.findByText('pick a', {}, T)
  return fallback
}
const send = () => screen.getByRole('button', { name: 'Send answer' })

describe('Send answer in the card', () => {
  it('with one-click custody a pick prepares nothing; Send answer opens the review dialog and never signs here', async () => {
    flag(true)
    const fallback = await mountHost()
    expect(send()).toBeDisabled()
    fireEvent.click(screen.getByText('pick a'))
    fireEvent.click(screen.getByText('pick b'))
    await waitFor(() => expect(send()).toBeEnabled(), T)
    await new Promise((r) => setTimeout(r, 600))
    expect(prepare).not.toHaveBeenCalled()
    expect(viewClick).not.toHaveBeenCalled()
    expect(screen.queryByTestId('answer-strip')).toBeNull()
    fireEvent.click(send())
    expect(fallback).toHaveBeenCalledTimes(1)
    expect(prepare).not.toHaveBeenCalled()
    expect(sign).not.toHaveBeenCalled()
  })

  it('without one-click custody nothing is prepared either; Send answer opens the dialog', async () => {
    flag(false)
    const fallback = await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    await waitFor(() => expect(send()).toBeEnabled(), T)
    fireEvent.click(send())
    expect(fallback).toHaveBeenCalledTimes(1)
    expect(prepare).not.toHaveBeenCalled()
    expect(screen.queryByTestId('answer-strip')).toBeNull()
  })

  it('a note alone prepares nothing: Send answer is the dialog', async () => {
    flag(true)
    const fallback = await mountHost()
    fireEvent.click(screen.getByText('type note'))
    await new Promise((r) => setTimeout(r, 600))
    expect(prepare).not.toHaveBeenCalled()
    fireEvent.click(send())
    expect(fallback).toHaveBeenCalledTimes(1)
  })
})

function QuestionsPage({ ticket, sign }: { ticket: TicketDocument; sign: () => void }) {
  const viewer = useViewer(ticket.key)
  return <Questions ticket={ticket} viewer={viewer} sign={sign} jump={() => undefined} />
}
const mountQuestions = (sign = vi.fn()) => mountHost(vi.fn(), (ticket) => <QuestionsPage ticket={ticket} sign={sign} />).then(() => sign)
const openQ2 = () => screen.findByRole('radio', { name: /DATE \(local midnight\)/ }, T)

describe('answering on the ticket page', () => {
  it('in one-click mode a radio pick prepares nothing and Send answer submits the form to the dialog', async () => {
    flag(true)
    const sign = await mountQuestions()
    fireEvent.click(await openQ2())
    const button = await screen.findByRole('button', { name: 'Send answer' })
    await waitFor(() => expect(button).toBeEnabled())
    await new Promise((r) => setTimeout(r, 600))
    expect(prepare).not.toHaveBeenCalled()
    fireEvent.click(button)
    expect(sign).toHaveBeenCalledTimes(1)
    expect(prepare).not.toHaveBeenCalled()
  })

  it('a browser that reports no submitter (undefined) still submits', async () => {
    flag(false)
    const sign = await mountQuestions()
    fireEvent.click(await openQ2())
    const button = await screen.findByRole('button', { name: 'Send answer' })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.submit(button.closest('form')!) // a plain Event: submitter is undefined
    expect(sign).toHaveBeenCalledTimes(1)
  })

  it('without one click the plain Send answer button still submits the form', async () => {
    flag(false)
    const sign = await mountQuestions()
    fireEvent.click(await openQ2())
    const button = await screen.findByRole('button', { name: 'Send answer' })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    expect(sign).toHaveBeenCalledTimes(1)
  })
})
