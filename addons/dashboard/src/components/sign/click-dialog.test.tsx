import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ARM_MS, CLICK_RISK, clickProblem, type ClickCard, type ClickViewAnswer } from '@/api/click'
import { setFeatures } from '@/api/features'
import { ApiError } from '@/api/types'
import { fakeRequest, mount, spies, state } from '@/test/handoffHarness'
import { STALE_HOPS } from '@/components/sign/useHandoff'

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
let card: ClickCard | undefined
function flag(on: boolean | 'unavailable') {
  card = on === 'unavailable' ? { custody: 'convenience' as const, enabled: true, available: false, problem: 'custody_unavailable' } : on ? { custody: 'convenience' as const, enabled: true, available: true } : undefined
  if (vi.isMockFunction(api.getWorkspaces)) return // the switch can move while the page is open: the spy reads the current card
  const real = api.getWorkspaces.bind(api)
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
/** The dialog prepares on open (the click that opened it is the intent): it goes straight to the review. */
async function toPanel() {
  const m = await open()
  await within(m.dialog).findByTestId('click-panel', {}, T)
  return m
}
const armed = async (dialog: HTMLElement) => {
  const b = within(dialog).getByRole('button', { name: /^Sign( |$)/ })
  await waitFor(() => expect(b).toBeEnabled(), { timeout: ARM_MS + 2000 })
  return b
}
const press = (b: HTMLElement) => {
  fireEvent.pointerDown(b)
  fireEvent.click(b)
}

describe('the action dialog with click-to-sign', () => {
  it('opens straight on the review: content first, Sign under it, never a Sign button before the content (WYSIWYS)', async () => {
    const { dialog } = await open()
    // before the review is shown there is no Sign button at all
    expect(within(dialog).queryByRole('button', { name: /^Sign/ })).toBeNull()
    await within(dialog).findByTestId('click-panel', {}, T)
    expect(within(dialog).getByText('Approve the plan of DEMO-0044')).toBeInTheDocument()
    expect(await within(dialog).findByRole('button', { name: /^Sign( |$)/ }, T)).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Sign in the terminal instead' })).toBeNull()
    expect(within(dialog).queryByRole('button', { name: 'One-click' })).toBeNull() // U5: one line and Sign, no chip
    expect(c.prepare).toHaveBeenCalledTimes(1)
    expect(s.prepare).not.toHaveBeenCalled() // one click request, never also a terminal one
  })

  it('at no moment is a Sign button on screen before the content is (observed on every DOM change)', async () => {
    const bad: string[] = []
    const look = () => {
      for (const d of document.querySelectorAll('[data-handoff-dialog]')) {
        const signs = [...d.querySelectorAll('button')].filter((b) => /^Sign( in Terminal)?$/.test(b.textContent?.trim() ?? ''))
        if (signs.length && !d.querySelector('[data-testid="click-panel"], [data-testid="handoff-command"]')) bad.push(d.textContent ?? '')
      }
    }
    const obs = new MutationObserver(look)
    obs.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true })
    const { dialog } = await toPanel()
    look()
    obs.disconnect()
    expect(within(dialog).getByRole('button', { name: /^Sign( |$)/ })).toBeInTheDocument()
    expect(bad).toEqual([])
  })

  it('with a phone request possible, nothing is prepared until the phone query has answered', async () => {
    flag(true)
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    const { HandoffDialog } = await import('./HandoffDialog')
    const { render } = await import('@testing-library/react')
    let answer!: (v: { phones: number }) => void
    vi.spyOn(api, 'phoneAvailable').mockReturnValue(new Promise((r) => { answer = r }))
    vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: null } as Awaited<ReturnType<typeof api.getHostSettings>>)
    vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'click', is_default: false, allowed: ['terminal', 'click', 'phone'] })
    const ws = (await api.getWorkspaces())[0]!.id
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><HandoffDialog ws={ws} title="Approve plan" action={{ action: 'approve', key: 'DEMO-0044', gate: 'plan' }} phoneRequest={{ ref: 'DEMO-0044', gate: 'plan', gate_gen: 3 }} autoStart onClose={() => {}} /></QueryClientProvider>)
    await new Promise((r) => setTimeout(r, 500))
    expect(c.prepare).not.toHaveBeenCalled()
    answer({ phones: 0 })
    await screen.findByTestId('click-panel', {}, T)
    expect(c.prepare).toHaveBeenCalledTimes(1)
  })

  it('a form dialog (request changes) with click is not labelled Sign before the review', async () => {
    flag(true)
    const m = await mount({ kind: 'request_changes', gate: 'plan' })
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(await within(dialog).findByRole('button', { name: 'Request changes' }, T)).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /^Sign$/ })).toBeNull()
    void m
  })

  it('without the flag there is no Sign button and the hand-off starts as before', async () => {
    flag(false)
    s.prepare.mockResolvedValue(fakeRequest())
    s.get.mockResolvedValue(state(fakeRequest()))
    await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(within(dialog).queryByRole('button', { name: /^Sign( |$)/ })).toBeNull()
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

  it('click-to-sign switched ON while the dialog showed the terminal way: the refusal refreshes it, Try again shows the click review, no reload', async () => {
    flag(false)
    s.prepare.mockImplementationOnce(async () => { flag(true) /* the host's switch is on by now */; throw new ApiError(409, { code: 'sign.click_only', message: 'This workspace now signs with one click in the dashboard.', retryable: false }, 3) })
    await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(await within(dialog).findByText(/signs with one click/, {}, T)).toBeInTheDocument()
    expect(within(dialog).queryByText(/Use Sign here/)).toBeNull() // never points at a button that is not shown
    fireEvent.click(await within(dialog).findByRole('button', { name: /Try again|Start again|Prepare again/ }, T))
    expect(await within(dialog).findByTestId('click-panel', {}, T)).toBeInTheDocument()
  })

  it('click-to-sign switched OFF while the dialog showed one click: the refusal refreshes it, Try again shows the terminal code, no reload', async () => {
    flag(true)
    s.prepare.mockResolvedValue(fakeRequest())
    s.get.mockResolvedValue(state(fakeRequest()))
    c.prepare.mockImplementationOnce(async () => { flag(false) /* the host's switch is off by now */; throw new ApiError(409, { code: 'sign.convenience_unavailable', message: 'disabled: convenience signing is off for this workspace', retryable: false, reason: 'disabled' }, 3) })
    await mount(PLAN)
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByText(/Click-to-sign was switched off/, {}, T)
    const again = await within(dialog).findByRole('button', { name: /Try again|Start again|Prepare again/ }, T)
    fireEvent.click(again)
    expect(await within(dialog).findByTestId('handoff-command', {}, T)).toBeInTheDocument()
  })

  it('Sign prepares once with one idem, reads the View and shows only it', async () => {
    const { ws, ticket, dialog } = await toPanel()
    expect(c.prepare).toHaveBeenCalledTimes(1)
    expect(c.prepare).toHaveBeenCalledWith(ws, { action: 'approve', key: ticket.key, gate: 'plan' }, 0)
    expect(c.view).toHaveBeenCalledWith(ws, REQ)
    const panel = within(dialog).getByTestId('click-panel')
    expect(within(panel).queryByRole('button', { name: 'One-click' })).toBeNull()
    expect(within(panel).getAllByText('Technical details')).toHaveLength(1)
    expect(within(dialog).queryByTestId('handoff-command')).toBeNull()
    expect(s.prepare).not.toHaveBeenCalled()
    expect(s.postAction).not.toHaveBeenCalled()
  })

  it('signs with the digest of the View it shows, after the arming time, and says Signed', async () => {
    const { ws, dialog } = await toPanel()
    const b = within(dialog).getByRole('button', { name: /^Sign( |$)/ })
    expect(b).toBeDisabled()
    press(b)
    expect(c.sign).not.toHaveBeenCalled()
    press(await armed(dialog))
    await within(dialog).findByText('Signed', {}, T)
    expect(c.sign).toHaveBeenCalledWith(ws, REQ, DIGEST, undefined)
    expect(c.cancel.mock.calls).toEqual([])
  })

  it('a confirmed signature drops its item from Today at once and refreshes the queries', async () => {
    const { ws, ticket, dialog, client } = await toPanel()
    const item = { kind: 'approval', ticket: ticket.key, title: 't', text: 'x', since: '2026-10-19T10:00:00Z', ref: 'plan' }
    client.setQueryData(['today', ws], { now: '', workspace: ws, needs_you: [item], read_only_open: [] })
    client.setQueryData(['ticket', ws, ticket.key], ticket)
    press(await armed(dialog))
    await within(dialog).findByText('Signed', {}, T)
    expect((client.getQueryData(['today', ws]) as { needs_you: unknown[] }).needs_you).toEqual([])
    expect(client.getQueryState(['ticket', ws, ticket.key])?.isInvalidated).toBe(true)
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
    press(within(dialog).getByRole('button', { name: /^Sign( |$)/ }))
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
    expect(within(dialog).getByRole('button', { name: /^Sign( |$)/ })).toBeDisabled()
  })

  it('a word op asks for the word and, once typed, sends the View’s word on Sign', async () => {
    c.prepare.mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'policy.set', confirm: 'word' })
    c.view.mockResolvedValue(viewAnswer({ op: 'policy.set', confirm: { kind: 'word', word: 'DEMO~aaaaaaaa/POLICY' } }))
    const { ws, dialog } = await toPanel()
    expect(within(dialog).getByTestId('typed-word')).toHaveTextContent('Type DEMO~aaaaaaaa/POLICY to confirm')
    const b = within(dialog).getByRole('button', { name: /^Sign/ })
    await new Promise(r => setTimeout(r, ARM_MS + 200))
    expect(b).toBeDisabled() // armed, but the word is not typed
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'DEMO~aaaaaaaa/POLICY' } })
    await waitFor(() => expect(b).toBeEnabled())
    press(b)
    await waitFor(() => expect(c.sign).toHaveBeenCalledWith(ws, REQ, DIGEST, 'DEMO~aaaaaaaa/POLICY'), { timeout: 2000 })
    expect(c.sign).toHaveBeenCalledTimes(1)
  })

  it('the prepare failing shows the honest message and no terminal path, and nothing is open', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'sign.convenience_unavailable', message: 'other_member: x', retryable: false, reason: 'other_member' }))
    const { dialog } = await open()
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
    expect(within(dialog).queryByRole('button', { name: /^Sign( |$)/ })).toBeNull()
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
    await within(m.dialog).findByTestId('click-problem', {}, T)
    expect(within(m.dialog).queryByTestId('click-panel')).toBeNull()
  })

  it('shows the custody and the device of the View', async () => {
    const { dialog } = await toPanel()
    expect(within(dialog).getByTestId('click-custody')).toHaveTextContent('One click on this computer, no fingerprint/passphrase') // under Technical details, no orange line
    expect(within(dialog).getByTestId('sign-method')).toHaveTextContent('Sign on this computer')
    expect(within(dialog).queryByText('Other ways')).toBeNull()
    expect(within(dialog).getByText('d_x').closest('details')).not.toHaveAttribute('open')
  })

  it('a rate limit keeps Sign off for the time the signer names (fake timers: no wall-clock race)', async () => {
    const { dialog } = await toPanel()
    c.sign.mockRejectedValueOnce(new ApiError(503, { code: 'rate.limited', message: 'x', retryable: true, retry_after_ms: 5000 }, 3))
    press(await armed(dialog))
    await within(dialog).findByText(/Try again in 5 s/, {}, T)
    await waitFor(() => expect(within(dialog).getByRole('button', { name: /^Sign( |$)/ })).toBeDisabled())
    // The clock jumps past the wait (only Date is faked); the panel's own ticker notices on its next tick.
    vi.useFakeTimers({ toFake: ['Date'], now: Date.now() + 5600 })
    try {
      await waitFor(() => expect(within(dialog).getByRole('button', { name: /^Sign( |$)/ })).toBeEnabled(), T)
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
    expect(await within(m.dialog).findByText('Already done', {}, T)).toBeInTheDocument()
    expect(m.dialog).toHaveTextContent('A similar comment with the same text was signed 2 min ago')
    expect(m.dialog).toHaveTextContent('HOST-SIGNED comment text')
    expect(within(m.dialog).getByText(/A similar comment/).closest('p')).toHaveAttribute('title', expect.stringMatching(/^202/))
    // A-1: the host's record is never part of the sentence: it is folded under "Technical details"
    expect(within(m.dialog).getByText(/A similar comment/).closest('p')).not.toHaveTextContent('HOST-SIGNED')
    expect(within(m.dialog).getByTestId('technical-details')).not.toHaveAttribute('open')
    expect(c.prepare).toHaveBeenCalledTimes(1) // nothing starts again by itself
    expect(within(m.dialog).queryByRole('button', { name: /^Sign$|Sign in the terminal instead/ })).toBeNull()
    fireEvent.click(within(m.dialog).getByRole('button', { name: 'Do it again' }))
    await within(m.dialog).findByTestId('click-panel', {}, T).catch(() => undefined)
    await waitFor(() => expect(c.prepare).toHaveBeenCalledTimes(2), T)
    expect(c.prepare.mock.calls.map((call) => call[2])).toEqual([0, 1])
  })

  it('an add-on that is already as asked says so in plain words: no raw JSON, no Do it again', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'addon.already', message: 'Terminals is already on.', retryable: false }, 1))
    const m = await open()
    expect(await within(m.dialog).findByText('Already done', {}, T)).toBeInTheDocument()
    expect(m.dialog).toHaveTextContent('Terminals is already on.')
    expect(m.dialog).not.toHaveTextContent(/event_id|package_sha256|\{"/)
    expect(within(m.dialog).queryByRole('button', { name: /Do it again|^Sign$|Try again/ })).toBeNull()
  })

  it('a submitted failure needs an explicit Start again and advances retry', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'intent.failed', message: 'The earlier attempt was submitted but did not land (gate.stale). It was not signed into the log.', retryable: false }))
    const m = await open()
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
    fireEvent.click(await within(m.dialog).findByRole('button', { name: 'Start again' }, T))
    await waitFor(() => expect(c.prepare).toHaveBeenCalledTimes(2), T)
    expect(c.prepare.mock.calls.map((call) => call[2])).toEqual([0, 1])
  })

  it('a cancelled or expired earlier attempt does not block a fresh open: it prepares anew once (retry 1) and shows the review', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'intent.ended', message: 'The earlier attempt is cancelled.', retryable: false }, 5))
    const m = await open()
    await within(m.dialog).findByTestId('click-panel', {}, T)
    expect(c.prepare.mock.calls.map((call) => call[2])).toEqual([0, 1])
    expect(m.dialog).not.toHaveTextContent('The earlier attempt ended')
  })

  it('an action cancelled several times before opens straight to a fresh review (walks past every ended attempt)', async () => {
    const ended = () => new ApiError(409, { code: 'intent.ended', message: 'The earlier attempt is cancelled.', retryable: false }, 5)
    c.prepare.mockRejectedValueOnce(ended()).mockRejectedValueOnce(ended()).mockRejectedValueOnce(ended())
    const m = await open()
    await within(m.dialog).findByTestId('click-panel', {}, T)
    expect(c.prepare.mock.calls.map((call) => call[2])).toEqual([0, 1, 2, 3])
    expect(m.dialog).not.toHaveTextContent('The earlier attempt ended')
  })

  it('a host that only answers "ended" is said after a bounded walk, with an explicit Start again (no endless loop)', async () => {
    c.prepare.mockRejectedValue(new ApiError(409, { code: 'intent.ended', message: 'The earlier attempt is cancelled.', retryable: false }, 5))
    const m = await open()
    fireEvent.click(await within(m.dialog).findByRole('button', { name: 'Start again' }, T))
    await waitFor(() => expect(c.prepare).toHaveBeenCalledTimes(STALE_HOPS + 2), T)
    expect(c.prepare.mock.calls.map((call) => call[2])).toEqual([...Array(STALE_HOPS + 2).keys()])
  })

  it('retention expiry waits for Start again before preparing with retry 1', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'intent.expired', message: 'Start again to make a new one.', retryable: false }, 5))
    const m = await open()
    expect(await within(m.dialog).findByText('This attempt is too old; start again', {}, T)).toBeInTheDocument()
    expect(c.prepare).toHaveBeenCalledTimes(1)
    fireEvent.click(within(m.dialog).getByRole('button', { name: 'Start again' }))
    await waitFor(() => expect(c.prepare).toHaveBeenCalledTimes(2), T)
    expect(c.prepare.mock.calls.map((call) => call[2])).toEqual([0, 1])
  })

  it('an attempt that is open at the host (terminal, or before a restart) is named and nothing prepares again', async () => {
    c.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'intent.open', message: 'x', retryable: false }, 5))
    const m = await open()
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
    ['disabled', err('sign.convenience_unavailable', 'disabled'), /switched off/, 'retry'],
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
  it('names the real workspace prefix in the switched-off text, never a literal <prefix>', async () => {
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    const { ClickProblemBox } = await import('./RefusalDevices')
    const { render } = await import('@testing-library/react')
    const w = (await api.getWorkspaces())[0]!
    const problem = clickProblem(err('sign.convenience_unavailable', 'disabled'))
    expect(problem.body).toContain('<prefix>')
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ClickProblemBox problem={problem} ws={w.id} /></QueryClientProvider>)
    await waitFor(() => expect(screen.getByTestId('click-problem').textContent).toContain(`--workspace ${w.prefix} --in-place`), T)
    expect(screen.getByTestId('click-problem').textContent).not.toContain('<prefix>')
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
  await within(dialog).findByTestId('handoff-command', {}, T)
  // No phone is paired and none was asked for: Phone is not offered at all (no "coming soon" button).
  expect(within(dialog).queryByRole('button', { name: /Approve on your phone/ })).toBeNull()
  expect(c.prepare).not.toHaveBeenCalled()
  expect(s.prepare).toHaveBeenCalledTimes(1)
})

