import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ARM_MS, CLICK_RISK, clickProblem, HOLD_MS, type ClickViewAnswer } from '@/api/click'
import { setFeatures } from '@/api/features'
import { ApiError } from '@/api/types'
import { fakeRequest, mount, spies, state } from '@/test/handoffHarness'

const T = { timeout: 5000 }
const PLAN = { kind: 'approve', gate: 'plan' } as const
const REQ = 'hr_' + '0'.repeat(25) + '1'
const DIGEST = 'sha256:' + 'c'.repeat(64)
let s: ReturnType<typeof spies>

const viewAnswer = (over: Partial<ClickViewAnswer['view']> = {}, digest = DIGEST): ClickViewAnswer => ({
  view_digest: digest,
  view: {
    risk: CLICK_RISK,
    op: 'approve',
    workspace: { id: 'x'.repeat(32), name: 'billing', prefix: 'DEMO' },
    fingerprint: '0123456789ab',
    lines: ['Approve the plan of DEMO-0044'],
    checks: [],
    confirm: { kind: 'click' },
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    auth: 'convenience',
    device: 'd_x',
    artifacts: null,
    ...over,
  },
})

const click = {
  prepare: () => vi.spyOn(api, 'prepareClick'),
  view: () => vi.spyOn(api, 'viewClick'),
  sign: () => vi.spyOn(api, 'signClick'),
  cancel: () => vi.spyOn(api, 'cancelClick'),
}
let c: { prepare: ReturnType<typeof click.prepare>; view: ReturnType<typeof click.view>; sign: ReturnType<typeof click.sign>; cancel: ReturnType<typeof click.cancel> }

/** What the server sends for each workspace card (the signer's flag), whatever the store is reset to. */
function flag(on: boolean | 'unavailable') {
  const real = api.getWorkspaces.bind(api)
  const card = on === 'unavailable' ? { custody: 'convenience' as const, enabled: true, available: false, problem: 'custody_unavailable' } : on ? { custody: 'convenience' as const, enabled: true, available: true } : undefined
  vi.spyOn(api, 'getWorkspaces').mockImplementation(async () => (await real()).map((w) => ({ ...w, click: card })))
}

beforeEach(() => {
  s = spies()
  c = { prepare: click.prepare(), view: click.view(), sign: click.sign(), cancel: click.cancel() }
  c.prepare.mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'approve', confirm: 'click' })
  c.view.mockResolvedValue(viewAnswer())
  c.sign.mockResolvedValue({ state: 'submitted', duplicate: false, key: 'DEMO-0044', seq: 9 })
  c.cancel.mockResolvedValue({ id: REQ, state: 'cancelled' })
})
afterEach(async () => {
  cleanup() // unmounting an open request cancels it one tick later: let that happen inside this test
  await new Promise((r) => setTimeout(r, 20))
  vi.restoreAllMocks()
  setFeatures(null)
})

async function open(action: Parameters<typeof mount>[0] = PLAN) {
  flag(true)
  const m = await mount(action)
  const dialog = await screen.findByRole('dialog', {}, T)
  await waitFor(() => expect(within(dialog).getAllByRole('button', { name: /^(Sign|Sign in Terminal)$/ }).find(el => !el.closest('details'))).toBeEnabled(), T)
  return { ...m, dialog }
}
async function toPanel() {
  const m = await open()
  const sign = await within(m.dialog).findByRole('button', { name: 'Sign' }, T)
  fireEvent.click(sign)
  await within(m.dialog).findByTestId('click-panel', {}, T)
  return m
}
const armed = async (dialog: HTMLElement) => {
  const b = within(dialog).getByRole('button', { name: 'Sign' })
  await waitFor(() => expect(b).toBeEnabled(), { timeout: ARM_MS + 2000 })
  return b
}
const press = (b: HTMLElement) => {
  fireEvent.pointerDown(b)
  fireEvent.click(b)
}

