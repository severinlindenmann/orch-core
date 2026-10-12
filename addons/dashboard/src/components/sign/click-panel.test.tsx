import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ARM_MS, CLICK_RISK, type ClickViewAnswer } from '@/api/click'
import { ClickSignPanel } from './ClickSignPanel'

const WS = '705d40abbb8c1c90354a1acaa94c935c'
const answer = (over: Partial<ClickViewAnswer['view']> = {}, digest = 'sha256:' + 'a'.repeat(64)): ClickViewAnswer => ({
  view_digest: digest,
  view: {
    risk: CLICK_RISK,
    op: 'approve',
    workspace: { id: WS, name: 'billing', prefix: 'DEMO' },
    fingerprint: 'abcdef012345',
    lines: ['Approve the plan of DEMO-0001', '  by Demo Owner', ''],
    checks: [],
    confirm: { kind: 'click' },
    expires_at: '2026-10-12T12:10:00Z',
    auth: 'convenience',
    device: 'd_x',
    artifacts: null,
    ...over,
  },
})

function mount(a = answer(), props: Partial<React.ComponentProps<typeof ClickSignPanel>> = {}) {
  const fns = { onSign: vi.fn(), onCancel: vi.fn(), onTerminal: vi.fn() }
  const ui = (x: ClickViewAnswer) => <ClickSignPanel answer={x} problem={null} signing={false} {...fns} {...props} />
  const view = render(ui(a))
  return { ...fns, ...view, again: (x: ClickViewAnswer) => view.rerender(ui(x)) }
}

