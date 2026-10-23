// B11 (finding 9): a ticket and its text on ONE review. The terminal path: the dialog shows the ticket AND every written
// section in the one view, one command, one fingerprint; it is not "done" when the first signature landed but when the
// whole run is in the ticket; and a run that stops says so and what stays a draft.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fingerprintOf, requestIsConsistent, reviewDigestOf, type HandoffAction, type HandoffEvent, type HandoffRequest } from '@/api/handoff'
import { clickEligible } from '@/api/click'
import { fakeRequest, spies, state } from '@/test/handoffHarness'
import { SignPopover } from './SignPopover'
import { GRANT_STALL_MS, POLL_MS, SEQUENCE_STALL_MS } from './useHandoff'

const T = { timeout: 5000 }
const ACTION: HandoffAction = { action: 'new_sequence', title: 'Plan the launch', type: 'feature', priority: 'high', sections: { summary: 'Why this exists.', context: 'Line one\nLine two' } }
const INTENT = { v: 1, kind: 'new_ticket', title: 'Plan the launch', ticket_type: 'feature', set: { priority: 'high' }, sections: { context: 'Line one\nLine two', summary: 'Why this exists.' } }

/** The first request of a sequence as the dashboard server sends it: `new`, the intent in the material, the SignContent. */
function sequenceRequest(): HandoffRequest {
  const event: HandoffEvent = { v: 2, id: '01J9ZP0000000000000000000B', type: 'ticket.created', actor: { kind: 'person', id: 'p_sev', device: null }, auth: null, hash_v: 1, roster_v: 4, based_on: null as never, key: 'DEMO-0044', ticket_type: 'feature', title: 'Plan the launch', owner: 'p_sev' }
  const material = {
    kind: 'payload' as const,
    ticket: { key: 'DEMO-0044', uid: '01J9ZP0000000000000000000A' },
    sequence: { digest: 'sha256:' + '1'.repeat(64), of: 4, intent: INTENT },
    review: [{ event_id: event.id as string, type: 'ticket.created', payload: { key: 'DEMO-0044', ticket_type: 'feature', title: 'Plan the launch', owner: 'p_sev' }, derived: { sections: {} } }],
  }
  const digest = reviewDigestOf(material)
  const base = fakeRequest({ key: 'DEMO-0044', events: [event], material })
  return {
    ...base,
    op: 'new',
    material,
    review_digest: digest,
    fingerprint: fingerprintOf(digest, [event], 'new'),
    gate_hash: null,
    headline: 'Create DEMO-0044: Plan the launch',
    content: {
      headline: 'Create DEMO-0044 (key per host; signed id …0000000A): Plan the launch',
      title: 'New ticket DEMO-0044: Plan the launch',
      subline: 'and its text (2 sections)',
      ticket: { key: 'DEMO-0044', title: 'Plan the launch', uid_tail: '0000000A', new: true, key_note: 'key per host, not proven; the ticket’s signed id ends …0000000A' },
      sections: [
        { id: 'context', title: 'Context', text: 'Line one\nLine two' },
        { id: 'summary', title: 'Summary', text: 'Why this exists.' },
      ],
      fields_changed: [{ field: 'priority', before: null, after: 'high' }],
      notes: ['One confirmation signs all 4 steps, in order (4 signatures). If the ticket changes meanwhile, signing stops and what was created stays a draft.'],
    },
  } as HandoffRequest
}