describe('the action dialog with click-to-sign', () => {
  it('offers only Sign (one signing path) and prepares nothing until it is pressed', async () => {
    const { dialog } = await open()
    expect(await within(dialog).findByRole('button', { name: 'Sign' }, T)).toBeEnabled()
    expect(within(dialog).queryByRole('button', { name: 'Sign in the terminal instead' })).toBeNull()
    expect(within(dialog).getByRole('button', { name: 'One-click' })).toBeInTheDocument()
    expect(c.prepare).not.toHaveBeenCalled()
    expect(s.prepare).not.toHaveBeenCalled() // an approve no longer starts the terminal request by itself
  })

  it('without the flag there is no Sign button and the hand-off starts as before', async () => {
    flag(false)
    s.prepare.mockResolvedValue(fakeRequest())
    s.get.mockResolvedValue(state(fakeRequest()))
    await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(within(dialog).queryByRole('button', { name: 'Sign' })).toBeNull()
    expect(within(dialog).queryByTestId('click-risk')).toBeNull()
    expect(c.prepare).not.toHaveBeenCalled()
  })

  it('falls back to terminal when the signer is unavailable and settings are absent', async () => {
    flag('unavailable')
    s.prepare.mockResolvedValue(fakeRequest())
    s.get.mockResolvedValue(state(fakeRequest()))
    await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(c.prepare).not.toHaveBeenCalled()
    expect(s.prepare).toHaveBeenCalledTimes(1)
  })

  it('a hand-off refused with sign.click_only (the flag was switched on) says the workspace signs in the dashboard', async () => {
    flag(false)
    s.prepare.mockRejectedValue(new ApiError(409, { code: 'sign.click_only', message: 'This workspace signs in the dashboard.', retryable: false }, 3))
    await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(await within(dialog).findByText(/signs in the dashboard/, {}, T)).toBeInTheDocument()
  })

  it('Sign prepares once with one idem, reads the View and shows only it', async () => {
    const { ws, ticket, dialog } = await toPanel()
    expect(c.prepare).toHaveBeenCalledTimes(1)
    expect(c.prepare).toHaveBeenCalledWith(ws, { action: 'approve', key: ticket.key, gate: 'plan' }, 0)
    expect(c.view).toHaveBeenCalledWith(ws, REQ)
    const panel = within(dialog).getByTestId('click-panel')
    expect(within(panel).getByRole('button', { name: 'One-click' })).toBeInTheDocument()
    expect(within(dialog).queryByTestId('handoff-command')).toBeNull()
    expect(s.prepare).not.toHaveBeenCalled()
    expect(s.postAction).not.toHaveBeenCalled()
  })

  it('signs with the digest of the View it shows, after the arming time, and says Signed', async () => {
    const { ws, dialog } = await toPanel()
    const b = within(dialog).getByRole('button', { name: 'Sign' })
    expect(b).toBeDisabled()
    press(b)
    expect(c.sign).not.toHaveBeenCalled()
    press(await armed(dialog))
    await within(dialog).findByText('Signed', {}, T)
    expect(c.sign).toHaveBeenCalledWith(ws, REQ, DIGEST, undefined)
    expect(c.cancel.mock.calls).toEqual([])
  })

  it('Cancel cancels the request; closing it by Escape does too', async () => {
    const { ws, dialog, onClose } = await toPanel()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(c.cancel).toHaveBeenCalledWith(ws, REQ)
  })

  it('a refusal to sign never cancels: the request stays open to try again or to cancel', async () => {
    const { dialog } = await toPanel()
    c.sign.mockRejectedValueOnce(new ApiError(409, { code: 'human.click_refused', message: 'x', retryable: false, reason: 'too_soon' }))
    press(await armed(dialog))
    const problem = await within(dialog).findByTestId('click-problem', {}, T)
    expect(problem).toHaveTextContent('Too soon')
    expect(c.cancel).not.toHaveBeenCalled()
    expect(within(dialog).getByTestId('click-panel')).toBeInTheDocument()
    c.sign.mockResolvedValueOnce({ state: 'submitted', duplicate: false })
    press(within(dialog).getByRole('button', { name: 'Sign' }))
    await within(dialog).findByText('Signed', {}, T)
    expect(c.sign).toHaveBeenCalledTimes(2)
  })

  it('view_changed asks to read the review again, which shows the new View (and resets the arming)', async () => {
    const { ws, dialog } = await toPanel()
    c.sign.mockRejectedValueOnce(new ApiError(409, { code: 'human.click_refused', message: 'x', retryable: false, reason: 'view_changed' }))
    c.view.mockResolvedValueOnce(viewAnswer({ lines: ['A different review'] }, 'sha256:' + 'd'.repeat(64)))
    press(await armed(dialog))
    await within(dialog).findByText('The review changed', {}, T)
    expect(c.cancel).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Read the review again' }))
    await within(dialog).findByText('A different review', {}, T)
    expect(c.view).toHaveBeenLastCalledWith(ws, REQ)
    expect(within(dialog).getByRole('button', { name: 'Sign' })).toBeDisabled()
  })

  it('a word op shows the word and, after a full hold of Sign, sends the View’s word', async () => {
    c.prepare.mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'policy.set', confirm: 'word' })
    c.view.mockResolvedValue(viewAnswer({ op: 'policy.set', confirm: { kind: 'word', word: 'DEMO~aaaaaaaa/POLICY' } }))
    const { ws, dialog } = await toPanel()
    expect(within(dialog).getByTestId('click-hold-hint')).toHaveTextContent('Hold Sign to confirm DEMO~aaaaaaaa/POLICY')
    const b = within(dialog).getByRole('button', { name: 'Hold to sign' })
    await waitFor(() => expect(b).toBeEnabled(), { timeout: ARM_MS + 2000 })
    fireEvent.pointerDown(b)
    await waitFor(() => expect(c.sign).toHaveBeenCalledWith(ws, REQ, DIGEST, 'DEMO~aaaaaaaa/POLICY'), { timeout: HOLD_MS + 2000 })
    expect(c.sign).toHaveBeenCalledTimes(1)
  })

  it('the prepare failing shows the honest message and no terminal path, and nothing is open', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'sign.convenience_unavailable', message: 'other_member: x', retryable: false, reason: 'other_member' }))
    const { dialog } = await open()
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Sign' }, T))
    const problem = await within(dialog).findByTestId('click-problem', {}, T)
    expect(problem).toHaveTextContent('another member')
    expect(within(dialog).queryByRole('button', { name: 'Sign in the terminal instead' })).toBeNull()
    expect(s.prepare).not.toHaveBeenCalled() // never a hand-off
    expect(c.cancel).not.toHaveBeenCalled()
  })

  it('an unknown outcome waits for the host: no cancel, no second sign, no terminal button; it ends when the host says submitted', async () => {
    const stateSpy = vi.spyOn(api, 'stateClick').mockResolvedValue({ id: REQ, state: 'bound' })
    const { dialog, onClose } = await toPanel()
    c.sign.mockRejectedValueOnce(new ApiError(409, { code: 'human.click_refused', message: 'x', retryable: false, reason: 'uncertain' }))
    press(await armed(dialog))
    const settling = await within(dialog).findByTestId('click-settling', {}, T)
    expect(settling).toHaveTextContent('settles by itself')
    expect(within(dialog).queryByRole('button', { name: 'Sign' })).toBeNull()
    expect(within(dialog).queryByRole('button', { name: 'Sign in the terminal instead' })).toBeNull()
    expect(within(dialog).queryByRole('button', { name: /Try again|Prepare again/ })).toBeNull()
    await waitFor(() => expect(stateSpy).toHaveBeenCalled(), T)
    stateSpy.mockResolvedValue({ id: REQ, state: 'submitted' })
    await within(dialog).findByText('Signed', {}, { timeout: 8000 })
    expect(c.sign).toHaveBeenCalledTimes(1)
    expect(c.cancel.mock.calls).toEqual([])
    expect(onClose).not.toHaveBeenCalled()
  }, 15000)

  it('an uncertain submission that failed never becomes Signed', async () => {
    vi.spyOn(api, 'stateClick').mockRejectedValue(new ApiError(409, { code: 'intent.failed', message: 'The earlier attempt was submitted but did not land (internal). It was not signed into the log.', retryable: false }))
    const { dialog } = await toPanel()
    c.sign.mockRejectedValueOnce(new ApiError(409, { code: 'human.click_refused', message: 'x', retryable: false, reason: 'uncertain' }))
    press(await armed(dialog))
    await within(dialog).findByText('The earlier attempt did not land', {}, T)
    expect(dialog).not.toHaveTextContent('Already done')
    expect(within(dialog).queryByText('Signed')).toBeNull()
    expect(within(dialog).getByRole('button', { name: 'Start again' })).toBeInTheDocument()
    expect(c.sign).toHaveBeenCalledTimes(1)
  })

  it('closing while the outcome is unknown does not cancel; an expired request then offers no terminal path', async () => {
    const stateSpy = vi.spyOn(api, 'stateClick').mockResolvedValue({ id: REQ, state: 'bound' })
    const { dialog } = await toPanel()
    c.sign.mockRejectedValueOnce(new ApiError(409, { code: 'human.click_refused', message: 'x', retryable: false, reason: 'uncertain' }))
    press(await armed(dialog))
    await within(dialog).findByTestId('click-settling', {}, T)
    stateSpy.mockResolvedValue({ id: REQ, state: 'expired' })
    await within(dialog).findByText('Expired', {}, { timeout: 8000 })
    expect(within(dialog).queryByRole('button', { name: 'Sign in the terminal instead' })).toBeNull()
    expect(c.cancel.mock.calls).toEqual([])
  }, 15000)

  it('Try again after the View could not be read cancels the open request first, then prepares anew', async () => {
    c.view.mockRejectedValueOnce(new ApiError(503, { code: 'host.unavailable', message: 'x', retryable: true }, 9))
    const m = await open()
    fireEvent.click(await within(m.dialog).findByRole('button', { name: 'Sign' }, T))
    await within(m.dialog).findByTestId('click-problem', {}, T)
    fireEvent.click(within(m.dialog).getByRole('button', { name: 'Try again' }))
    await within(m.dialog).findByTestId('click-panel', {}, T)
    expect(c.cancel).toHaveBeenCalledWith(m.ws, REQ)
    expect(c.prepare).toHaveBeenCalledTimes(2)
    const keys = c.prepare.mock.calls.map((x) => x[2])
    expect(keys[0]).not.toBe(keys[1]) // a new request is a new click
  })

  it('a View whose risk line is not the card text is not shown', async () => {
    c.view.mockResolvedValue(viewAnswer({ risk: 'Convenience signing is fine.' }))
    const m = await open()
    fireEvent.click(await within(m.dialog).findByRole('button', { name: 'Sign' }, T))
    await within(m.dialog).findByTestId('click-problem', {}, T)
    expect(within(m.dialog).queryByTestId('click-panel')).toBeNull()
  })

  it('shows the custody and the device of the View', async () => {
    const { dialog } = await toPanel()
    expect(within(dialog).getByTestId('click-custody')).toHaveTextContent('One click on this computer, no fingerprint/passphrase')
    expect(within(dialog).getByText('d_x').closest('details')).not.toHaveAttribute('open')
  })

  it('a rate limit keeps Sign off for the time the signer names (fake timers: no wall-clock race)', async () => {
    const { dialog } = await toPanel()
    c.sign.mockRejectedValueOnce(new ApiError(503, { code: 'rate.limited', message: 'x', retryable: true, retry_after_ms: 5000 }, 3))
    press(await armed(dialog))
    await within(dialog).findByText(/Try again in 5 s/, {}, T)
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Sign' })).toBeDisabled())
    // The clock jumps past the wait (only Date is faked); the panel's own ticker notices on its next tick.
    vi.useFakeTimers({ toFake: ['Date'], now: Date.now() + 5600 })
    try {
      await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Sign' })).toBeEnabled(), T)
    } finally {
      vi.useRealTimers()
    }
  }, 20000)
})

