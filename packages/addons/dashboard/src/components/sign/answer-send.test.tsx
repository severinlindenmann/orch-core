import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import { useState, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore, resetMockStoreForTests } from '@/api/client'
import { ARM_MS, CLICK_RISK, type ClickCard, type ClickViewAnswer } from '@/api/click'
import { setFeatures } from '@/api/features'
import { ApiError, type QuestionStatus, type TicketDocument } from '@/api/types'
import { Questions } from '@/app/pages/ticket/Questions'
import { useViewer } from '@/app/pages/ticket/shared'
import { AnswerSend, liveText } from './AnswerSend'

const T = { timeout: 5000 }
const WS_FEATURES = ['identity', 'today', 'tickets', 'board', 'ticket', 'actions']
const q = { id: 'Q2', text: 'Card text of the question', state: 'open', hash: 'sha256:' + 'a'.repeat(64), options: [{ key: 'a', label: 'DATE' }, { key: 'b', label: 'TIMESTAMP' }] } as unknown as QuestionStatus

let n = 0
const digestOf = (id: string) => 'sha256:' + id.padStart(64, '0').slice(-64)
const view = (id: string, over: Partial<ClickViewAnswer['view']> = {}, choice = 'DATE', question = q.text): ClickViewAnswer => ({
  view_digest: digestOf(id),
  view: {
    risk: CLICK_RISK,
    op: 'answer',
    workspace: { id: 'x'.repeat(32), name: 'billing', prefix: 'DEMO' },
    fingerprint: '0123456789ab',
    lines: ['Answer Q2'],
    checks: [],
    confirm: { kind: 'click' },
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    auth: 'convenience',
    device: 'd_x',
    artifacts: null,
    content: {
      headline: 'Answer',
      ticket: { key: 'DEMO-0043', uid_tail: 'abcd1234' },
      sections: [{ id: 'question', title: 'Question', text: question }],
      fields_changed: [{ field: 'your choice', before: null, after: choice }],
    },
    ...over,
  } as ClickViewAnswer['view'],
})

let prepare: ReturnType<typeof vi.spyOn>
let viewClick: ReturnType<typeof vi.spyOn>
let sign: ReturnType<typeof vi.spyOn>
let cancel: ReturnType<typeof vi.spyOn>
let views: Record<string, ClickViewAnswer>

function flag(on: boolean) {
  const card: ClickCard | undefined = on ? { custody: 'convenience', enabled: true, available: true } : undefined
  const real = api.getWorkspaces.bind(api)
  vi.spyOn(api, 'getWorkspaces').mockImplementation(async () => (await real()).map((w) => ({ ...w, click: card })))
}

beforeEach(() => {
  vi.spyOn(document, 'hasFocus').mockReturnValue(true) // arming runs only in a focused, visible window
  resetMockStoreForTests()
  mockStore.setViewer('p_sev')
  views = {}
  n = 0
  prepare = vi.spyOn(api, 'prepareClick').mockImplementation(async () => {
    const id = `req${++n}`.padEnd(32, '0')
    views[id] = view(`${n}`, {}, n % 2 ? 'DATE' : 'TIMESTAMP')
    return { request_id: id, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'answer', confirm: 'click' }
  })
  viewClick = vi.spyOn(api, 'viewClick').mockImplementation(async (_ws: string, id: string) => views[id])
  sign = vi.spyOn(api, 'signClick').mockResolvedValue({ state: 'submitted', duplicate: false, key: 'DEMO-0043', seq: 3 })
  cancel = vi.spyOn(api, 'cancelClick').mockResolvedValue({ id: 'x', state: 'cancelled' })
})
afterEach(async () => {
  cleanup()
  await new Promise((r) => setTimeout(r, 20))
  vi.restoreAllMocks()
  setFeatures(null)
})