let s: ReturnType<typeof spies>
function mount(onSettled = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const user = userEvent.setup()
  render(
    <QueryClientProvider client={client}>
      <SignPopover title="New ticket" ws="w_1" action={ACTION} autoStart onClose={() => {}} onSettled={onSettled} />
    </QueryClientProvider>,
  )
  return { user, onSettled }
}
beforeEach(() => {
  s = spies()
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('a ticket and its text on one review (terminal)', () => {
  it('is a click action too: the signer signs every step on one click', () => {
    expect(clickEligible(ACTION)).toBe(true)
  })

  it('the request recomputes with the intent in its material (the digest covers the whole intent)', () => {
    const r = sequenceRequest()
    expect(requestIsConsistent(r)).toBe(true)
    const changed = { ...r, material: { ...r.material, sequence: { ...r.material.sequence!, intent: { ...INTENT, title: 'Another' } } } }
    expect(requestIsConsistent(changed)).toBe(false) // another intent under the same digest is never shown
  })

  it('shows the ticket AND its written text in one view, with one command and one fingerprint', async () => {
    const request = sequenceRequest()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    mount()
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(s.prepare).toHaveBeenCalledTimes(1)
    expect(s.prepare.mock.calls[0][1]).toEqual(ACTION)
    // U3: the dialog title is the plain one; the key qualifier and signed id are only under Technical details
    expect(within(dialog).getByRole('heading', { name: 'New ticket DEMO-0044: Plan the launch' })).toBeInTheDocument()
    expect(within(dialog).getAllByText('Technical details')).toHaveLength(1)
    expect(within(dialog).getByText('Technical details').closest('details')).toHaveTextContent('signed id …0000000A')
    expect(dialog).toHaveTextContent('and its text (2 sections)')
    expect(dialog).toHaveTextContent('Verification fingerprint:')
    expect(dialog).toHaveTextContent('Why this exists.')
    expect(dialog).toHaveTextContent('Line two')
    expect(dialog).toHaveTextContent('One confirmation signs all 4 steps')
    expect(within(dialog).getAllByTestId('handoff-command')).toHaveLength(1)
  })

  it('is not done when only the first signature landed: it follows the run, then is done', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const request = sequenceRequest()
    s.prepare.mockResolvedValue(request)
    const landed = (done: number) => state(request, { state: 'submitted', seq: 1, event_ids: ['x'], progress: { done, of: 4 } })
    s.get.mockResolvedValueOnce(state(request)).mockResolvedValueOnce(landed(1)).mockResolvedValueOnce(landed(3)).mockResolvedValue(landed(4))
    const { onSettled } = mount()
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_MS * 2)))
    expect(await within(dialog).findByTestId('sequence-progress', {}, T)).toHaveTextContent('1 of 4 steps')
    expect(onSettled).not.toHaveBeenCalledWith('done', expect.anything(), expect.anything())
    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_MS * 3)))
    await waitFor(() => expect(onSettled).toHaveBeenCalledWith('done', expect.objectContaining({ op: 'new', key: 'DEMO-0044' }), expect.anything()), T)
  })

  it('a run that stops says what landed and that the ticket stays a draft', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const request = sequenceRequest()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValueOnce(state(request)).mockResolvedValue(state(request, { state: 'submitted', seq: 1, event_ids: ['x'], progress: { done: 2, of: 4 } }))
    const { onSettled } = mount()
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_MS * 2)))
    await act(async () => void (await vi.advanceTimersByTimeAsync(SEQUENCE_STALL_MS + POLL_MS * 2)))
    expect(await within(dialog).findByText(/Signing stopped after 2 of 4 signatures/, {}, T)).toBeInTheDocument()
    expect(dialog).toHaveTextContent('stays a draft')
    expect(onSettled).not.toHaveBeenCalledWith('done', expect.anything(), expect.anything())
  })
})

