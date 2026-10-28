import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ARM_MS, CLICK_RISK, clickProblem, type ClickCard, type ClickViewAnswer } from '@/api/click'
import { setFeatures } from '@/api/features'
import { ApiError } from '@/api/types'
import { mount, spies } from '@/test/handoffHarness'
import * as gesture from './trustedPress'

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
  vi.spyOn(gesture, 'trustedPress').mockReturnValue(true) // fireEvent is untrusted: stand in for a real press of the outcome button
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

/** Opening the dialog grants no intent: the trusted press of its outcome button prepares, then the review shows. */
async function open(action: Parameters<typeof mount>[0] = PLAN, outcome = 'Approve plan') {
  flag(true)
  const m = await mount(action)
  const dialog = await screen.findByRole('dialog', {}, T)
  const b = await within(dialog).findByRole('button', { name: outcome }, T)
  await waitFor(() => expect(b).toBeEnabled(), T)
  expect(c.prepare).not.toHaveBeenCalled()
  fireEvent.click(b)
  return { ...m, dialog }
}
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
  it('the armed Sign never signs on a synthetic (untrusted) press or a key auto-repeat; a trusted press signs once', async () => {
    const { dialog } = await toPanel()
    const b = await armed(dialog)
    vi.mocked(gesture.trustedPress).mockRestore() // jsdom's own untrusted events, judged by the real rule
    press(b)
    await new Promise((r) => setTimeout(r, 100))
    expect(c.sign).not.toHaveBeenCalled()
    const real = gesture.trustedPress
    vi.spyOn(gesture, 'trustedPress').mockImplementation((e) => real({ isTrusted: true, repeat: e.repeat }))
    fireEvent.keyDown(b, { key: 'Enter' }) // began armed
    fireEvent.keyDown(document, { key: 'Enter', repeat: true }) // then held: the click lands during auto-repeat
    fireEvent.click(b)
    await new Promise((r) => setTimeout(r, 100))
    expect(c.sign).not.toHaveBeenCalled()
    fireEvent.keyUp(document, { key: 'Enter' })
    press(b)
    await waitFor(() => expect(c.sign).toHaveBeenCalledTimes(1), T)
    expect(c.sign.mock.calls[0][2]).toBe(DIGEST)
  })

  it('leaving the screen while the review is open cancels the open click request (no slot left until it expires)', async () => {
    const { unmount } = await toPanel()
    expect(c.cancel).not.toHaveBeenCalled()
    unmount()
    await waitFor(() => expect(c.cancel).toHaveBeenCalledTimes(1))
    expect(c.cancel.mock.calls[0][1]).toBe(REQ)
  })

  it('keeps Sign disabled in the footer until the review is ready', async () => {
    const { dialog } = await open()
    await within(dialog).findByTestId('click-panel', {}, T)
    expect(within(dialog).getByText('Approve the plan of DEMO-0044')).toBeInTheDocument()
    expect(await within(dialog).findByRole('button', { name: /^Sign( |$)/ }, T)).toBeDisabled() // the 1 s arm is the only way on
    expect(within(dialog).queryByRole('button', { name: 'One-click' })).toBeNull() // U5: one line and Sign, no chip
    expect(c.prepare).toHaveBeenCalledTimes(1) // one click request
    expect(s.postAction).not.toHaveBeenCalled()
    await armed(dialog)
    expect(c.sign).not.toHaveBeenCalled() // arming never signs by itself
  })

  it('never enables Sign before the content is shown (observed on every DOM change)', async () => {
    const bad: string[] = []
    const look = () => {
      for (const d of document.querySelectorAll('[data-handoff-dialog]')) {
        // The review's own Sign lives in the footer; the outcome button before it only prepares.
        const signs = [...d.querySelectorAll('[data-testid="sign-footer"] button')].filter((b) => !(b as HTMLButtonElement).disabled && /^Sign( |$)/.test(b.textContent?.trim() ?? ''))
        if (signs.length && !d.querySelector('[data-testid="click-panel"]')) bad.push(d.textContent ?? '')
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


  it('a network failure is a retry, not a crash', () => {
    expect(clickProblem(new Error('boom')).next).toBe('retry')
  })
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
    expect(s.postAction).not.toHaveBeenCalled()
    expect(c.prepare).toHaveBeenCalledTimes(1)
  })

  it('switched off between review and Sign: one sign attempt, and never a request of another kind', async () => {
    const { dialog } = await toPanel()
    c.sign.mockImplementationOnce(async () => { flag(false); throw new ApiError(409, { code: 'sign.convenience_unavailable', message: 'disabled: off', retryable: false, reason: 'disabled' }, 3) })
    press(await armed(dialog))
    await waitFor(() => expect(c.sign).toHaveBeenCalledTimes(1), T)
    await new Promise((r) => setTimeout(r, 300))
    expect(c.sign).toHaveBeenCalledTimes(1)
    expect(c.prepare).toHaveBeenCalledTimes(1)
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

  it('the outcome press opens the compact review, signs with one press and closes itself with a toast, no Close step', async () => {
    c.prepare.mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'answer', confirm: 'click' })
    c.view.mockResolvedValue(viewAnswer({ op: 'answer', content, lines: ['Answer Q2'] } as never))
    const { dialog, onClose } = await open(ANSWER, 'Send answer')
    await within(dialog).findByTestId('click-panel', {}, T)
    expect(within(dialog).getAllByRole('button', { name: 'Send answer' })).toHaveLength(1) // the review's own button; the outcome button that prepared is gone
    // what is answered is in view; the question and the other options are under Details, closed
    expect(within(dialog).getByTestId('sign-choice')).toHaveTextContent(/Your answer.*DATE \(local midnight\)/)
    const compact = within(dialog).getByTestId('sign-compact')
    expect(within(compact).getByText('abcd1234')).not.toBeVisible()
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
    const { dialog } = await open(ANSWER, 'Send answer')
    await within(dialog).findByTestId('click-panel', {}, T)
    expect(within(dialog).queryByTestId('sign-compact')).toBeNull()
    expect(within(dialog).getByText('Which date should the report use?')).toBeVisible()
    expect(within(dialog).getByText('TIMESTAMP')).not.toBeVisible()
  })
})
