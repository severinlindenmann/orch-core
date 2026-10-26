import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ARM_MS, CLICK_RISK, clickProblem, type ClickCard, type ClickViewAnswer } from '@/api/click'
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
  it('leaving the screen while the review is open cancels the open click request (no slot left until it expires)', async () => {
    const { unmount } = await toPanel()
    expect(c.cancel).not.toHaveBeenCalled()
    unmount()
    await waitFor(() => expect(c.cancel).toHaveBeenCalledTimes(1))
    expect(c.cancel.mock.calls[0][1]).toBe(REQ)
  })

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
        const signs = [...d.querySelectorAll('button')].filter((b) => /^Sign( in a terminal)?$/.test(b.textContent?.trim() ?? ''))
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

  it('#1161: a sign_methods change goes the click path (core classes it click + word), not the terminal hand-off', async () => {
    flag(true)
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    const { SignPopover } = await import('./SignPopover')
    const { render } = await import('@testing-library/react')
    vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: null } as Awaited<ReturnType<typeof api.getHostSettings>>)
    vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'click', is_default: false, allowed: ['terminal', 'click', 'phone'] })
    const ws = (await api.getWorkspaces())[0]!.id
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SignPopover ws={ws} title="Change allowed ways to approve" action={{ action: 'settings_set', sign_methods: ['terminal', 'click'] }} autoStart onClose={() => {}} /></QueryClientProvider>)
    await screen.findByTestId('click-panel', {}, T)
    expect(c.prepare).toHaveBeenCalledTimes(1)
    expect(c.prepare.mock.calls[0]?.[1]).toMatchObject({ action: 'settings_set', sign_methods: ['terminal', 'click'] })
    expect(s.prepare).not.toHaveBeenCalled()
  })

  it('a network failure is a retry, not a crash', () => {
    expect(clickProblem(new Error('boom')).next).toBe('retry')
  })
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
  expect(within(dialog).queryByRole('button', { name: 'Sign in a terminal' })).toBeNull()
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