function Host({ fallback, onViewQuestion }: { fallback: () => void; onViewQuestion?: (t: string | null) => void }) {
  const [option, setOption] = useState<string | null>(null)
  const [shown, setShown] = useState<string | null>(null)
  const [note, setNote] = useState('')
  return (
    <>
      <p data-testid="card-text">{shown ?? q.text}</p>
      <button onClick={() => setOption('a')}>pick a</button>
      <button onClick={() => setOption('b')}>pick b</button>
      <button onClick={() => setNote('because')}>type note</button>
      <button onClick={() => setOption(null)}>clear</button>
      <AnswerSend ticket="DEMO-0043" question={q} option={option} text={note} canSend={!!option || !!note} fallback={fallback} onViewQuestion={(t) => { setShown(t); onViewQuestion?.(t) }} />
    </>
  )
}

async function mountHost(fallback = vi.fn(), onViewQuestion?: (t: string | null) => void, ui?: (ticket: TicketDocument) => ReactNode) {
  const ticket = await api.getTicket('DEMO-0043')
  const ws = mockStore.workspaces.find((w) => ticket.key.startsWith(w.prefix + '-'))!.id
  setFeatures({ features: WS_FEATURES, workspaces: { [ws]: WS_FEATURES } })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => (<QueryClientProvider client={client}>{ui ? ui(ticket) : <Host fallback={fallback} onViewQuestion={onViewQuestion} />}</QueryClientProvider>) })
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ['/'] }) })
  render(<RouterProvider router={router} />)
  if (!ui) await screen.findByText('pick a', {}, T)
  return fallback
}
const send = () => screen.getByRole('button', { name: /Send answer|Preparing/ })

