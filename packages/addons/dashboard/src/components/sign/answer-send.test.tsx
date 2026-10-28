import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import { useState, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore, resetMockStoreForTests } from '@/api/client'
import { ARM_MS, CLICK_RISK, type ClickCard, type ClickViewAnswer } from '@/api/click'
import { setFeatures } from '@/api/features'
import { ApiError, type NeedsYouItem, type QuestionStatus, type TicketDocument } from '@/api/types'
import { Questions } from '@/app/pages/ticket/Questions'
import { useViewer } from '@/app/pages/ticket/shared'
import { QuestionRow } from '@/app/pages/today/rows'
import { AnswerSend, liveText } from './AnswerSend'
import { ClickSignPanel } from './ClickSignPanel'
import * as gesture from './trustedPress'

const T = { timeout: 5000 }
const WS_FEATURES = ['identity', 'today', 'tickets', 'board', 'ticket', 'actions']
const q = { id: 'Q2', text: 'Card text of the question', state: 'open', hash: 'sha256:' + 'a'.repeat(64), options: [{ key: 'a', label: 'DATE' }, { key: 'b', label: 'TIMESTAMP' }] } as unknown as QuestionStatus
const WS_ID = '0123456789abcdef0123456789abcdef'
const realTrusted = gesture.trustedPress

let n = 0
const digestOf = (id: string) => 'sha256:' + id.padStart(64, '0').slice(-64)
const view = (id: string, over: Partial<ClickViewAnswer['view']> = {}, choice = 'DATE', question = q.text): ClickViewAnswer => ({
  view_digest: digestOf(id),
  view: {
    risk: CLICK_RISK,
    op: 'answer',
    workspace: { id: WS_ID, name: 'billing', prefix: 'DEMO' },
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

/** The workspaces with (or without) a click-ready card; `only` keeps the ticket's workspace alone. */
function flag(on: boolean, only = false) {
  const card: ClickCard | undefined = on ? { custody: 'convenience', enabled: true, available: true } : undefined
  const real = api.getWorkspaces.bind(api)
  vi.spyOn(api, 'getWorkspaces').mockImplementation(async () => (await real()).filter((w) => !only || w.prefix === 'DEMO').slice(0, only ? 1 : undefined).map((w) => ({ ...w, click: card })))
}
/** Every press from here on is a trusted, non-repeat gesture, judged by the real rule (a key repeat still blocks it). */
const trusted = () => vi.spyOn(gesture, 'trustedPress').mockImplementation((e) => realTrusted({ isTrusted: true, repeat: e.repeat }))

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

function Host({ fallback, onViewQuestion }: { fallback?: () => void; onViewQuestion?: (t: string | null) => void }) {
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
  setFeatures({ features: WS_FEATURES, workspaces: { [ws]: WS_FEATURES } }) // a real host
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => (<QueryClientProvider client={client}>{ui ? ui(ticket) : <Host fallback={fallback} onViewQuestion={onViewQuestion} />}</QueryClientProvider>) })
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ['/'] }) })
  const rendered = render(<RouterProvider router={router} />)
  if (!ui) await screen.findByText('pick a', {}, T)
  return { fallback, ...rendered }
}
const send = () => screen.getByRole('button', { name: /Send answer|Preparing/ })
/** A real press: pointer down then click (the strip's Send counts a press only if it began while armed). */
const press = (b: HTMLElement = send()) => { fireEvent.pointerDown(b); fireEvent.click(b) }
const armed = () => waitFor(() => expect(send()).toBeEnabled(), { timeout: ARM_MS + 2000 })

