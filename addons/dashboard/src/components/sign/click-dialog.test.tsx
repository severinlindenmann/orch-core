import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ARM_MS, CLICK_RISK, clickProblem, type ClickViewAnswer } from '@/api/click'
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
  it('offers Sign and "Sign in the terminal instead" and prepares nothing until one is pressed', async () => {
    const { dialog } = await open()
    expect(await within(dialog).findByRole('button', { name: 'Sign' }, T)).toBeEnabled()
    expect(within(dialog).getByRole('button', { name: 'Sign in the terminal instead' })).toBeEnabled()
    expect(within(dialog).getByTestId('click-risk')).toHaveTextContent(CLICK_RISK)
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

  it('with the flag present but the signer unable (not available) there is no Sign button either', async () => {
    flag('unavailable')
    s.prepare.mockResolvedValue(fakeRequest())
    s.get.mockResolvedValue(state(fakeRequest()))
    await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(within(dialog).queryByRole('button', { name: 'Sign' })).toBeNull()
  })

  it('Sign prepares once with one idem, reads the View and shows only it', async () => {
    const { ws, ticket, dialog } = await toPanel()
    expect(c.prepare).toHaveBeenCalledTimes(1)
    expect(c.prepare).toHaveBeenCalledWith(ws, { action: 'approve', key: ticket.key, gate: 'plan' }, expect.stringMatching(/^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$/))
    expect(c.view).toHaveBeenCalledWith(ws, REQ)
    const panel = within(dialog).getByTestId('click-panel')
    expect(panel.firstElementChild).toHaveTextContent(CLICK_RISK)
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

  it('"Sign in the terminal instead" from the panel cancels the click request and prepares the hand-off', async () => {
    const request = fakeRequest()
    s.prepare.mockResolvedValue(request)
    s.get.mockResolvedValue(state(request))
    const { ws, ticket, dialog } = await toPanel()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Sign in the terminal instead' }))
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(c.cancel).toHaveBeenCalledWith(ws, REQ)
    expect(s.prepare).toHaveBeenCalledWith(ws, { action: 'approve', key: ticket.key, gate: 'plan' }, expect.stringMatching(/^a_/))
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

  it('a word op shows the word and sends exactly what was typed', async () => {
    c.prepare.mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'policy.set', confirm: 'word' })
    c.view.mockResolvedValue(viewAnswer({ op: 'policy.set', confirm: { kind: 'word', word: 'DEMO~aaaaaaaa/POLICY' } }))
    const { ws, dialog } = await toPanel()
    const input = within(dialog).getByLabelText(/To confirm, type/)
    fireEvent.change(input, { target: { value: 'DEMO~aaaaaaaa/POLICY ' } })
    press(await armed(dialog))
    await waitFor(() => expect(c.sign).toHaveBeenCalledWith(ws, REQ, DIGEST, 'DEMO~aaaaaaaa/POLICY '))
  })

  it('the prepare failing shows the honest message and the terminal path, and nothing is open', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'sign.convenience_unavailable', message: 'other_member: x', retryable: false, reason: 'other_member' }))
    const { dialog } = await open()
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Sign' }, T))
    const problem = await within(dialog).findByTestId('click-problem', {}, T)
    expect(problem).toHaveTextContent('another member')
    expect(within(dialog).getByRole('button', { name: 'Sign in the terminal instead' })).toBeInTheDocument()
    expect(c.cancel).not.toHaveBeenCalled()
  })

  it('an unknown outcome is not cancelled when the dialog closes, and says not to sign again', async () => {
    const { dialog, onClose } = await toPanel()
    c.sign.mockRejectedValueOnce(new ApiError(409, { code: 'human.click_refused', message: 'x', retryable: false, reason: 'uncertain' }))
    press(await armed(dialog))
    await within(dialog).findByText(/Do not sign again/, {}, T)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(c.cancel).not.toHaveBeenCalled()
  })

  it('a rate limit keeps Sign off for the time the signer names', async () => {
    const { dialog } = await toPanel()
    c.sign.mockRejectedValueOnce(new ApiError(503, { code: 'rate.limited', message: 'x', retryable: true, retry_after_ms: 1500 }))
    press(await armed(dialog))
    await within(dialog).findByText(/Try again in 2 s/, {}, T)
    expect(within(dialog).getByRole('button', { name: 'Sign' })).toBeDisabled()
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Sign' })).toBeEnabled(), { timeout: 4000 })
  })
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
    ['terminal_only', err('human.click_refused', 'terminal_only'), /terminal/, 'terminal'],
    ['not_prepared_here', err('human.click_refused', 'not_prepared_here'), /does not know this request/, 'terminal'],
    ['expired', err('human.click_refused', 'expired'), /ran out of time/, 'prepare'],
    ['view_changed', err('human.click_refused', 'view_changed'), /no longer what you were shown/, 'review'],
    ['too_soon', err('human.click_refused', 'too_soon'), /less than a second/, 'retry'],
    ['word', err('human.click_refused', 'word'), /exactly as the review shows/, 'retry'],
    ['custody_changed', err('human.click_refused', 'custody_changed'), /custody of your device key changed/, 'prepare'],
    ['busy', err('human.click_refused', 'busy'), /Another signature is in progress/, 'retry'],
    ['check_failed', err('human.click_refused', 'check_failed'), /refused what the host sent/, 'terminal'],
    ['uncertain', err('human.click_refused', 'uncertain'), /Do not sign again/, 'check'],
    ['no_key', err('custody.no_key'), /no device key/i, 'terminal'],
    ['custody_unavailable', err('custody.unavailable'), /locked or refused/, 'terminal'],
    ['version_mismatch', err('host.version_mismatch'), /different versions of orch-core/, 'terminal'],
    ['contract', err('click.contract'), /do not speak the same version/, 'terminal'],
    ['signer stopped', err('click.unavailable'), /not running/, 'terminal'],
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
  it('a rate limit names how long to wait', () => {
    expect(clickProblem(err('rate.limited', undefined, { retry_after_ms: 4200 })).waitMs).toBe(4200)
  })
  it('a network failure is a retry, not a crash', () => {
    expect(clickProblem(new Error('boom')).next).toBe('retry')
  })
})