describe('a quick task with agent access (terminal)', () => {
  const withAgent = (kind = 'new_ticket'): HandoffRequest => {
    const r = sequenceRequest()
    const material = { ...r.material, sequence: { ...r.material.sequence!, intent: { ...INTENT, kind, agent: { hours: 24, scope: 'workable' } } } }
    const digest = reviewDigestOf(material)
    return { ...r, material, review_digest: digest, fingerprint: fingerprintOf(digest, r.events, 'new') }
  }

  it('asks for the same word as the terminal: GRANT, not the ticket key', async () => {
    const request = withAgent()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    mount()
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(dialog).toHaveTextContent('In your terminal you type GRANT to continue.')
    expect(dialog).not.toHaveTextContent(/you type DEMO-0044\//)
  })

  it('without agent access the word stays the ticket key and its id tail', async () => {
    const request = sequenceRequest()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    mount()
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(dialog).toHaveTextContent('you type DEMO-0044/0000000A to continue')
  })

  it('says in words that the TICKET exists (nothing was approved) and the agent access is not confirmed when the last signature never follows', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const request = withAgent()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValueOnce(state(request)).mockResolvedValue(state(request, { state: 'submitted', seq: 1, event_ids: ['x'], progress: { done: 3, of: 4, last: 'grant' } }))
    const { onSettled } = mount()
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_MS * 2)))
    await act(async () => void (await vi.advanceTimersByTimeAsync(GRANT_STALL_MS + POLL_MS * 2)))
    expect(await within(dialog).findByText(/Ticket created; agent access not confirmed yet/, {}, T)).toBeInTheDocument()
    expect(dialog).toHaveTextContent('DEMO-0044 was created. Agent access is not confirmed yet: the last signature has not arrived')
    expect(within(dialog).queryByRole('button', { name: 'Start again' })).toBeNull() // another Start would make a second task
    expect(onSettled).not.toHaveBeenCalledWith('done', expect.anything(), expect.anything())
  })

  it('F-712-4: asks once more before saying the last signature has not arrived, and does not fail when it lands meanwhile', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const request = withAgent()
    s.prepare.mockResolvedValue(request)
    const stalled = state(request, { state: 'submitted', seq: 1, event_ids: ['x'], progress: { done: 3, of: 4, last: 'grant' } })
    const landed = state(request, { state: 'submitted', seq: 2, event_ids: ['x', 'y'] })
    let last = 0
    s.get.mockImplementation(async () => {
      const now = Date.now()
      const back = now - last < 1000 && last !== 0 // two reads in the same poll: the stall check and its one re-check
      const stallTime = now > t0 + GRANT_STALL_MS
      last = now
      return back && stallTime ? landed : stalled
    })
    const t0 = Date.now()
    const { onSettled } = mount()
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_MS * 2)))
    await act(async () => void (await vi.advanceTimersByTimeAsync(GRANT_STALL_MS + POLL_MS * 6)))
    expect(dialog).not.toHaveTextContent(/not confirmed yet/)
    expect(onSettled).not.toHaveBeenCalledWith('failed', expect.anything(), expect.anything())
  })

  it('for a quick task: says the task exists and is approved, and the agent access is not confirmed when the last signature never follows', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const request = withAgent('quick_task')
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValueOnce(state(request)).mockResolvedValue(state(request, { state: 'submitted', seq: 1, event_ids: ['x'], progress: { done: 3, of: 4, last: 'grant' } }))
    const { onSettled } = mount()
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_MS * 2)))
    await act(async () => void (await vi.advanceTimersByTimeAsync(GRANT_STALL_MS + POLL_MS * 2)))
    expect(await within(dialog).findByText(/Task created; agent access not confirmed yet/, {}, T)).toBeInTheDocument()
    expect(dialog).toHaveTextContent('agent access is not confirmed yet: the last signature has not arrived')
    expect(within(dialog).queryByRole('button', { name: 'Start again' })).toBeNull() // another Start would make a second task
    expect(onSettled).not.toHaveBeenCalledWith('done', expect.anything(), expect.anything())
  })
})

describe('a click sequence that stopped part way', () => {
  it('is said in the signer\'s words, never as "nothing was signed"', async () => {
    const { clickProblem } = await import('@/api/click')
    const { ApiError } = await import('@/api/types')
    const p = clickProblem(new ApiError(409, { code: 'human.click_refused', message: 'the ticket changed while signing, review again. 2 of 5 signatures landed; the ticket DEMO-0044 stays a draft.', retryable: false, reason: 'stopped' }))
    expect(p.title).toBe('Signing stopped part way')
    expect(p.body).toContain('2 of 5 signatures landed')
    expect(p.body).toContain('DEMO-0044')
    expect(p.body).not.toContain('Nothing was signed')
    expect(p.body).toContain('Starting again makes a new ticket')
    expect(p.next).toBe('open')
  })
  it('a half-done run a click cannot resume says where to finish it', async () => {
    const { clickProblem } = await import('@/api/click')
    const { ApiError } = await import('@/api/types')
    const p = clickProblem(new ApiError(409, { code: 'human.click_refused', message: 'DEMO-0044 was started from this review and is not finished. Finish it in your terminal (orch sign) or on the ticket page; nothing is signed here.', retryable: false, reason: 'resumable' }))
    expect(p.title).toBe('Not finished yet')
    expect(p.body).toContain('terminal')
    expect(p.next).toBe('open')
  })
})
