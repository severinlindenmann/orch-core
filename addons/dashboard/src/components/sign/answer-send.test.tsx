import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore, resetMockStoreForTests } from '@/api/client'
import { ARM_MS, CLICK_RISK, type ClickCard, type ClickViewAnswer } from '@/api/click'
import { setFeatures } from '@/api/features'
import type { QuestionStatus } from '@/api/types'
import { AnswerSend } from './AnswerSend'

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
  return (
    <>
      <p data-testid="card-text">{shown ?? q.text}</p>
      <button onClick={() => setOption('a')}>pick a</button>
      <button onClick={() => setOption('b')}>pick b</button>
      <AnswerSend ticket="DEMO-0043" question={q} option={option} canSend={!!option} fallback={fallback} onViewQuestion={(t) => { setShown(t); onViewQuestion?.(t) }} />
    </>
  )
}

async function mountHost(fallback = vi.fn(), onViewQuestion?: (t: string | null) => void) {
  const ticket = await api.getTicket('DEMO-0043')
  const ws = mockStore.workspaces.find((w) => ticket.key.startsWith(w.prefix + '-'))!.id
  setFeatures({ features: WS_FEATURES, workspaces: { [ws]: WS_FEATURES } })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => (<QueryClientProvider client={client}><Host fallback={fallback} onViewQuestion={onViewQuestion} /></QueryClientProvider>) })
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ['/'] }) })
  render(<RouterProvider router={router} />)
  await screen.findByText('pick a', {}, T)
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
})
