// Run-13 owner e2e: a question answered in its card (one click) stays on screen as "You answered: <option>" instead of
// vanishing into "Nothing needs you", and a question card names its ticket in plain words (title only; the key sits
// behind the small "i"). On Today and on the ticket page.
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore } from '@/api/client'
import { ARM_MS, CLICK_RISK, type ClickCard, type ClickViewAnswer } from '@/api/click'
import { setFeatures } from '@/api/features'
import { resetSignedDrops } from '@/api/signedDrop'
import { openInfo } from '@/test/info'
import { renderApp } from '@/test/renderApp'
import { ANSWERED_MS } from './index'

const T = { timeout: 6000 }
const FEATURES = ['identity', 'today', 'tickets', 'board', 'ticket', 'actions']
const KEY = 'DEMO-0043'

let signed = 0

beforeEach(() => {
  vi.spyOn(document, 'hasFocus').mockReturnValue(true) // arming runs only in a focused, visible window
  resetSignedDrops()
  signed = 0
  const card: ClickCard = { custody: 'convenience', enabled: true, available: true }
  const real = api.getWorkspaces.bind(api)
  vi.spyOn(api, 'getWorkspaces').mockImplementation(async () => {
    const rows = (await real()).map((w) => ({ ...w, click: card }))
    setFeatures({ features: FEATURES, workspaces: Object.fromEntries(rows.map((w) => [w.id, FEATURES])) })
    return rows
  })
  setFeatures({ features: FEATURES })
  vi.spyOn(api, 'prepareClick').mockImplementation(async () => ({ request_id: 'req1'.padEnd(32, '0'), expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'answer', confirm: 'click' }))
  vi.spyOn(api, 'viewClick').mockImplementation(async () => view())
  vi.spyOn(api, 'cancelClick').mockResolvedValue({ id: 'x', state: 'cancelled' })
  // The host records the answer: the next read of "what needs you" no longer lists the question.
  vi.spyOn(api, 'signClick').mockImplementation(async () => {
    signed++
    const q = mockStore.ticket(KEY)!.questions_state.find((x) => x.id === 'Q2')!
    mockStore.append(KEY, { type: 'question.answered', actor: 'p_sev', question: 'Q2', option: q.options![0].key, hash: q.hash })
    return { state: 'submitted', duplicate: false, key: KEY, seq: 99 }
  })
})
afterEach(async () => {
  cleanup()
  await new Promise((r) => setTimeout(r, 20))
  vi.useRealTimers()
  vi.restoreAllMocks()
  setFeatures(null)
})

function view(): ClickViewAnswer {
  const q = mockStore.ticket(KEY)!.questions_state.find((x) => x.id === 'Q2')!
  return {
    view_digest: 'sha256:' + '1'.padStart(64, '0'),
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
        ticket: { key: KEY, uid_tail: 'abcd1234' },
        sections: [{ id: 'question', title: 'Question', text: q.text }],
        fields_changed: [{ field: 'your choice', before: null, after: q.options![0].label }],
      },
    } as unknown as ClickViewAnswer['view'],
  }
}

/** Picks the first option in `scope`, waits for the strip to arm and presses Send answer. */
async function answerIn(scope: () => HTMLElement) {
  fireEvent.click((await within(scope()).findAllByRole('radio', {}, T))[0])
  await within(scope()).findByTestId('answer-strip', {}, T)
  const send = () => within(scope()).getByRole('button', { name: 'Send answer' })
  await waitFor(() => expect(send()).toBeEnabled(), { timeout: ARM_MS + 3000 })
  fireEvent.pointerDown(send())
  fireEvent.click(send())
  await waitFor(() => expect(signed).toBe(1), T)
}

describe('Today: the answered question card', () => {
  it('collapses to "You answered: <option>" and stays while the list no longer has the question, then leaves', async () => {
    const title = mockStore.ticket(KEY)!.title
    const label = mockStore.ticket(KEY)!.questions_state.find((x) => x.id === 'Q2')!.options![0].label
    const { client } = renderApp('/', { viewer: 'p_sev' })
    const row = () => screen.getByTestId(`card-question:${KEY}:Q2`)
    await screen.findByTestId(`card-question:${KEY}:Q2`, {}, T)
    await answerIn(row)
    const line = await within(row()).findByTestId('answered-line', {}, T)
    expect(line).toHaveTextContent(`You answered: ${label}`)
    // The host's list is read again and no longer has the question: the card stays.
    const ws = mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id
    await act(() => client.refetchQueries({ queryKey: ['today', ws] }))
    expect(mockStore.needsYou(ws).some((i) => i.ticket === KEY && i.ref === 'Q2')).toBe(false)
    await new Promise((r) => setTimeout(r, 1500))
    expect(within(row()).getByTestId('answered-line')).toHaveTextContent(`You answered: ${label}`)
    expect(screen.queryByText('Nothing needs you right now')).toBeNull()
    // Plain words: the ticket's title, no key on the card.
    expect(within(row()).getByRole('link', { name: title })).toBeInTheDocument()
    expect(row()).not.toHaveTextContent(KEY)
    // After a few seconds the answered card goes.
    await waitFor(() => expect(screen.queryByTestId(`card-question:${KEY}:Q2`)).toBeNull(), { timeout: ANSWERED_MS + 3000 })
  }, ANSWERED_MS + 20_000)

  it('an open question card shows the ticket title, not its key; the key is behind the "i"', async () => {
    const title = mockStore.ticket(KEY)!.title
    renderApp('/', { viewer: 'p_sev' })
    const row = await screen.findByTestId(`card-question:${KEY}:Q2`, {}, T)
    expect(within(row).getByRole('link', { name: title })).toHaveAttribute('href', expect.stringContaining(`/ticket/${KEY}`))
    expect(row).not.toHaveTextContent(KEY)
    expect(await openInfo(row)).toHaveTextContent(KEY)
  })
})

describe('Ticket page: the answered question card', () => {
  const card = () => document.getElementById('question-Q2') as HTMLElement

  it('turns into "You answered: <option>" and stays after the ticket is read again', async () => {
    const label = mockStore.ticket(KEY)!.questions_state.find((x) => x.id === 'Q2')!.options![0].label
    const { client } = renderApp(`/ticket/${KEY}?tab=questions`, { viewer: 'p_sev' })
    await waitFor(() => expect(card()).not.toBeNull(), T)
    await answerIn(card)
    await waitFor(() => expect(within(card()).getByTestId('answered-line')).toHaveTextContent(`You answered: ${label}`), T)
    await act(() => client.refetchQueries({ queryKey: ['ticket'] }))
    await new Promise((r) => setTimeout(r, 500))
    expect(within(card()).getByTestId('answered-line')).toHaveTextContent(`You answered: ${label}`)
  })

  it('the question card carries no ticket key and no question id; both are behind the "i"', async () => {
    renderApp(`/ticket/${KEY}?tab=questions`, { viewer: 'p_sev' })
    await waitFor(() => expect(card()).not.toBeNull(), T)
    expect(card()).not.toHaveTextContent(KEY)
    expect(card()).not.toHaveTextContent(/\bQ2\b/)
    const info = await openInfo(card())
    expect(info).toHaveTextContent(KEY)
    expect(info).toHaveTextContent('Q2')
  })
})