describe('Send answer in the card, one click (C2: prepare only on a trusted press)', () => {
  it('a question is answered with one click: pick, press Send answer, the strip, the armed press signs once with its digest', async () => {
    flag(true)
    trusted()
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    await waitFor(() => expect(send()).toBeEnabled(), T)
    expect(prepare).not.toHaveBeenCalled() // the pick alone prepares nothing
    press()
    const strip = await screen.findByTestId('answer-strip', {}, T)
    expect(prepare).toHaveBeenCalledTimes(1)
    expect(within(strip).getByRole('heading', { name: 'You are answering' })).toBeInTheDocument()
    expect(screen.getByTestId('sign-choice')).toHaveTextContent('DATE')
    expect(screen.getByTestId('strip-workspace')).toHaveTextContent(`DEMO · ${WS_ID.slice(-8)}`)
    expect(screen.getByTestId('strip-technical')).toBeInTheDocument() // the technical facts sit behind the small "i"
    expect(screen.queryByTestId('click-fingerprint')).toBeNull()
    expect(send()).toBeDisabled()
    press() // before 1 s: nothing
    expect(sign).not.toHaveBeenCalled()
    await armed()
    press()
    await waitFor(() => expect(sign).toHaveBeenCalledTimes(1), T)
    expect(sign.mock.calls[0][2]).toBe(digestOf('1')) // bound to this request's digest
    expect(prepare).toHaveBeenCalledTimes(1)
  })

  it('nothing is prepared on render, pick, note typing or a synthetic (untrusted) click', async () => {
    flag(true)
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    fireEvent.click(screen.getByText('type note'))
    fireEvent.click(screen.getByText('pick b'))
    await new Promise((r) => setTimeout(r, 600))
    expect(prepare).not.toHaveBeenCalled()
    await waitFor(() => expect(send()).toBeEnabled(), T)
    press() // jsdom events are not trusted: the real rule refuses them
    await new Promise((r) => setTimeout(r, 100))
    expect(prepare).not.toHaveBeenCalled()
    expect(screen.queryByTestId('answer-strip')).toBeNull()
  })

  it('a key auto-repeat prepares nothing; the next clean press prepares once', async () => {
    flag(true)
    trusted()
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    await waitFor(() => expect(send()).toBeEnabled(), T)
    fireEvent.keyDown(document, { key: 'Enter', repeat: true })
    fireEvent.click(send())
    await new Promise((r) => setTimeout(r, 100))
    expect(prepare).not.toHaveBeenCalled()
    fireEvent.keyUp(document, { key: 'Enter' })
    press()
    await screen.findByTestId('answer-strip', {}, T)
    expect(prepare).toHaveBeenCalledTimes(1)
  })

  it('the armed strip never signs on a synthetic (untrusted) press; the next trusted press signs once', async () => {
    flag(true)
    const real = trusted()
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    await waitFor(() => expect(send()).toBeEnabled(), T)
    press()
    await screen.findByTestId('answer-strip', {}, T)
    await armed()
    real.mockRestore() // from here jsdom's own untrusted events are judged by the real rule
    press()
    fireEvent.keyDown(send(), { key: 'Enter' })
    fireEvent.click(send())
    await new Promise((r) => setTimeout(r, 100))
    expect(sign).not.toHaveBeenCalled()
    trusted()
    press()
    await waitFor(() => expect(sign).toHaveBeenCalledTimes(1), T)
    await new Promise((r) => setTimeout(r, 100))
    expect(sign).toHaveBeenCalledTimes(1)
  })

  it('the armed strip never signs on a key auto-repeat; the next clean press signs once', async () => {
    flag(true)
    trusted()
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    await waitFor(() => expect(send()).toBeEnabled(), T)
    press()
    await screen.findByTestId('answer-strip', {}, T)
    await armed()
    fireEvent.keyDown(send(), { key: 'Enter' }) // began armed
    fireEvent.keyDown(document, { key: 'Enter', repeat: true }) // then held: the click lands during auto-repeat
    fireEvent.click(send())
    await new Promise((r) => setTimeout(r, 100))
    expect(sign).not.toHaveBeenCalled()
    fireEvent.keyUp(document, { key: 'Enter' })
    press()
    await waitFor(() => expect(sign).toHaveBeenCalledTimes(1), T)
  })

  it('Cancel tombstones the request: it is never signed, and the next press is a new attempt with a new digest', async () => {
    flag(true)
    trusted()
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    await screen.findByTestId('answer-strip', {}, T)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1), T)
    expect(cancel.mock.calls[0][1]).toBe('req1'.padEnd(32, '0'))
    expect(await screen.findByTestId('answer-cancelled', {}, T)).toHaveTextContent('Nothing was signed')
    expect(screen.queryByTestId('answer-strip')).toBeNull()
    expect(prepare).toHaveBeenCalledTimes(1) // nothing comes back by itself
    press()
    await screen.findByTestId('answer-strip', {}, T)
    expect(prepare).toHaveBeenCalledTimes(2)
    expect(prepare.mock.calls[1][2]).toBeGreaterThan(prepare.mock.calls[0][2] as number) // a new attempt, not the old intent
    await armed()
    press()
    await waitFor(() => expect(sign).toHaveBeenCalledTimes(1), T)
    expect(sign.mock.calls[0][1]).toBe('req2'.padEnd(32, '0'))
    expect(sign.mock.calls[0][2]).toBe(digestOf('2'))
  })

  it('a changed option cancels the open request, prepares nothing by itself, and the next press signs the NEW answer', async () => {
    flag(true)
    trusted()
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    await screen.findByTestId('answer-strip', {}, T)
    await new Promise((r) => setTimeout(r, ARM_MS * 0.7))
    fireEvent.click(screen.getByText('pick b'))
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1), T)
    await waitFor(() => expect(screen.queryByTestId('answer-strip')).toBeNull(), T)
    await new Promise((r) => setTimeout(r, 600))
    expect(prepare).toHaveBeenCalledTimes(1)
    press()
    await waitFor(() => expect(screen.getByTestId('sign-choice')).toHaveTextContent('TIMESTAMP'), T)
    expect(send()).toBeDisabled() // 0.7 s on the first View does not count for the second
    await armed()
    press()
    await waitFor(() => expect(sign).toHaveBeenCalledTimes(1), T)
    expect(sign.mock.calls[0][2]).toBe(digestOf('2'))
  })

  it('a note typed after the strip cancels the request; the next press prepares the answer with the note', async () => {
    flag(true)
    trusted()
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    await screen.findByTestId('answer-strip', {}, T)
    fireEvent.click(screen.getByText('type note'))
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1), T)
    await new Promise((r) => setTimeout(r, 600))
    expect(prepare).toHaveBeenCalledTimes(1)
    press()
    await waitFor(() => expect(prepare).toHaveBeenCalledTimes(2), T)
    expect(prepare.mock.calls[1][1]).toMatchObject({ action: 'answer', option: 'a', message: 'because' })
    await armed()
    press()
    await waitFor(() => expect(sign).toHaveBeenCalledTimes(1), T)
    expect(sign.mock.calls[0][2]).toBe(digestOf('2'))
  })

  it("the View's question text replaces the card's when they differ", async () => {
    flag(true)
    trusted()
    const seen: (string | null)[] = []
    prepare.mockImplementation(async () => {
      const id = 'reqx'.padEnd(32, '0')
      views[id] = view('9', {}, 'DATE', 'The question as the signer has it')
      return { request_id: id, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'answer', confirm: 'click' }
    })
    await mountHost(vi.fn(), (t) => seen.push(t))
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    await screen.findByTestId('answer-strip', {}, T)
    await waitFor(() => expect(screen.getByTestId('card-text')).toHaveTextContent('The question as the signer has it'))
    expect(seen).toContain('The question as the signer has it')
  })

  it('a View without the fields the strip needs shows the full review in place, armed the same way, never a dialog', async () => {
    flag(true)
    trusted()
    prepare.mockImplementation(async () => {
      const id = 'reqy'.padEnd(32, '0')
      views[id] = view('8', { content: { headline: 'Answer', fields_changed: [{ field: 'option b', before: null, after: 'x' }] } as never })
      return { request_id: id, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'answer', confirm: 'click' }
    })
    const { fallback } = await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    const panel = await screen.findByTestId('click-panel', {}, T)
    expect(screen.queryByTestId('answer-strip')).toBeNull()
    expect(within(panel).getByTestId('sign-workspace')).toHaveTextContent(`DEMO · ${WS_ID.slice(-8)}`)
    expect(send()).toBeDisabled()
    await armed()
    press()
    await waitFor(() => expect(sign).toHaveBeenCalledTimes(1), T)
    expect(sign.mock.calls[0][2]).toBe(digestOf('8'))
    expect(fallback).not.toHaveBeenCalled()
  })

  it('not click-ready: one plain line with one button, nothing prepared, no dialog', async () => {
    flag(false)
    trusted()
    const { fallback } = await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    const line = await screen.findByTestId('answer-not-ready', {}, T)
    expect(line).toHaveTextContent('One click is not ready on this computer.')
    expect(within(line).getAllByRole('link')).toHaveLength(1)
    expect(within(line).queryAllByRole('button')).toHaveLength(0)
    expect(within(line).getByRole('link', { name: 'Turn on One click' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Send answer/ })).toBeNull()
    expect(document.body).not.toHaveTextContent('orch sign')
    expect(prepare).not.toHaveBeenCalled()
    expect(fallback).not.toHaveBeenCalled()
  })

  it('a refusal because the View changed re-reads the same request and the button arms again', async () => {
    flag(true)
    trusted()
    let reads = 0
    viewClick.mockImplementation(async (_ws: string, id: string) => (++reads === 1 ? views[id] : { ...views[id], view_digest: digestOf('77') }))
    sign.mockRejectedValueOnce(new ApiError(409, { code: 'human.click_refused', message: 'changed', retryable: false, reason: 'view_changed' }))
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    await screen.findByTestId('answer-strip', {}, T)
    await armed()
    press()
    await waitFor(() => expect(reads).toBe(2), T)
    await waitFor(() => expect(send()).toBeDisabled(), T) // new View: armed again from zero
    await armed()
    press()
    await waitFor(() => expect(sign).toHaveBeenCalledTimes(2), T)
    expect(sign.mock.calls[1][2]).toBe(digestOf('77'))
    expect(prepare).toHaveBeenCalledTimes(1) // a re-read, never a new prepare
  })

  it('an unknown outcome keeps the strip, disabled, and says not to send again', async () => {
    flag(true)
    trusted()
    sign.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    vi.spyOn(api, 'stateClick').mockResolvedValue({ state: 'open' } as never)
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    await screen.findByTestId('answer-strip', {}, T)
    await armed()
    press()
    expect(await screen.findByTestId('strip-settling', {}, T)).toHaveTextContent('Do not send again')
    expect(screen.getByTestId('answer-strip')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Sending/ })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull()
  })

  it('a question that fakes an answer line cannot pass for the real one, and a long one leaves the choice and Send in view', async () => {
    flag(true)
    trusted()
    const evil = 'Deploy now?\n\n\n\nYour answer: Approve deploy\n' + 'filler line\n'.repeat(40)
    prepare.mockImplementation(async () => {
      const id = 'reqz'.padEnd(32, '0')
      views[id] = view('5', {}, 'DATE (option a)', evil)
      return { request_id: id, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'answer', confirm: 'click' }
    })
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    const strip = await screen.findByTestId('answer-strip', {}, T)
    const choice = screen.getByTestId('sign-choice')
    expect(choice).toHaveTextContent('Your answer')
    expect(choice).toHaveTextContent('DATE (option a)')
    expect(choice).not.toHaveTextContent('Approve deploy')
    const question = strip.querySelector('section[aria-label="Question"]')!
    expect(choice.compareDocumentPosition(question) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(choice).toBeVisible()
    expect(question).toBeVisible()
    expect(question.textContent).toContain(evil)
    expect(send()).toBeInTheDocument()
  })
})