it('without the signer the terminal request opens and there is no Other ways', async () => {
  flag('unavailable')
  s.prepare.mockResolvedValue(fakeRequest())
  s.get.mockResolvedValue(state(fakeRequest()))
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: ['terminal', 'click', 'phone'] } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'click', fallback: 'terminal', allowed: ['terminal', 'click', 'phone'], is_default: false })
  await mount(PLAN)
  const dialog = await screen.findByRole('dialog')
  await within(dialog).findByTestId('handoff-command', {}, T)
  // Nothing usable is left: no click (signer unavailable), no phone (none paired), so no "Other ways" at all.
  expect(within(dialog).queryByText('Other ways')).toBeNull()
  expect(within(dialog).queryByRole('button', { name: 'One click' })).toBeNull()
  expect(within(dialog).queryByRole('button', { name: /Approve on your phone/ })).toBeNull()
})

it('has no "Other ways": the method follows My settings, with the phone as the only quiet alternative', async () => {
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: ['terminal', 'click'] } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'click', fallback: 'terminal', allowed: ['terminal', 'click'], is_default: false })
  s.prepare.mockResolvedValue(fakeRequest())
  s.get.mockResolvedValue(state(fakeRequest()))
  const { dialog } = await toPanel()
  expect(within(dialog).queryByText('Other ways')).toBeNull()
  expect(within(dialog).queryByRole('button', { name: 'Sign in Terminal' })).toBeNull()
  expect(within(dialog).queryByRole('button', { name: 'Use my phone instead' })).toBeNull() // no phone paired
  expect(s.prepare).not.toHaveBeenCalled()
})