describe('an unknown outcome never leads to a second signature', () => {
  const settle = async (m: Awaited<ReturnType<typeof toPanel>>) => {
    press(await armed(m.dialog))
    return within(m.dialog).findByTestId('click-settling', {}, T)
  }
  it.each([
    ['a failed fetch', () => new TypeError('Failed to fetch')],
    ['a body-less 5xx', () => new ApiError(502, { code: 'host.unavailable', message: 'Host not available', retryable: true })],
  ])('%s on /click/sign is an unknown outcome: settling, no terminal, no retry', async (_n, failure) => {
    const stateSpy = vi.spyOn(api, 'stateClick').mockResolvedValue({ id: REQ, state: 'bound' })
    const m = await toPanel()
    c.sign.mockRejectedValueOnce(failure())
    await settle(m)
    expect(within(m.dialog).queryByRole('button', { name: /Sign in the terminal instead|Try again|^Sign$/ })).toBeNull()
    await waitFor(() => expect(stateSpy).toHaveBeenCalled(), T)
  }, 20000)

  it('an action that already landed is "Already done"; Do it again is an explicit click with the next retry', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'intent.done', message: 'This is already done.', retryable: false, state: 'submitted', op: 'log', signed_at: new Date(Date.now() - 120_000).toISOString(), summary: 'HOST-SIGNED comment text' } as never, 5))
    const m = await open()
    fireEvent.click(await within(m.dialog).findByRole('button', { name: 'Sign' }, T))
    expect(await within(m.dialog).findByText('Already done', {}, T)).toBeInTheDocument()
    expect(m.dialog).toHaveTextContent('A similar comment with the same text was signed 2 min ago')
    expect(m.dialog).toHaveTextContent('HOST-SIGNED comment text')
    expect(within(m.dialog).getByText(/A similar comment/).closest('p')).toHaveAttribute('title', expect.stringMatching(/^202/))
    expect(c.prepare).toHaveBeenCalledTimes(1) // nothing starts again by itself
    expect(within(m.dialog).queryByRole('button', { name: /^Sign$|Sign in the terminal instead/ })).toBeNull()
    fireEvent.click(within(m.dialog).getByRole('button', { name: 'Do it again' }))
    await within(m.dialog).findByTestId('click-panel', {}, T).catch(() => undefined)
    await waitFor(() => expect(c.prepare).toHaveBeenCalledTimes(2), T)
    expect(c.prepare.mock.calls.map((call) => call[2])).toEqual([0, 1])
  })

  it('a submitted failure needs an explicit Start again and advances retry', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'intent.failed', message: 'The earlier attempt was submitted but did not land (gate.stale). It was not signed into the log.', retryable: false }))
    const m = await open()
    fireEvent.click(await within(m.dialog).findByRole('button', { name: 'Sign' }, T))
    await within(m.dialog).findByText('The earlier attempt did not land', {}, T)
    expect(m.dialog).toHaveTextContent('not signed into the log')
    expect(m.dialog).not.toHaveTextContent('Already done')
    expect(c.prepare).toHaveBeenCalledTimes(1)
    fireEvent.click(within(m.dialog).getByRole('button', { name: 'Start again' }))
    await waitFor(() => expect(c.prepare).toHaveBeenCalledTimes(2), T)
    expect(c.prepare.mock.calls.map((call) => call[2])).toEqual([0, 1])
  })

  it('a submitted sign refusal shows the prior signature and Do it again', async () => {
    const m = await toPanel()
    c.sign.mockRejectedValueOnce(new ApiError(409, { code: 'intent.done', message: 'Already done', retryable: false, op: 'approve', signed_at: null, summary: 'HOST-SIGNED approval' }))
    press(await armed(m.dialog))
    expect(await within(m.dialog).findByText('Already done', {}, T)).toBeInTheDocument()
    expect(m.dialog).toHaveTextContent('HOST-SIGNED approval')
    expect(m.dialog).toHaveTextContent('signing time unavailable')
    expect(within(m.dialog).getByRole('button', { name: 'Do it again' })).toBeInTheDocument()
    expect(within(m.dialog).queryByRole('button', { name: 'Start again' })).toBeNull()
  })

  it('a closed binding conflict offers an explicit Start again', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'intent.blocked', message: 'No request is open. An earlier one may have been signed.', retryable: false }))
    const m = await open()
    fireEvent.click(await within(m.dialog).findByRole('button', { name: 'Sign' }, T))
    fireEvent.click(await within(m.dialog).findByRole('button', { name: 'Start again' }, T))
    await waitFor(() => expect(c.prepare).toHaveBeenCalledTimes(2), T)
    expect(c.prepare.mock.calls.map((call) => call[2])).toEqual([0, 1])
  })

  it('a cancelled or expired earlier attempt offers Start again (retry 1); an open attempt elsewhere offers nothing', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'intent.ended', message: 'The earlier attempt is cancelled.', retryable: false }, 5))
    const m = await open()
    fireEvent.click(await within(m.dialog).findByRole('button', { name: 'Sign' }, T))
    fireEvent.click(await within(m.dialog).findByRole('button', { name: 'Start again' }, T))
    await waitFor(() => expect(c.prepare).toHaveBeenCalledTimes(2), T)
    expect(c.prepare.mock.calls.map((call) => call[2])).toEqual([0, 1])
  })

  it('retention expiry waits for Start again before preparing with retry 1', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'intent.expired', message: 'Start again to make a new one.', retryable: false }, 5))
    const m = await open()
    fireEvent.click(await within(m.dialog).findByRole('button', { name: 'Sign' }, T))
    expect(await within(m.dialog).findByText('This attempt is too old; start again', {}, T)).toBeInTheDocument()
    expect(c.prepare).toHaveBeenCalledTimes(1)
    fireEvent.click(within(m.dialog).getByRole('button', { name: 'Start again' }))
    await waitFor(() => expect(c.prepare).toHaveBeenCalledTimes(2), T)
    expect(c.prepare.mock.calls.map((call) => call[2])).toEqual([0, 1])
  })

  it('an attempt that is open at the host (terminal, or before a restart) is named and nothing prepares again', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'intent.open', message: 'x', retryable: false }, 5))
    const m = await open()
    fireEvent.click(await within(m.dialog).findByRole('button', { name: 'Sign' }, T))
    expect(await within(m.dialog).findByText('An attempt for this is already open', {}, T)).toBeInTheDocument()
    expect(within(m.dialog).queryByRole('button', { name: /Start again|Do it again|Try again|^Sign$|terminal instead/ })).toBeNull()
  })

  it('after the polling deadline it says so honestly and still offers nothing', async () => {
    c.prepare.mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() - 120_000).toISOString(), op: 'approve', confirm: 'click' })
    vi.spyOn(api, 'stateClick').mockResolvedValue({ id: REQ, state: 'bound' })
    const m = await toPanel()
    c.sign.mockRejectedValueOnce(new ApiError(409, { code: 'human.click_refused', message: 'x', retryable: false, reason: 'uncertain' }))
    await settle(m)
    const note = await within(m.dialog).findByTestId('click-settle-note', {}, { timeout: 8000 })
    expect(note).toHaveTextContent(/Still not settled/)
    expect(note).toHaveTextContent(/Check the ticket/)
    expect(within(m.dialog).queryByRole('button', { name: /Sign in the terminal instead|Try again|Prepare again|^Sign$/ })).toBeNull()
  }, 20000)
})