const sign = () => screen.getByRole('button', { name: 'Sign' })
const arm = () => act(() => void vi.advanceTimersByTime(ARM_MS + 10))
/** A press that begins armed: pointer down, then click (what a person's click is). */
const press = (el: HTMLElement) => {
  fireEvent.pointerDown(el)
  fireEvent.click(el)
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('the sign panel shows only the signer’s View', () => {
  it('puts the verbatim risk line first, then the View’s fields', () => {
    mount(answer({ artifacts: 'unseen', checks: [{ repo: 'code', clean: true, attested_by: 'worker', at: '2026-10-12T11:00:00Z' }] }))
    const panel = screen.getByTestId('click-panel')
    expect(panel.firstElementChild).toBe(screen.getByTestId('click-risk'))
    expect(screen.getByTestId('click-risk').textContent).toBe(CLICK_RISK)
    expect(CLICK_RISK).toBe('Convenience signing: agents running as you, or anything that takes over this dashboard, could sign.')
    expect(panel).toHaveTextContent('approve')
    expect(screen.getByTestId('click-workspace')).toHaveTextContent('id a94c935c')
    expect(screen.getByTestId('click-workspace')).toHaveTextContent('DEMO')
    expect(screen.getByTestId('click-workspace')).toHaveTextContent('billing')
    expect(screen.getByTestId('click-fingerprint')).toHaveTextContent('abcdef012345')
    expect(screen.getByTestId('click-lines')).toHaveTextContent('Approve the plan of DEMO-0001')
    expect(screen.getByTestId('click-checks')).toHaveTextContent('Repository code: clean, checked by worker')
    expect(screen.getByTestId('click-artifacts')).toHaveTextContent('did not see the artifact bytes')
  })

  it('shows the workspace badge before the name, and the name with hidden characters made visible', () => {
    mount(answer({ workspace: { id: WS, name: 'bill‮ing', prefix: 'DEMO' } }))
    const ws = screen.getByTestId('click-workspace')
    expect(ws.firstElementChild).toHaveAttribute('data-ws-id')
    expect(ws).toHaveTextContent('bill\\u{202e}ing')
  })

  it('draws `lines` as text, never as HTML', () => {
    const evil = '<img src=x onerror=alert(1)><b>bold</b>'
    mount(answer({ lines: [evil] }))
    const box = screen.getByTestId('click-lines')
    expect(box.textContent).toContain(evil)
    expect(box.querySelector('img, b, script')).toBeNull()
  })

  it('has no preview of its own: no host fingerprint, no ticket text, nothing but the View', () => {
    mount()
    const text = screen.getByTestId('click-panel').textContent ?? ''
    expect(text).not.toMatch(/Preview|Content hash|orch sign/)
  })
})

describe('Sign is armed by presentation', () => {
  it('is off for 500 ms, then on', () => {
    mount()
    expect(sign()).toBeDisabled()
    act(() => void vi.advanceTimersByTime(ARM_MS - 50))
    expect(sign()).toBeDisabled()
    act(() => void vi.advanceTimersByTime(60))
    expect(sign()).toBeEnabled()
  })

  it('restarts the timer when the window loses focus and when the page is hidden', () => {
    mount()
    act(() => void vi.advanceTimersByTime(300))
    act(() => void window.dispatchEvent(new Event('blur')))
    act(() => void vi.advanceTimersByTime(ARM_MS * 3))
    expect(sign()).toBeDisabled() // blurred: never arms
    act(() => void window.dispatchEvent(new Event('focus')))
    act(() => void vi.advanceTimersByTime(ARM_MS - 100))
    expect(sign()).toBeDisabled()
    act(() => void vi.advanceTimersByTime(110))
    expect(sign()).toBeEnabled()
    // Hidden: off again, and a full 500 ms of being visible again turns it back on.
    const vis = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    act(() => void document.dispatchEvent(new Event('visibilitychange')))
    expect(sign()).toBeDisabled()
    vis.mockReturnValue('visible')
    act(() => void document.dispatchEvent(new Event('visibilitychange')))
    act(() => void vi.advanceTimersByTime(ARM_MS - 100))
    expect(sign()).toBeDisabled()
    act(() => void vi.advanceTimersByTime(110))
    expect(sign()).toBeEnabled()
  })

  it('does not arm in a page that is not visible', () => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    mount()
    act(() => void vi.advanceTimersByTime(ARM_MS * 4))
    expect(sign()).toBeDisabled()
  })

  it('ignores a press that began before the button was armed, and a held-down key', () => {
    const { onSign } = mount()
    fireEvent.pointerDown(sign()) // pressed during the first 500 ms (disabled: React drops it)
    arm()
    fireEvent.click(sign()) // released after arming: this click did not begin armed
    expect(onSign).not.toHaveBeenCalled()
    fireEvent.keyDown(sign(), { key: 'Enter', repeat: true }) // auto-repeat of a key held since before
    fireEvent.click(sign())
    expect(onSign).not.toHaveBeenCalled()
    fireEvent.click(sign()) // a bare click with no press at all (a script) does not count either
    expect(onSign).not.toHaveBeenCalled()
  })

  it('signs on a press that began armed, by pointer or by key, once', () => {
    const { onSign } = mount()
    arm()
    press(sign())
    expect(onSign).toHaveBeenCalledTimes(1)
    expect(onSign).toHaveBeenCalledWith(undefined)
    fireEvent.click(sign()) // the same activation is not reusable
    expect(onSign).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(sign(), { key: 'Enter' })
    fireEvent.click(sign())
    expect(onSign).toHaveBeenCalledTimes(2)
  })

  it('a new View resets the confirmation: disabled again, the typed word gone', () => {
    const word = { kind: 'word', word: 'p_abc' } as const
    const { again } = mount(answer({ confirm: word }))
    arm()
    fireEvent.change(screen.getByLabelText(/To confirm, type/), { target: { value: 'p_abc' } })
    expect(sign()).toBeEnabled()
    again(answer({ confirm: word, lines: ['changed'] }, 'sha256:' + 'b'.repeat(64)))
    expect(sign()).toBeDisabled()
    expect(screen.getByLabelText(/To confirm, type/)).toHaveValue('')
  })

  it('is off while signing and while a rate limit runs', () => {
    const { rerender } = render(<ClickSignPanel answer={answer()} problem={null} signing onSign={vi.fn()} onCancel={vi.fn()} onTerminal={vi.fn()} />)
    arm()
    expect(screen.getByRole('button', { name: /Signing/ })).toBeDisabled()
    rerender(<ClickSignPanel answer={answer()} problem={null} signing={false} onSign={vi.fn()} onCancel={vi.fn()} onTerminal={vi.fn()} waitUntil={Date.now() + 5000} />)
    expect(sign()).toBeDisabled()
    act(() => void vi.advanceTimersByTime(5300))
    expect(sign()).toBeEnabled()
  })
})

describe('a word op', () => {
  it('shows the word the View asks for and sends exactly what was typed', () => {
    const { onSign } = mount(answer({ confirm: { kind: 'word', word: 'DEMO~a94c935c/POLICY' } }))
    expect(screen.getByLabelText(/To confirm, type/)).toBeInTheDocument()
    expect(screen.getByTestId('click-panel')).toHaveTextContent('DEMO~a94c935c/POLICY')
    arm()
    expect(sign()).toBeDisabled() // nothing typed
    fireEvent.change(screen.getByLabelText(/To confirm, type/), { target: { value: '  demo~a94c935c/policy ' } })
    press(sign())
    expect(onSign).toHaveBeenCalledWith('  demo~a94c935c/policy ') // not trimmed, not corrected
  })
})

describe('the other buttons', () => {
  it('Cancel and the terminal path are always there', async () => {
    vi.useRealTimers()
    const { onCancel, onTerminal } = mount()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await userEvent.click(screen.getByRole('button', { name: 'Sign in the terminal instead' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onTerminal).toHaveBeenCalledTimes(1)
  })
})