describe('[rev818] adversarial: the switch moving never re-binds or signs an open request', () => {
  it('a stale-method event while the click review is open: the review stays, nothing is signed, cancelled or re-prepared', async () => {
    const { dialog } = await toPanel()
    flag(false)
    act(() => { window.dispatchEvent(new Event('orch:signing-method-stale')) })
    await new Promise((r) => setTimeout(r, 300))
    expect(within(dialog).getByTestId('click-panel')).toBeInTheDocument()
    expect(c.sign).not.toHaveBeenCalled()
    expect(c.cancel).not.toHaveBeenCalled()
    expect(s.prepare).not.toHaveBeenCalled()
    expect(c.prepare).toHaveBeenCalledTimes(1)
  })

  it('switched off between review and Sign: one sign attempt, Try again cancels the click request BEFORE a new terminal request', async () => {
    s.prepare.mockResolvedValue(fakeRequest())
    s.get.mockResolvedValue(state(fakeRequest()))
    const { dialog } = await toPanel()
    c.sign.mockImplementationOnce(async () => { flag(false); throw new ApiError(409, { code: 'sign.convenience_unavailable', message: 'disabled: off', retryable: false, reason: 'disabled' }, 3) })
    press(await armed(dialog))
    await within(dialog).findByTestId('click-problem', {}, T)
    // The text says Try again, so the button is there (the request is still open: a sign refusal keeps it)
    const again = await within(dialog).findByRole('button', { name: /Try again/ }, T)
    expect(c.sign).toHaveBeenCalledTimes(1)
    fireEvent.click(again)
    await within(dialog).findByTestId('handoff-command', {}, T)
    expect(c.cancel).toHaveBeenCalledTimes(1) // the click request is cancelled before the new terminal request
    expect(c.sign).toHaveBeenCalledTimes(1) // never signed another way
    expect(s.prepare).toHaveBeenCalledTimes(1)
    expect(s.postAction).not.toHaveBeenCalled()
  })
})