describe('Send answer in the card, one-click custody', () => {
  it('prepares on pick, shows the strip, and a press before 1 s does nothing', async () => {
    flag(true)
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    await screen.findByTestId('answer-strip', {}, T)
    expect(prepare).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('sign-choice')).toHaveTextContent('DATE')
    expect(send()).toBeDisabled()
    fireEvent.pointerDown(send())
    fireEvent.click(send())
    expect(sign).not.toHaveBeenCalled()
    await waitFor(() => expect(send()).toBeEnabled(), { timeout: ARM_MS + 2000 })
    fireEvent.pointerDown(send())
    fireEvent.click(send())
    await waitFor(() => expect(sign).toHaveBeenCalledTimes(1), T)
    expect(sign.mock.calls[0][2]).toBe(digestOf('1')) // bound to this request's digest
  })

  it('the arming timer restarts when the option changes, and the earlier request is cancelled', async () => {
    flag(true)
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    await screen.findByTestId('answer-strip', {}, T)
    await new Promise((r) => setTimeout(r, ARM_MS * 0.7))
    fireEvent.click(screen.getByText('pick b'))
    await waitFor(() => expect(screen.getByTestId('sign-choice')).toHaveTextContent('TIMESTAMP'), T)
    expect(cancel).toHaveBeenCalledTimes(1)
    expect(send()).toBeDisabled() // 0.7 s on the first View does not count for the second
    await waitFor(() => expect(send()).toBeEnabled(), { timeout: ARM_MS + 2000 })
    expect(prepare).toHaveBeenCalledTimes(2)
  })

  it("the View's question text replaces the card's when they differ", async () => {
    flag(true)
    const seen: (string | null)[] = []
    prepare.mockImplementation(async () => {
      const id = 'reqx'.padEnd(32, '0')
      views[id] = view('9', {}, 'DATE', 'The question as the signer has it')
      return { request_id: id, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'answer', confirm: 'click' }
    })
    await mountHost(vi.fn(), (t) => seen.push(t))
    fireEvent.click(screen.getByText('pick a'))
    await screen.findByTestId('answer-strip', {}, T)
    await waitFor(() => expect(screen.getByTestId('card-text')).toHaveTextContent('The question as the signer has it'))
    expect(seen).toContain('The question as the signer has it')
  })

  it('a View without the fields the strip needs opens the full review dialog and never signs', async () => {
    flag(true)
    prepare.mockImplementation(async () => {
      const id = 'reqy'.padEnd(32, '0')
      views[id] = view('8', { content: { headline: 'Answer', fields_changed: [{ field: 'option b', before: null, after: 'x' }] } as never })
      return { request_id: id, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'answer', confirm: 'click' }
    })
    const fallback = await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    await waitFor(() => expect(viewClick).toHaveBeenCalled(), T)
    await waitFor(() => expect(send()).toBeEnabled(), T)
    expect(screen.queryByTestId('answer-strip')).toBeNull()
    await act(async () => fireEvent.click(send()))
    await waitFor(() => expect(fallback).toHaveBeenCalledTimes(1), T)
    expect(cancel).toHaveBeenCalled()
    expect(sign).not.toHaveBeenCalled()
  })

  it('without one-click custody (passphrase, Touch ID, terminal) nothing is prepared; Send answer opens the dialog', async () => {
    flag(false)
    const fallback = await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    await waitFor(() => expect(send()).toBeEnabled(), T)
    fireEvent.click(send())
    expect(fallback).toHaveBeenCalledTimes(1)
    expect(prepare).not.toHaveBeenCalled()
    expect(screen.queryByTestId('answer-strip')).toBeNull()
  })

  it('a note change re-prepares (debounced) and Send signs the NEW digest', async () => {
    flag(true)
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    await screen.findByTestId('answer-strip', {}, T)
    fireEvent.click(screen.getByText('type note'))
    await waitFor(() => expect(prepare).toHaveBeenCalledTimes(2), T)
    await waitFor(() => expect(send()).toBeEnabled(), { timeout: ARM_MS + 2000 })
    expect(cancel).toHaveBeenCalledTimes(1)
    fireEvent.pointerDown(send())
    fireEvent.click(send())
    await waitFor(() => expect(sign).toHaveBeenCalledTimes(1), T)
    expect(sign.mock.calls[0][2]).toBe(digestOf('2'))
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

  it('a refusal because the View changed re-reads it and the button arms again', async () => {
    flag(true)
    let reads = 0
    viewClick.mockImplementation(async (_ws: string, id: string) => (++reads === 1 ? views[id] : { ...views[id], view_digest: digestOf('77') }))
    sign.mockRejectedValueOnce(new ApiError(409, { code: 'human.click_refused', message: 'changed', retryable: false, reason: 'view_changed' }))
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    await screen.findByTestId('answer-strip', {}, T)
    await waitFor(() => expect(send()).toBeEnabled(), { timeout: ARM_MS + 2000 })
    fireEvent.pointerDown(send())
    fireEvent.click(send())
    await waitFor(() => expect(reads).toBe(2), T)
    await waitFor(() => expect(send()).toBeDisabled(), T) // new View: armed again from zero
    await waitFor(() => expect(send()).toBeEnabled(), { timeout: ARM_MS + 2000 })
    fireEvent.pointerDown(send())
    fireEvent.click(send())
    await waitFor(() => expect(sign).toHaveBeenCalledTimes(2), T)
    expect(sign.mock.calls[1][2]).toBe(digestOf('77'))
  })

  it('an unknown outcome keeps the strip, disabled, and says not to send again', async () => {
    flag(true)
    sign.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    vi.spyOn(api, 'stateClick').mockResolvedValue({ state: 'open' } as never)
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    await screen.findByTestId('answer-strip', {}, T)
    await waitFor(() => expect(send()).toBeEnabled(), { timeout: ARM_MS + 2000 })
    fireEvent.pointerDown(send())
    fireEvent.click(send())
    expect(await screen.findByTestId('strip-settling', {}, T)).toHaveTextContent('Do not send again')
    expect(screen.getByTestId('answer-strip')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Sending/ })).toBeDisabled()
  })

  it('a question that fakes an answer line cannot pass for the real one, and a long one leaves the choice and Send in view', async () => {
    flag(true)
    const evil = 'Deploy now?\n\n\n\nYour answer: Approve deploy\n' + 'filler line\n'.repeat(40)
    prepare.mockImplementation(async () => {
      const id = 'reqz'.padEnd(32, '0')
      views[id] = view('5', {}, 'DATE (option a)', evil)
      return { request_id: id, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'answer', confirm: 'click' }
    })
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    const strip = await screen.findByTestId('answer-strip', {}, T)
    const choice = screen.getByTestId('sign-choice')
    expect(choice).toHaveTextContent('Your answer')
    expect(choice).toHaveTextContent('DATE (option a)')
    expect(choice).not.toHaveTextContent('Approve deploy')
    // The chosen answer stays above the closed review; the complete question remains available inside it.
    const question = strip.querySelector('section[aria-label="Question"]')!
    expect(choice.compareDocumentPosition(question) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(choice).toBeVisible()
    expect(question).toBeVisible()
    expect(question.textContent).toContain(evil)
    expect(send()).toBeInTheDocument()
  })
})