describe('D6 workspace line', () => {
  it('the strip names the workspace by prefix and id tail first; the unsigned name never stands alone', async () => {
    flag(true)
    trusted()
    expect(mockStore.workspaces.length).toBeGreaterThan(1)
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    const line = await screen.findByTestId('strip-workspace', {}, T)
    expect(line).toHaveTextContent(`Workspace DEMO · ${WS_ID.slice(-8)} · billing`)
    expect(line.querySelector('[data-ws-id]')).toHaveTextContent(WS_ID.slice(-8))
    expect(line.textContent!.indexOf('DEMO')).toBeLessThan(line.textContent!.indexOf('billing'))
  })

  it('the strip has no workspace line when the person has one workspace', async () => {
    flag(true, true)
    trusted()
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    await screen.findByTestId('answer-strip', {}, T)
    expect(screen.queryByTestId('strip-workspace')).toBeNull()
  })

  it('the generic click review shows the same line with more than one workspace, and none with one', () => {
    const a = view('3')
    const { rerender } = render(<ClickSignPanel answer={a} problem={null} signing={false} onSign={vi.fn()} onCancel={vi.fn()} manyWorkspaces />)
    expect(screen.getByTestId('sign-workspace')).toHaveTextContent(`Workspace DEMO · ${WS_ID.slice(-8)} · billing`)
    rerender(<ClickSignPanel answer={a} problem={null} signing={false} onSign={vi.fn()} onCancel={vi.fn()} manyWorkspaces={false} />)
    expect(screen.queryByTestId('sign-workspace')).toBeNull()
  })
})

