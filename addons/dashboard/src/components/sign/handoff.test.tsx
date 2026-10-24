import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/types'
import { setFeatures } from '@/api/features'
import { can } from '@/api/permissions'
import { STALE_HOPS } from './useHandoff'
import { CODE, fakeRequest, mount, refusal, spies, state } from '@/test/handoffHarness'

const T = { timeout: 5000 }
const PLAN = { kind: 'approve', gate: 'plan' } as const
let s: ReturnType<typeof spies>

beforeEach(() => {
  s = spies()
})
afterEach(() => {
  vi.restoreAllMocks()
  setFeatures(null)
})

describe('the hand-off dialog (real host)', () => {
  it('prepares once on open and shows the host material, the fingerprint, the code and the exact command', async () => {
    const request = fakeRequest()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    const { ws, ticket } = await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(s.prepare).toHaveBeenCalledTimes(1)
    expect(s.prepare).toHaveBeenCalledWith(ws, { action: 'approve', key: ticket.key, gate: 'plan' }, 0)
    expect(within(dialog).getByRole('heading', { name: 'Approve plan' })).toBeInTheDocument()
    expect(within(dialog).getByTestId('handoff-command')).toHaveTextContent(`orch sign ${CODE}`)
    expect(within(dialog).getByTestId('handoff-fingerprint')).toHaveTextContent(request.fingerprint)
    expect(within(dialog).getByTestId('handoff-fingerprint')).toBeVisible()
    expect(dialog).toHaveTextContent('Check that this matches the fingerprint in your terminal')
    expect(dialog).toHaveTextContent(/This dashboard is not where you sign/)
    expect(dialog).toHaveTextContent('Preview. Your terminal shows what you sign.')
    expect(dialog).toHaveTextContent('Content hash')
    // Nothing in this dialog signs: no passphrase field, no Touch ID wording, no signing call.
    expect(within(dialog).queryByLabelText(/passphrase/i)).toBeNull()
    expect(dialog).not.toHaveTextContent(/Touch ID/)
    expect(s.postAction).not.toHaveBeenCalled()
  })

  it('retention expiry waits for Start again before preparing with retry 1', async () => {
    const request = fakeRequest()
    s.prepare.mockRejectedValueOnce(refusal('intent.expired')).mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    const { user } = await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(await within(dialog).findByText('This attempt is too old; start again', {}, T)).toBeInTheDocument()
    expect(s.prepare).toHaveBeenCalledTimes(1)
    await user.click(within(dialog).getByRole('button', { name: 'Start again' }))
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(s.prepare.mock.calls.map((call) => call[2])).toEqual([0, 1])
  })

  it('a closed binding conflict only advances retry on Start again', async () => {
    s.prepare.mockRejectedValueOnce(refusal('intent.blocked', 409)).mockResolvedValue(fakeRequest())
    const { user } = await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await user.click(await within(dialog).findByRole('button', { name: 'Start again' }, T))
    await waitFor(() => expect(s.prepare).toHaveBeenCalledTimes(2), T)
    expect(s.prepare.mock.calls.map((call) => call[2])).toEqual([0, 1])
  })

  it('a fast browser clock never expires a request the host still holds open', async () => {
    const request = fakeRequest()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 3600_000)
    await waitFor(() => expect(s.get).toHaveBeenCalled(), T)
    expect(within(dialog).queryByRole('button', { name: 'Start again' })).toBeNull()
    expect(within(dialog).getByTestId('handoff-command')).toBeInTheDocument()
  })

  it('draws the preview only from the host material, never from the cached ticket', async () => {
    const request = fakeRequest()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    const { ticket } = await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    const preview = await within(dialog).findByText(/HOST-MATERIAL task text/, {}, T)
    expect(preview).toBeInTheDocument()
    // The cached ticket's own plan text is not in the dialog.
    const cached = ticket.tasks[0].text
    expect(cached).toBeTruthy()
    expect(dialog.textContent).not.toContain(cached)
    // Exact values: the bidi override in the host's text is shown as an escape, not applied.
    expect(dialog.textContent).toContain('\\u{202e}')
    expect(dialog.textContent).not.toContain('‮')
  })

  it('copies the exact command', async () => {
    const request = fakeRequest()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    const { user } = await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    const write = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: write }, configurable: true })
    await user.click(await within(dialog).findByRole('button', { name: 'Copy command' }, T))
    expect(write).toHaveBeenCalledWith(`orch sign ${CODE}`)
  })

  it('counts down to expiry', async () => {
    const request = fakeRequest({ expiresInMs: 125_000 })
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    await mount(PLAN)
    const timer = await screen.findByTestId('handoff-expiry', {}, T)
    expect(timer.textContent).toMatch(/^2:0[4-5]$/)
    await waitFor(() => expect(timer.textContent).toMatch(/^2:0[0-3]$/), T)
  })

  it('follows the request by polling and shows Signed when the host says submitted', async () => {
    const request = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '9' })
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValueOnce(state(request)).mockResolvedValue(state(request, { state: 'submitted', event_ids: [request.events[0].id as string], seq: 9 }))
    const { onClose } = await mount(PLAN)
    await waitFor(() => expect(onClose).toHaveBeenCalled(), { timeout: 9000 }) // the one toast says it; the review closes
    expect(s.get).toHaveBeenCalledWith(expect.any(String), request.id)
    expect(s.prepare).toHaveBeenCalledTimes(1) // never prepares again on its own
    expect(s.cancel).not.toHaveBeenCalledWith(expect.any(String), request.id) // a signed request is never cancelled
  }, 15000)

  it('never follows another request: a poll answering with a different id ends in Unknown outcome, not Signed', async () => {
    const request = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '9' })
    const other = fakeRequest({ id: 'hr_' + '0'.repeat(25) + 'A' })
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(other, { state: 'submitted', event_ids: ['x'], seq: 3 }))
    await mount(PLAN)
    expect(await screen.findByText('Unknown outcome', {}, { timeout: 9000 })).toBeInTheDocument()
    expect(screen.queryByText('Signed')).not.toBeInTheDocument()
  }, 15000)

  it('after a lost answer, Try again reuses the key and shows the request the host returns', async () => {
    const request = fakeRequest()
    s.prepare.mockRejectedValueOnce(refusal('outcome.unknown', 503)).mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    const { user } = await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByText('Unknown outcome', {}, T)
    expect(dialog).not.toHaveTextContent('earlier attempt')
    await user.click(within(dialog).getByRole('button', { name: 'Try again' }))
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(s.prepare).toHaveBeenCalledTimes(2)
    expect(s.prepare.mock.calls[1][2]).toBe(s.prepare.mock.calls[0][2]) // one key per click, reused
  })

  it('a retry that finds the request already signed ends in Signed, with no second prepare', async () => {
    s.prepare.mockRejectedValueOnce(refusal('outcome.unknown', 503)).mockResolvedValue({ ...fakeRequest(), state: 'submitted' })
    const { user, onClose } = await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await user.click(await within(dialog).findByRole('button', { name: 'Try again' }, T))
    await waitFor(() => expect(onClose).toHaveBeenCalled(), T)
    expect(s.prepare).toHaveBeenCalledTimes(2)
  })

  it('idem.conflict at the host is an attempt that is already open: nothing is offered that prepares again', async () => {
    s.prepare.mockRejectedValue(refusal('intent.open', 409))
    await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(await within(dialog).findByText('An attempt for this is already open', {}, T)).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /again|Try/ })).toBeNull()
    expect(s.prepare).toHaveBeenCalledTimes(1)
  })

  it('a request that is already signed is "Already done", and Do it again is an explicit new intent', async () => {
    const done = new ApiError(409, { code: 'intent.done', message: 'Already done', retryable: false, op: 'log', signed_at: new Date(Date.now() - 120_000).toISOString(), summary: 'HOST-SIGNED comment text' })
    const fresh = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '2', code: 'AAAA-BBBB' })
    s.prepare.mockRejectedValueOnce(done).mockResolvedValueOnce(fresh)
    s.get.mockResolvedValue(state(fresh))
    const { user } = await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(await within(dialog).findByText('Already done', {}, T)).toBeInTheDocument()
    expect(within(dialog).queryByText('Signed')).toBeNull()
    expect(dialog).toHaveTextContent('A similar comment with the same text was signed 2 min ago')
    expect(dialog).toHaveTextContent('HOST-SIGNED comment text')
    expect(within(dialog).getByText(/A similar comment/).closest('p')).toHaveAttribute('title', done.signed_at)
    expect(within(dialog).getByText(/A similar comment/).closest('p')).not.toHaveTextContent('HOST-SIGNED') // folded below
    expect(within(dialog).getByTestId('technical-details')).not.toHaveAttribute('open')
    expect(s.prepare).toHaveBeenCalledTimes(1) // nothing starts again by itself
    await user.click(within(dialog).getByRole('button', { name: 'Do it again' }))
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(s.prepare.mock.calls.map((c) => c[2])).toEqual([0, 1])
  })

  it('an add-on already as asked is said in plain words, with nothing to do again', async () => {
    s.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'addon.already', message: 'Terminals is already on.', retryable: false }))
    await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(await within(dialog).findByText('Already done', {}, T)).toBeInTheDocument()
    expect(dialog).toHaveTextContent('Terminals is already on.')
    expect(within(dialog).queryByRole('button', { name: 'Do it again' })).toBeNull()
    expect(s.prepare).toHaveBeenCalledTimes(1)
  })

  it('a failed submitted intent offers Start again with the next retry', async () => {
    const fresh = fakeRequest()
    s.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'intent.failed', message: 'The earlier attempt was submitted but did not land (gate.stale). It was not signed into the log.', retryable: false })).mockResolvedValue(fresh)
    s.get.mockResolvedValue(state(fresh))
    const { user } = await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByText('The earlier attempt did not land', {}, T)
    expect(dialog).not.toHaveTextContent('Already done')
    expect(dialog).toHaveTextContent('not signed into the log')
    await user.click(within(dialog).getByRole('button', { name: 'Start again' }))
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(s.prepare.mock.calls.map((call) => call[2])).toEqual([0, 1])
  })

  it('keeps polling a busy host until a committed outcome arrives', async () => {
    const request = fakeRequest()
    s.prepare.mockResolvedValue(request)
    for (let i = 0; i < 5; i++) s.get.mockRejectedValueOnce(new ApiError(503, { code: 'lock.busy', message: 'busy', retryable: true }, 9))
    s.get.mockResolvedValue(state(request, { state: 'submitted', seq: 8 }))
    const { onClose } = await mount(PLAN)
    await waitFor(() => expect(onClose).toHaveBeenCalled(), { timeout: 18000 })
    expect(s.prepare).toHaveBeenCalledTimes(1)
    expect(s.get.mock.calls.length).toBeGreaterThanOrEqual(6)
  }, 22000)

  it('a lost answer is retried as the same intent (the same retry), also after signing in again', async () => {
    const request = fakeRequest()
    s.prepare.mockRejectedValueOnce(refusal('outcome.unknown', 503)).mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    const { user } = await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByText('Unknown outcome', {}, T)
    await user.click(within(dialog).getByRole('button', { name: 'Try again' }))
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(s.prepare.mock.calls.map((c) => c[2])).toEqual([0, 0]) // the server derives the same idem; no browser key
  })

  it('cancel asks the host to cancel and closes', async () => {
    const request = fakeRequest()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    const { user, onClose, ws } = await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(s.cancel).toHaveBeenCalledWith(ws, request.id)
  })

  it('shows Cancelled when the host reports it and does not prepare again', async () => {
    const request = fakeRequest()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request, { state: 'cancelled' }))
    await mount(PLAN)
    expect(await screen.findByText('Cancelled', {}, { timeout: 9000 })).toBeInTheDocument()
    expect(screen.getByText(/Nothing was signed\./)).toBeInTheDocument()
    expect(s.prepare).toHaveBeenCalledTimes(1)
  }, 15000)

  it('after a cancelled request, Try again starts a fresh request (a new intent), never the same cancelled one', async () => {
    const first = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '1' })
    const second = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '2', code: 'AAAA-BBBB' })
    s.prepare.mockResolvedValueOnce(first).mockResolvedValueOnce(second)
    s.get.mockResolvedValueOnce(state(first, { state: 'cancelled' })).mockResolvedValue(state(second))
    const { user } = await mount(PLAN)
    expect(await screen.findByText('Cancelled', {}, { timeout: 9000 })).toBeInTheDocument()
    expect(s.prepare).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByTestId('handoff-command', {}, T)).toHaveTextContent('orch sign AAAA-BBBB')
    expect(s.prepare.mock.calls.map((c) => c[2])).toEqual([0, 1])
  }, 15000)

  it('opening the action again after a cancel asks for a fresh request instead of showing the cancelled one', async () => {
    const old = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '1' })
    const fresh = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '2', code: 'CCCC-DDDD' })
    s.prepare.mockResolvedValueOnce({ ...old, state: 'cancelled' }).mockResolvedValueOnce(fresh)
    s.get.mockResolvedValue(state(fresh))
    await mount(PLAN)
    expect(await screen.findByTestId('handoff-command', {}, T)).toHaveTextContent('orch sign CCCC-DDDD')
    expect(s.prepare.mock.calls.map((c) => c[2])).toEqual([0, 1])
    expect(screen.queryByText('Cancelled')).toBeNull()
  })

  it('opening the action again after several cancels walks past every earlier ended attempt to a fresh request', async () => {
    // e2e run 4 pass 2: an action cancelled twice before showed "Cancelled" on the next open and needed Try again 1-2x.
    const ended = (n: number, st: 'cancelled' | 'expired') => ({ ...fakeRequest({ id: 'hr_' + '0'.repeat(25) + n }), state: st })
    const fresh = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '9', code: 'EEEE-FFFF' })
    s.prepare
      .mockResolvedValueOnce(ended(5, 'cancelled'))
      .mockResolvedValueOnce(ended(6, 'cancelled'))
      .mockResolvedValueOnce(ended(7, 'expired'))
      .mockResolvedValueOnce(fresh)
    s.get.mockResolvedValue(state(fresh))
    await mount(PLAN)
    expect(await screen.findByTestId('handoff-command', {}, T)).toHaveTextContent('orch sign EEEE-FFFF')
    expect(s.prepare.mock.calls.map((c) => c[2])).toEqual([0, 1, 2, 3])
    expect(screen.queryByText('Cancelled')).toBeNull()
    // an ended request is left as it is: nothing resurrects or cancels it again
    expect(s.cancel.mock.calls.map((c) => c[1]).filter((id) => /[567]$/.test(String(id)))).toEqual([])
  })

  it('Try again after a cancel also walks past earlier ended attempts of the same action', async () => {
    const first = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '1' })
    const fresh = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '3', code: 'GGGG-HHHH' })
    s.prepare
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce({ ...fakeRequest({ id: 'hr_' + '0'.repeat(25) + '2' }), state: 'cancelled' })
      .mockResolvedValueOnce(fresh)
    s.get.mockResolvedValueOnce(state(first, { state: 'cancelled' })).mockResolvedValue(state(fresh))
    const { user } = await mount(PLAN)
    expect(await screen.findByText('Cancelled', {}, { timeout: 9000 })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByTestId('handoff-command', {}, T)).toHaveTextContent('orch sign GGGG-HHHH')
    expect(s.prepare.mock.calls.map((c) => c[2])).toEqual([0, 1, 2])
  }, 15000)

  it('the walk past ended attempts is bounded: a host that only answers cancelled ends at Cancelled', async () => {
    s.prepare.mockResolvedValue({ ...fakeRequest(), state: 'cancelled' })
    await mount(PLAN)
    expect(await screen.findByText('Cancelled', {}, T)).toBeInTheDocument()
    expect(s.prepare).toHaveBeenCalledTimes(STALE_HOPS + 1)
  })

  it('shows Expired with Start again, and prepares again only when the person asks', async () => {
    const first = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '1' })
    const second = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '2', code: 'AAAA-BBBB' })
    s.prepare.mockResolvedValueOnce(first).mockResolvedValueOnce(second)
    s.get.mockResolvedValueOnce(state(first, { state: 'expired' })).mockResolvedValue(state(second))
    const { user } = await mount(PLAN)
    expect(await screen.findByText('Expired', {}, { timeout: 9000 })).toBeInTheDocument()
    expect(s.prepare).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: 'Start again' }))
    expect(await screen.findByTestId('handoff-command', {}, T)).toHaveTextContent('orch sign AAAA-BBBB')
    expect(s.prepare).toHaveBeenCalledTimes(2)
    expect(s.prepare.mock.calls.map((c) => c[2])).toEqual([0, 1]) // the explicit click advances the retry counter
  }, 20000)

  it.each(['gate.already_approved', 'transition.refused'])('%s is "Already done": nothing to sign, nothing prepared again', async (code) => {
    s.prepare.mockRejectedValue(refusal(code))
    await mount(PLAN)
    const note = await screen.findByText('Already done', {}, T)
    expect(note).toBeInTheDocument()
    expect(screen.queryByTestId('handoff-command')).toBeNull()
    expect(s.prepare).toHaveBeenCalledTimes(1)
  })

  it.each(['gate.stale', 'question.stale', 'artifact.mismatch'])('%s is "Changed since you started" and waits for the person', async (code) => {
    s.prepare.mockRejectedValue(refusal(code))
    const { user } = await mount(PLAN)
    expect(await screen.findByText('Changed since you started', {}, T)).toBeInTheDocument()
    expect(s.prepare).toHaveBeenCalledTimes(1)
    s.prepare.mockResolvedValue(fakeRequest())
    s.get.mockResolvedValue(state(fakeRequest()))
    await user.click(screen.getByRole('button', { name: 'Prepare again' }))
    expect(await screen.findByTestId('handoff-command', {}, T)).toBeInTheDocument()
    expect(s.prepare).toHaveBeenCalledTimes(2)
  })

  it('a transient failure is retried as the same intent', async () => {
    const lost = refusal('host.unavailable', 503, true)
    s.prepare.mockRejectedValueOnce(lost).mockResolvedValue(fakeRequest())
    s.get.mockResolvedValue(state(fakeRequest()))
    const { user } = await mount(PLAN)
    await user.click(await screen.findByRole('button', { name: 'Try again' }, T))
    await screen.findByTestId('handoff-command', {}, T)
    expect(s.prepare).toHaveBeenCalledTimes(2)
    expect(s.prepare.mock.calls.map((c) => c[2])).toEqual([0, 0])
  })

  it('a host answer whose fingerprint does not recompute is not shown, and is cancelled', async () => {
    const request = { ...fakeRequest(), fingerprint: '0'.repeat(12) }
    s.prepare.mockResolvedValue(request)
    await mount(PLAN)
    expect(await screen.findByText('Not shown', {}, T)).toBeInTheDocument()
    expect(screen.queryByTestId('handoff-command')).toBeNull()
    expect(screen.queryByTestId('handoff-fingerprint')).toBeNull()
    await waitFor(() => expect(s.cancel).toHaveBeenCalledWith(expect.any(String), request.id))
  })

  it('warns when the ticket moved on after the request was prepared', async () => {
    const { ticket } = await (async () => {
      s.prepare.mockResolvedValue(fakeRequest({ gate_hash: 'sha256:' + 'e'.repeat(64) }))
      s.get.mockResolvedValue(state(fakeRequest()))
      return mount(PLAN)
    })()
    expect(ticket.gates.plan.hash).not.toBe('sha256:' + 'e'.repeat(64))
    expect(await screen.findByRole('alert', {}, T)).toHaveTextContent(/This ticket changed after the request was prepared/)
  })
})