describe('no address opens a sign panel', () => {
  it('a URL or query naming a request prepares and shows nothing', async () => {
    const { renderApp } = await import('@/test/renderApp')
    flag(true)
    renderApp('/?click=1&sign=hr_00000000000000000000000001&request_id=hr_00000000000000000000000001#sign')
    await screen.findByRole('heading', { name: 'Today' }, T)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 300))
    })
    expect(screen.queryByTestId('click-panel')).toBeNull()
    expect(c.prepare).not.toHaveBeenCalled()
    expect(c.view).not.toHaveBeenCalled()
    expect(c.sign).not.toHaveBeenCalled()
  })
})

describe('every error code and reason has an honest message', () => {
  const err = (code: string, reason?: string, extra: Partial<{ retry_after_ms: number; message: string }> = {}) =>
    new ApiError(409, { code, message: extra.message ?? 'server words', retryable: false, reason, retry_after_ms: extra.retry_after_ms })
  const cases: [string, ApiError, RegExp, string][] = [
    ['disabled', err('sign.convenience_unavailable', 'disabled'), /switched off/, 'terminal'],
    ['relay_paired', err('sign.convenience_unavailable', 'relay_paired'), /relay/, 'terminal'],
    ['other_member', err('sign.convenience_unavailable', 'other_member'), /another member/, 'terminal'],
    ['phone_device', err('sign.convenience_unavailable', 'phone_device'), /another device/, 'terminal'],
    ['reader_version', err('sign.convenience_unavailable', 'reader_version'), /Update orch-core/, 'terminal'],
    ['readers', err('sign.convenience_readers', undefined, { message: 'Update the readers first: relay' }), /Update the readers first/, 'terminal'],
    ['rate', err('rate.limited', undefined, { retry_after_ms: 4200 }), /Try again in 5 s/, 'retry'],
    ['terminal_only', err('human.click_refused', 'terminal_only'), /one click/, 'terminal'],
    ['not_prepared_here', err('human.click_refused', 'not_prepared_here'), /does not know this request/, 'prepare'],
    ['expired', err('human.click_refused', 'expired'), /ran out of time/, 'prepare'],
    ['view_changed', err('human.click_refused', 'view_changed'), /no longer what you were shown/, 'review'],
    ['too_soon', err('human.click_refused', 'too_soon'), /less than a second/, 'retry'],
    ['word', err('human.click_refused', 'word'), /exactly as the review shows/, 'retry'],
    ['custody_changed', err('human.click_refused', 'custody_changed'), /custody of your device key changed/, 'prepare'],
    ['busy', err('human.click_refused', 'busy'), /Another signature is in progress/, 'retry'],
    ['check_failed', err('human.click_refused', 'check_failed'), /refused what the host sent/, 'terminal'],
    ['uncertain', err('human.click_refused', 'uncertain'), /settles by itself/, 'check'],
    ['no_key', err('custody.no_key'), /no device key/i, 'terminal'],
    ['custody_unavailable', err('custody.unavailable'), /locked or refused/, 'terminal'],
    ['version_mismatch', err('host.version_mismatch'), /different versions of orch-core/, 'terminal'],
    ['contract', err('click.contract'), /do not speak the same version/, 'terminal'],
    ['signer stopped', err('click.unavailable'), /not running/, 'prepare'],
    ['request_expired', err('human.request_expired'), /ran out of time/, 'prepare'],
    ['request_state', err('human.request_state'), /already signed, cancelled or expired/, 'prepare'],
    ['host down', err('host.unavailable'), /does not answer/, 'retry'],
    ['not found', err('not_found'), /does not know this request any more/, 'prepare'],
    ['unknown refusal reason', err('human.click_refused', 'brand_new'), /brand_new/, 'terminal'],
  ]
  it.each(cases)('%s', (_n, e, text, next) => {
    const p = clickProblem(e)
    expect(`${p.title} ${p.body}`).toMatch(text)
    expect(p.next).toBe(next)
  })
  it('preserves the host reader fix', () => {
    const problem = clickProblem(new ApiError(409, { code: 'sign.convenience_readers', message: 'Reader refused', retryable: false, fix: 'HOST FIX \u202ehere' }))
    expect(problem.body).toContain('HOST FIX \u202ehere')
  })
  it('a rate limit names how long to wait', () => {
    expect(clickProblem(err('rate.limited', undefined, { retry_after_ms: 4200 })).waitMs).toBe(4200)
  })
  it('already_signed is "Already signed from another window" and waits on the request; record_unavailable is the terminal', () => {
    const signed = clickProblem(err('human.click_refused', 'already_signed'))
    expect(signed.title).toBe('Already signed from another window')
    expect(signed.next).toBe('check') // never Sign again: it re-attaches to the request's state
    const record = clickProblem(err('human.click_refused', 'record_unavailable'))
    expect(record.next).toBe('terminal')
    expect(record.body).toMatch(/orch host convenience off/)
  })
  it('intent answers: done offers Do it again, ended offers Start again, open offers nothing', () => {
    expect(clickProblem(err('intent.done')).next).toBe('again')
    expect(clickProblem(err('intent.failed')).next).toBe('prepare')
    expect(clickProblem(err('lock.busy'))).toMatchObject({ title: 'Busy', next: 'retry' })
    expect(clickProblem(err('intent.ended')).next).toBe('prepare')
    expect(clickProblem(err('intent.open')).next).toBe('open')
  })
  it('a network failure is a retry, not a crash', () => {
    expect(clickProblem(new Error('boom')).next).toBe('retry')
  })
})