function QuestionsPage({ ticket, sign }: { ticket: TicketDocument; sign: () => void }) {
  const viewer = useViewer(ticket.key)
  return <Questions ticket={ticket} viewer={viewer} sign={sign} jump={() => undefined} />
}
const mountQuestions = (sign = vi.fn()) => mountHost(vi.fn(), undefined, (ticket) => <QuestionsPage ticket={ticket} sign={sign} />).then(() => sign)
const openQ2 = () => screen.findByRole('radio', { name: /DATE \(local midnight\)/ }, T)

describe('both callers answer with one click', () => {
  it('Today card: pick, press Send answer, the strip, the armed press signs once', async () => {
    flag(true)
    trusted()
    const rowSign = vi.fn()
    const item = { kind: 'question', ticket: 'DEMO-0043', title: 'Ticket', text: 'Card text of the question', since: new Date().toISOString(), ref: 'Q2', blocking: true } as NeedsYouItem
    await mountHost(vi.fn(), undefined, (ticket) => <ul><QuestionRow item={item} ticket={ticket} now={new Date().toISOString()} expanded onToggle={() => undefined} sign={rowSign} /></ul>)
    fireEvent.click(await openQ2())
    await waitFor(() => expect(send()).toBeEnabled(), T)
    expect(prepare).not.toHaveBeenCalled()
    press()
    await screen.findByTestId('answer-strip', {}, T)
    expect(screen.getByTestId('strip-workspace')).toHaveTextContent(`DEMO · ${WS_ID.slice(-8)}`)
    await armed()
    press()
    await waitFor(() => expect(sign).toHaveBeenCalledTimes(1), T)
    expect(prepare).toHaveBeenCalledTimes(1)
    expect(rowSign).not.toHaveBeenCalled() // no dialog
  })

  it('ticket page: pick, press Send answer, the strip, the armed press signs once', async () => {
    flag(true)
    trusted()
    const pageSign = await mountQuestions()
    fireEvent.click(await openQ2())
    await waitFor(() => expect(send()).toBeEnabled(), T)
    expect(prepare).not.toHaveBeenCalled()
    press()
    await screen.findByTestId('answer-strip', {}, T)
    await armed()
    press()
    await waitFor(() => expect(sign).toHaveBeenCalledTimes(1), T)
    expect(pageSign).not.toHaveBeenCalled()
  })
})