describe('the preview shows values exactly', () => {
  it('tells "1" from 1, does not cut deep values, shows the full ticket id and the envelope', async () => {
    const deep = { a: { b: { c: { d: { e: { f: { g: { h: { i: 'DEEPEST-VALUE' } } } } } } } } }
    const base = fakeRequest()
    const request = fakeRequest({ material: { input: { n: 1, s: '1', t: true, ts: 'true', deep } } })
    void base
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    const text = dialog.textContent ?? ''
    expect(text).toContain('"1"')
    expect(text).toContain('"true"')
    expect(text).toContain('DEEPEST-VALUE')
    expect(text).not.toMatch(/nested deeper/)
    expect(text).toContain('01J9ZP0000000000000000000A')
    expect(text).toContain('ticket key is host-local, not proven')
    expect(text).toContain('DEMO-0044/')
    expect(text).toContain('Stated by the host; your terminal works these out again')
    expect(text).toContain('based_on')
  })
})

describe('requests stay with their workspace and their tab', () => {
  it('closing while preparing cancels the request once it arrives', async () => {
    const request = fakeRequest()
    let release!: (r: typeof request) => void
    s.prepare.mockReturnValue(new Promise((r) => (release = r)))
    const { unmount } = await mount(PLAN)
    await waitFor(() => expect(s.prepare).toHaveBeenCalled())
    unmount()
    release(request)
    await waitFor(() => expect(s.cancel).toHaveBeenCalledWith(expect.any(String), request.id))
  })

  it('shows a refusal before any append as still open, with the code text', async () => {
    const request = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '5' })
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request, { state: 'bound', error: { code: 'gate.stale' } }))
    await mount(PLAN)
    expect(await screen.findByText(/Refused: The ticket changed before it was signed/, {}, { timeout: 9000 })).toBeInTheDocument()
    expect(screen.getByTestId('handoff-command')).toBeInTheDocument() // the request is still open
  }, 15000)

  it('a bad expires_at is not shown', async () => {
    s.prepare.mockResolvedValue({ ...fakeRequest(), expires_at: 'whenever' })
    await mount(PLAN)
    expect(await screen.findByText('Not shown', {}, T)).toBeInTheDocument()
  })

  it('shows the catalog text when the host reports why the submit failed', async () => {
    const request = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '7' })
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request, { state: 'submitted', error: { code: 'gate.stale' } }))
    await mount(PLAN)
    expect(await screen.findByText(/The earlier attempt was submitted but did not land/, {}, { timeout: 9000 })).toBeInTheDocument()
  }, 15000)
})