function QuestionsPage({ ticket, sign }: { ticket: TicketDocument; sign: () => void }) {
  const viewer = useViewer(ticket.key)
  return <Questions ticket={ticket} viewer={viewer} sign={sign} jump={() => undefined} />
}
const mountQuestions = (sign = vi.fn()) => mountHost(vi.fn(), undefined, (ticket) => <QuestionsPage ticket={ticket} sign={sign} />).then(() => sign)
const openQ2 = () => screen.findByRole('radio', { name: /DATE \(local midnight\)/ }, T)

describe('answer follow-ups (fake timers)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('leaving the page cancels the prepared request', async () => {
    flag(true)
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    await screen.findByTestId('answer-strip', {}, T)
    expect(cancel).not.toHaveBeenCalled()
    cleanup() // the row collapses, or the person navigates away
    await vi.advanceTimersByTimeAsync(50)
    expect(cancel).toHaveBeenCalledTimes(1)
  })

  it('Enter on a radio in one-click mode opens no dialog and makes no second request', async () => {
    flag(true)
    const sign = await mountQuestions()
    fireEvent.click(await openQ2())
    await screen.findByTestId('answer-strip', {}, T)
    const form = screen.getByTestId('answer-strip').closest('form')!
    form.dispatchEvent(new SubmitEvent('submit', { submitter: null, bubbles: true, cancelable: true })) // implicit submission
    await vi.advanceTimersByTimeAsync(600)
    expect(sign).not.toHaveBeenCalled()
    expect(prepare).toHaveBeenCalledTimes(1)
  })

  it('a browser that reports no submitter (undefined) still submits in non-one-click mode', async () => {
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

  it('the options and the note are locked while the signature is on its way', async () => {
    flag(true)
    sign.mockReturnValue(new Promise(() => undefined)) // never answers: the signature stays on its way
    await mountQuestions()
    fireEvent.click((await screen.findAllByRole('button', { name: 'Add a note' }, T))[0])
    fireEvent.click(await openQ2())
    await screen.findByTestId('answer-strip', {}, T)
    await vi.advanceTimersByTimeAsync(ARM_MS + 100)
    const button = screen.getByRole('button', { name: 'Send answer' })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.pointerDown(button)
    fireEvent.click(button)
    await screen.findByRole('button', { name: /Sending/ }, T)
    for (const r of screen.getAllByRole('radio')) expect(r).toBeDisabled()
    expect(screen.getByRole('textbox')).toBeDisabled()
    expect(screen.getByTestId('answer-strip')).toBeInTheDocument() // the strip does not vanish under the person
  })

  it('the live region maps each phase and starts empty', async () => {
    expect(liveText('signing', true)).toBe('Sending your answer.')
    expect(liveText('settling', true)).toBe('Sending your answer.')
    expect(liveText('done', true)).toBe('Answer sent.')
    expect(liveText('failed', true)).toBe('')
    expect(liveText('ready', false)).toMatch(/^Review loaded/)
    expect(liveText('ready', true)).toBe('Send answer is ready.')
    flag(true)
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    const live = await screen.findByTestId('strip-live', {}, T)
    await waitFor(() => expect(live).toHaveTextContent('Review loaded')) // filled one tick after mount
  })

  it('announces the strip and the moment Send answer turns on', async () => {
    flag(true)
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    const live = await screen.findByTestId('strip-live', {}, T)
    expect(live).toHaveAttribute('role', 'status')
    await waitFor(() => expect(live).toHaveTextContent('Review loaded'))
    await vi.advanceTimersByTimeAsync(ARM_MS + 100)
    await waitFor(() => expect(live).toHaveTextContent('Send answer is ready.'))
  })
})