describe('answer follow-ups (fake timers)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('leaving the page cancels the prepared request', async () => {
    flag(true)
    trusted()
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    await screen.findByTestId('answer-strip', {}, T)
    expect(cancel).not.toHaveBeenCalled()
    cleanup() // the row collapses, or the person navigates away
    await vi.advanceTimersByTimeAsync(50)
    expect(cancel).toHaveBeenCalledTimes(1)
  })

  it('an expired View is cancelled, never signed, and nothing is prepared until the next press', async () => {
    flag(true)
    trusted()
    prepare.mockImplementation(async () => {
      const id = `req${++n}`.padEnd(32, '0')
      views[id] = view(`${n}`, { expires_at: new Date(Date.now() + 3000).toISOString() })
      return { request_id: id, expires_at: new Date(Date.now() + 3000).toISOString(), op: 'answer', confirm: 'click' }
    })
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    await screen.findByTestId('answer-strip', {}, T)
    await vi.advanceTimersByTimeAsync(3500)
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1), T)
    expect(await screen.findByTestId('answer-cancelled', {}, T)).toBeInTheDocument()
    await vi.advanceTimersByTimeAsync(1000)
    expect(prepare).toHaveBeenCalledTimes(1)
    expect(sign).not.toHaveBeenCalled()
  })

  it('Enter in the ticket form (implicit submission) prepares nothing and opens no dialog', async () => {
    flag(true)
    trusted()
    const pageSign = await mountQuestions()
    const radio = await openQ2()
    fireEvent.click(radio)
    const form = radio.closest('form')!
    form.dispatchEvent(new SubmitEvent('submit', { submitter: null, bubbles: true, cancelable: true }))
    fireEvent.submit(form) // a browser without `submitter`
    await vi.advanceTimersByTimeAsync(600)
    expect(pageSign).not.toHaveBeenCalled()
    expect(prepare).not.toHaveBeenCalled()
  })

  it('the options and the note are locked while the signature is on its way', async () => {
    flag(true)
    trusted()
    sign.mockReturnValue(new Promise(() => undefined)) // never answers: the signature stays on its way
    await mountQuestions()
    fireEvent.click((await screen.findAllByRole('button', { name: 'Add a note' }, T))[0])
    fireEvent.click(await openQ2())
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    await screen.findByTestId('answer-strip', {}, T)
    await vi.advanceTimersByTimeAsync(ARM_MS + 100)
    const button = screen.getByRole('button', { name: 'Send answer' })
    await waitFor(() => expect(button).toBeEnabled())
    press(button)
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
    trusted()
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    const live = await screen.findByTestId('strip-live', {}, T)
    await waitFor(() => expect(live).toHaveTextContent('Review loaded')) // filled one tick after mount
  })

  it('announces the strip and the moment Send answer turns on', async () => {
    flag(true)
    trusted()
    await mountHost()
    fireEvent.click(screen.getByText('pick a'))
    press(await waitFor(() => { const b = send(); expect(b).toBeEnabled(); return b }, T))
    const live = await screen.findByTestId('strip-live', {}, T)
    expect(live).toHaveAttribute('role', 'status')
    await waitFor(() => expect(live).toHaveTextContent('Review loaded'))
    await vi.advanceTimersByTimeAsync(ARM_MS + 100)
    await waitFor(() => expect(live).toHaveTextContent('Send answer is ready.'))
  })
})

describe('mockup (no real host): the simulated dialog stays', () => {
  it('Send answer opens the mockup dialog and prepares nothing', async () => {
    const fallback = vi.fn()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const root = createRootRoute({ component: () => <QueryClientProvider client={client}><Host fallback={fallback} /></QueryClientProvider> })
    render(<RouterProvider router={createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ['/'] }) })} />)
    fireEvent.click(await screen.findByText('pick a', {}, T))
    await waitFor(() => expect(send()).toBeEnabled(), T)
    await act(async () => fireEvent.click(send()))
    expect(fallback).toHaveBeenCalledTimes(1)
    expect(prepare).not.toHaveBeenCalled()
  })
})