describe('forms keep core words and prepare only on the verb', () => {
  it('request changes needs the reason, then prepares with it', async () => {
    const request = fakeRequest()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    const { user, ticket } = await mount({ kind: 'request_changes', gate: 'plan' })
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(s.prepare).not.toHaveBeenCalled() // opening the form is not yet a request
    const verb = within(dialog).getByRole('button', { name: 'Request changes' })
    expect(verb).toBeDisabled()
    await user.type(within(dialog).getByRole('textbox'), 'Split T2')
    await user.click(verb)
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(s.prepare).toHaveBeenCalledWith(expect.any(String), { action: 'request_changes', key: ticket.key, gate: 'plan', message: 'Split T2' }, 0)
  })

  it('a verdict names Pass or Send back and shows no cached commit facts before the request exists', async () => {
    const request = fakeRequest({ key: 'DEMO-0041' })
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    const { user, ticket } = await mount({ kind: 'verdict' }, { key: 'DEMO-0041' })
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(dialog).toHaveTextContent('Choose Pass or Send back')
    expect(dialog.textContent).not.toContain(ticket.branch.head)
    await user.click(within(dialog).getByRole('radio', { name: /Send back/ }))
    expect(within(dialog).getByRole('button', { name: 'Send back' })).toBeDisabled()
    await user.type(within(dialog).getByRole('textbox'), 'Add a receipt')
    await user.click(within(dialog).getByRole('button', { name: 'Send back' }))
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(s.prepare).toHaveBeenCalledWith(expect.any(String), { action: 'verdict', key: ticket.key, outcome: 'fail', message: 'Add a receipt' }, 0)
  })

  it('an answer is prepared with exactly the option and text the person chose', async () => {
    const request = fakeRequest({ key: 'DEMO-0043' })
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    const { ticket } = await mount({ kind: 'answer', question: 'Q2', option: 'date', text: 'local midnight' }, { key: 'DEMO-0043' })
    const dialog = await screen.findByRole('dialog', {}, T)
    // The click on the question's Send answer was the intent: the dialog prepares at once, no second Send answer.
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(within(dialog).queryByRole('button', { name: 'Send answer' })).toBeNull()
    expect(s.prepare).toHaveBeenCalledWith(expect.any(String), { action: 'answer', key: ticket.key, question: 'Q2', option: 'date', message: 'local midnight' }, 0)
  })

  it('a double click is one request', async () => {
    const request = fakeRequest()
    let release!: (r: typeof request) => void
    s.prepare.mockReturnValue(new Promise((r) => (release = r)))
    s.get.mockResolvedValue(state(request))
    const { user } = await mount({ kind: 'request_changes', gate: 'plan' })
    const dialog = await screen.findByRole('dialog', {}, T)
    await user.type(within(dialog).getByRole('textbox'), 'x')
    const verb = within(dialog).getByRole('button', { name: 'Request changes' })
    await user.dblClick(verb)
    release(request)
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(s.prepare).toHaveBeenCalledTimes(1)
  })
})