it.each([null, ['terminal', 'click', 'phone']])('disables phone approval and uses the usable fallback with allowed methods %j', async (sign_methods) => {
  s.prepare.mockResolvedValue(fakeRequest())
  s.get.mockResolvedValue(state(fakeRequest()))
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'phone', fallback: 'terminal', allowed: ['terminal', 'click', 'phone'], is_default: false })
  const { dialog } = await open()
  fireEvent.click(within(dialog).getByText('Other ways'))
  const phone = within(dialog).getByRole('button', { name: 'Approve on your phone · coming soon' })
  expect(phone).toBeDisabled()
  fireEvent.click(phone)
  expect(within(dialog).queryByText('Waiting for your phone…')).toBeNull()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Sign in Terminal' }))
  await within(dialog).findByTestId('handoff-command')
  expect(c.prepare).not.toHaveBeenCalled()
})

it('omits One click from Other ways when the signer is unavailable', async () => {
  flag('unavailable')
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: ['terminal', 'click', 'phone'] } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'click', fallback: 'terminal', allowed: ['terminal', 'click', 'phone'], is_default: false })
  await mount(PLAN)
  const dialog = await screen.findByRole('dialog')
  await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Sign in Terminal' })).toBeEnabled())
  fireEvent.click(within(dialog).getByText('Other ways'))
  expect(within(dialog).queryByRole('button', { name: 'One click' })).toBeNull()
  expect(within(dialog).getByRole('button', { name: /Approve on your phone/ })).toBeDisabled()
})

it('cancels the click review before switching to a fresh terminal intent', async () => {
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: ['terminal', 'click'] } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'click', fallback: 'terminal', allowed: ['terminal', 'click'], is_default: false })
  s.prepare.mockResolvedValue(fakeRequest())
  s.get.mockResolvedValue(state(fakeRequest()))
  const { dialog, ws, ticket } = await toPanel()
  fireEvent.click(within(dialog).getByText('Other ways'))
  fireEvent.click(within(dialog).getByRole('button', { name: 'Sign in Terminal' }))
  await within(dialog).findByTestId('handoff-command')
  expect(c.cancel).toHaveBeenCalledWith(ws, REQ)
  expect(s.prepare).toHaveBeenCalledWith(ws, { action: 'approve', key: ticket.key, gate: 'plan' }, 1)
  expect(c.sign).not.toHaveBeenCalled()
})