describe('answering a question in one-click mode', () => {
  const ANSWER = { kind: 'answer', question: 'Q2', option: 'date' } as const
  const content = {
    headline: 'Answer “Which date?” on DEMO-0043 with DATE (local midnight)',
    sections: [{ id: 'question', title: 'Question', text: 'Which date should the report use?' }],
    ticket: { key: 'DEMO-0043', uid_tail: 'abcd1234' },
    fields_changed: [
      { field: 'your choice', before: null, after: 'DATE (local midnight)' },
      { field: 'option b', before: null, after: 'TIMESTAMP' },
    ],
  }

  it('opens straight on the compact review, signs with one press and closes itself with a toast, no Close step', async () => {
    c.prepare.mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'answer', confirm: 'click' })
    c.view.mockResolvedValue(viewAnswer({ op: 'answer', content, lines: ['Answer Q2'] } as never))
    const { dialog, onClose } = await open(ANSWER)
    await within(dialog).findByTestId('click-panel', {}, T)
    expect(within(dialog).getAllByRole('button', { name: 'Send answer' })).toHaveLength(1) // the review's own button, no first screen before it
    // what is answered is in view; the question and the other options are under Details, closed
    expect(within(dialog).getByTestId('sign-choice')).toHaveTextContent(/Your answer.*DATE \(local midnight\)/)
    const compact = within(dialog).getByTestId('sign-compact')
    const shown = within(compact).getAllByTestId('sign-ticket').find((e) => !e.closest('details'))! // outside Details
    expect(shown).toHaveTextContent('DEMO-0043')
    expect(shown).toHaveTextContent('abcd1234')
    expect(compact).toHaveTextContent('Which date should the report use?')
    const exact = within(dialog).getByTestId('sign-exact')
    expect(exact).not.toHaveAttribute('open')
    expect(within(exact).getByText('TIMESTAMP')).toBeInTheDocument() // the full signed values are still in the page
    const b = within(dialog).getByRole('button', { name: 'Send answer' })
    await waitFor(() => expect(b).toBeEnabled(), { timeout: ARM_MS + 2000 })
    press(b)
    await waitFor(() => expect(onClose).toHaveBeenCalled(), T) // the dialog's owner closes it; nothing says "Signed" to dismiss
    expect(c.sign).toHaveBeenCalledTimes(1)
    expect(c.sign.mock.calls[0][2]).toBe(DIGEST)
  })

  it('a review without the fields compact expects is shown in full, never compact', async () => {
    c.prepare.mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'answer', confirm: 'click' })
    c.view.mockResolvedValue(viewAnswer({ op: 'answer', content: { headline: 'Answer', sections: [{ id: 'question', title: 'Question', text: 'Which date should the report use?' }], fields_changed: [{ field: 'option b', before: null, after: 'TIMESTAMP' }] }, lines: ['Answer Q2'] } as never))
    const { dialog } = await open(ANSWER)
    await within(dialog).findByTestId('click-panel', {}, T)
    expect(within(dialog).queryByTestId('sign-compact')).toBeNull()
    expect(within(dialog).getByText('Which date should the report use?')).toBeVisible()
    expect(within(dialog).getByText('TIMESTAMP')).toBeVisible()
  })
})