describe('move stays hidden on a real host until the host serves it', () => {
  it('can(ticket.move) is off without the move feature, on with it, and on in the mock', () => {
    setFeatures({ features: ['actions'] })
    expect(can('owner', 'ticket.move')).toBe(false)
    expect(can('owner', 'ticket.act')).toBe(true)
    setFeatures({ features: ['actions', 'move'] })
    expect(can('owner', 'ticket.move')).toBe(true)
    setFeatures(null)
    expect(can('owner', 'ticket.move')).toBe(true)
  })
})

it('uses the host content headline and keeps the technical preview collapsed', async () => {
  const request = { ...fakeRequest(), content: { headline: 'Approve the requirements of DEMO-0044', sections: [{ id: 'requirements', title: 'Requirements', text: 'Do the thing' }] } }
  s.prepare.mockResolvedValue(request)
  s.get.mockResolvedValue(state(request))
  await mount(PLAN)
  const title = await screen.findByRole('heading', { name: request.content.headline }, T)
  const dialog = title.closest('[role="dialog"]')!
  expect(within(dialog as HTMLElement).getByText('Do the thing')).toBeVisible()
  expect(dialog.querySelector('details')).not.toHaveAttribute('open')
  expect(dialog.querySelector('[data-handoff-preview]')?.closest('details')).not.toHaveAttribute('open')
})
