import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ARM_MS, CLICK_RISK, HOLD_MS, type ClickViewAnswer } from '@/api/click'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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
  const fns = { onSign: vi.fn(), onCancel: vi.fn() }
  const ui = (x: ClickViewAnswer) => <ClickSignPanel answer={x} problem={null} signing={false} {...fns} {...props} />
  const view = render(ui(a))
  return { ...fns, ...view, again: (x: ClickViewAnswer) => view.rerender(ui(x)) }
}

const sign = () => screen.getByRole('button', { name: 'Sign' })
const holdSign = () => screen.getByRole('button', { name: 'Hold to sign' })
const wait = (ms: number) => act(() => void vi.advanceTimersByTime(ms))
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
  it('shows a One-click badge and keeps technical fields in collapsed Details', () => {
    mount(answer({ artifacts: 'unseen', checks: [{ repo: 'code', clean: true, attested_by: 'worker', at: '2026-10-12T11:00:00Z' }] }))
    const panel = screen.getByTestId('click-panel')
    expect(screen.getByRole('button', { name: 'One-click' })).toBeInTheDocument()
    expect(panel.querySelector('details')).not.toHaveAttribute('open')
    fireEvent.focus(screen.getByRole('button', { name: 'One-click' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent(CLICK_RISK)
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

  it('does not arm while the window has no focus, even when nothing in it is focused', () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    mount()
    act(() => void vi.advanceTimersByTime(ARM_MS * 4))
    expect(sign()).toBeDisabled()
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    act(() => void window.dispatchEvent(new Event('focus')))
    act(() => void vi.advanceTimersByTime(ARM_MS + 10))
    expect(sign()).toBeEnabled()
  })

  it('arms after one second, the signer’s own dwell', () => {
    expect(ARM_MS).toBe(1000)
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

  it('a new View resets the confirmation: disabled again, a half-done hold gone', () => {
    const word = { kind: 'word', word: 'p_abc' } as const
    const { onSign, again } = mount(answer({ confirm: word }))
    arm()
    fireEvent.pointerDown(holdSign())
    wait(HOLD_MS - 200)
    expect(screen.getByRole('progressbar')).toBeInTheDocument()
    again(answer({ confirm: word, lines: ['changed'] }, 'sha256:' + 'b'.repeat(64)))
    expect(holdSign()).toBeDisabled()
    expect(screen.queryByRole('progressbar')).toBeNull()
    wait(HOLD_MS * 2) // the old hold's remaining time passes (and the new View arms): nothing signs
    expect(onSign).not.toHaveBeenCalled()
  })

  it('is off while signing and while a rate limit runs', () => {
    const { rerender } = render(<ClickSignPanel answer={answer()} problem={null} signing onSign={vi.fn()} onCancel={vi.fn()} />)
    arm()
    expect(screen.getByRole('button', { name: /Signing/ })).toBeDisabled()
    rerender(<ClickSignPanel answer={answer()} problem={null} signing={false} onSign={vi.fn()} onCancel={vi.fn()} waitUntil={Date.now() + 5000} />)
    expect(sign()).toBeDisabled()
    act(() => void vi.advanceTimersByTime(5300))
    expect(sign()).toBeEnabled()
  })
})

describe('a word op', () => {
  const WORD = 'DEMO~a94c935c/POLICY'
  const mountWord = () => mount(answer({ confirm: { kind: 'word', word: WORD } }))

  it('shows the word in a hint the button points at, and has no field to type in', () => {
    mountWord()
    const hint = screen.getByTestId('click-hold-hint')
    expect(hint).toHaveTextContent(`Hold Sign to confirm ${WORD}`)
    expect(holdSign()).toHaveAttribute('aria-describedby', hint.id)
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Sign' })).toBeNull()
  })

  it('a short press does not sign, and neither does a click', () => {
    const { onSign } = mountWord()
    arm()
    fireEvent.pointerDown(holdSign())
    wait(HOLD_MS - 100)
    fireEvent.pointerUp(holdSign())
    fireEvent.click(holdSign())
    wait(HOLD_MS * 2)
    expect(onSign).not.toHaveBeenCalled()
    expect(screen.queryByRole('progressbar')).toBeNull()
  })

  it('a full hold signs exactly once, with the View’s word, and shows its progress meanwhile', () => {
    const { onSign } = mountWord()
    arm()
    fireEvent.pointerDown(holdSign())
    wait(HOLD_MS / 2)
    const bar = screen.getByRole('progressbar')
    expect(Number(bar.getAttribute('aria-valuenow'))).toBeGreaterThanOrEqual(40)
    expect(Number(bar.getAttribute('aria-valuenow'))).toBeLessThan(100)
    wait(HOLD_MS / 2)
    expect(onSign).toHaveBeenCalledTimes(1)
    expect(onSign).toHaveBeenCalledWith(WORD)
    wait(HOLD_MS * 2) // still held down: no second signature
    fireEvent.pointerUp(holdSign())
    fireEvent.click(holdSign())
    expect(onSign).toHaveBeenCalledTimes(1)
  })

  it('releasing early, then holding again, signs (the new hold starts from zero)', () => {
    const { onSign } = mountWord()
    arm()
    fireEvent.pointerDown(holdSign())
    wait(HOLD_MS - 300)
    fireEvent.pointerLeave(holdSign())
    fireEvent.pointerDown(holdSign())
    wait(HOLD_MS - 100)
    expect(onSign).not.toHaveBeenCalled() // not the sum of both holds
    wait(100)
    expect(onSign).toHaveBeenCalledTimes(1)
    expect(onSign).toHaveBeenCalledWith(WORD)
  })

  it('pointer cancel, blur and a secondary button do not sign', () => {
    const { onSign } = mountWord()
    arm()
    fireEvent.pointerDown(holdSign())
    wait(HOLD_MS - 100)
    fireEvent.pointerCancel(holdSign())
    fireEvent.pointerDown(holdSign())
    wait(HOLD_MS - 100)
    fireEvent.blur(holdSign())
    fireEvent.pointerDown(holdSign(), { button: 2 })
    wait(HOLD_MS * 2)
    expect(onSign).not.toHaveBeenCalled()
  })

  it('holds by keyboard: Space down starts it, auto-repeats neither restart nor finish it early, key up cancels', () => {
    const { onSign } = mountWord()
    arm()
    fireEvent.keyDown(holdSign(), { key: ' ' })
    for (let i = 0; i < 10; i++) {
      wait(100)
      fireEvent.keyDown(holdSign(), { key: ' ', repeat: true })
    }
    fireEvent.keyUp(holdSign(), { key: ' ' }) // released at 1000 ms
    wait(HOLD_MS)
    expect(onSign).not.toHaveBeenCalled()
    fireEvent.keyDown(holdSign(), { key: ' ' })
    wait(HOLD_MS - 500)
    fireEvent.keyDown(holdSign(), { key: ' ', repeat: true }) // a repeat does not restart the clock
    wait(500)
    expect(onSign).toHaveBeenCalledTimes(1)
    expect(onSign).toHaveBeenCalledWith(WORD)
    fireEvent.keyUp(holdSign(), { key: ' ' })
    fireEvent.keyDown(holdSign(), { key: 'Enter', repeat: true }) // a held key since the last hold starts nothing
    wait(HOLD_MS * 2)
    expect(onSign).toHaveBeenCalledTimes(1)
  })

  it('a hold that began before arming does not sign, even held well past it', () => {
    const { onSign } = mountWord()
    fireEvent.pointerDown(holdSign()) // disabled: React drops it
    fireEvent.keyDown(holdSign(), { key: ' ' })
    wait(ARM_MS + HOLD_MS * 2)
    expect(holdSign()).toBeEnabled()
    expect(onSign).not.toHaveBeenCalled()
    fireEvent.keyDown(holdSign(), { key: ' ', repeat: true }) // the key held since before arming
    wait(HOLD_MS * 2)
    expect(onSign).not.toHaveBeenCalled()
  })

  it('a hold is dropped when the window loses focus mid-hold', () => {
    const { onSign } = mountWord()
    arm()
    fireEvent.pointerDown(holdSign())
    wait(HOLD_MS - 100)
    act(() => void window.dispatchEvent(new Event('blur')))
    act(() => void window.dispatchEvent(new Event('focus')))
    wait(ARM_MS + HOLD_MS)
    expect(onSign).not.toHaveBeenCalled()
  })
})

describe('the other buttons', () => {
  it('Cancel is always there, and there is no terminal path beside Sign', async () => {
    vi.useRealTimers()
    const { onCancel } = mount()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: /terminal/ })).toBeNull()
  })
})

it('renders structured content while retaining the full lines in Details', () => {
  mount(answer({ content: { headline: 'Approve requirements', sections: [{ id: 'requirements', title: 'Requirements', text: 'Do the thing' }], acceptance: [{ id: 'AC1', text: 'It works' }], tasks: [{ n: 2, id: 'T-3', title: 'Check it', verify: ['npm test'] }], fields_changed: [{ field: 'title', before: 'Old', after: 'New', before_note: 'per host, not signed' }] } }))
  expect(screen.getByText('Do the thing')).toBeVisible()
  expect(screen.getByText('It works')).toBeVisible()
  expect(screen.getByText('npm test')).toBeVisible()
  expect(screen.getByText('per host, not signed')).toBeVisible()
  expect(screen.getByTestId('click-lines').closest('details')).not.toHaveAttribute('open')
})

it('keeps custody and warnings visible with structured content and technical details collapsed', () => {
  mount(answer({ content: { headline: 'Approve', notes: ['Review this'] }, artifacts: 'unseen',
    checks: [{ repo: 'dirty-repo', clean: false, attested_by: 'worker', at: '2026-10-12T11:00:00Z' }] }))
  expect(screen.getByText('Technical details').closest('details')).not.toHaveAttribute('open')
  expect(screen.getByTestId('click-custody')).toBeVisible()
  expect(screen.getByTestId('click-custody')).toHaveTextContent('One click on this computer, no fingerprint/passphrase')
  expect(screen.getByText('dirty-repo', { selector: 'p *' }).closest('p')).toBeVisible()
  expect(screen.getByTestId('click-artifacts')).toBeVisible()
  expect(screen.getByTestId('click-fingerprint')).not.toBeVisible()
  expect(screen.getByTestId('click-lines')).not.toBeVisible()
})

it('shows the full terminal lines when structured content is absent', () => {
  mount(answer({ lines: ['Fallback first line', 'Fallback second line'] }))
  expect(screen.getByText('Fallback first line')).toBeVisible()
  expect(screen.getByText('Fallback second line')).toBeVisible()
})

it('makes the badge risk text available inline on small touch screens', () => {
  mount()
  expect(screen.getByText(CLICK_RISK)).toHaveClass('sm:hidden')
})

it('K6: a removed person reads "remove: Severin", never "remove: — → Severin"', () => {
  const ID = 'p_181d953f12ab34cd56ef78901234abcd'
  const client = new QueryClient()
  client.setQueryData(['workspaces'], [{ id: WS, members: [{ person: ID, name: 'Severin' }] }])
  render(<QueryClientProvider client={client}><ClickSignPanel answer={answer({ content: { headline: 'x', fields_changed: [{ field: 'assignees: remove', before: null, after: ID }] } })} problem={null} signing={false} onSign={vi.fn()} onCancel={vi.fn()} ws={WS} /></QueryClientProvider>)
  const row = screen.getByText('assignees: remove').parentElement!
  expect(row).toHaveTextContent(/^assignees: remove: Severin/)
  expect(row).not.toHaveTextContent('—')
})

it('E-4: a field that stays as it is shows its value once, without an arrow', () => {
  render(<QueryClientProvider client={new QueryClient()}><ClickSignPanel answer={answer({ content: { headline: 'x', fields_changed: [{ field: 'repository api: folder (unchanged, per host)', before: null, after: '/work/api' }, { field: 'repository api: main branch', before: 'main', after: 'dev' }] } })} problem={null} signing={false} onSign={vi.fn()} onCancel={vi.fn()} ws={WS} /></QueryClientProvider>)
  const row = screen.getByText('repository api: folder (unchanged, per host)').parentElement!
  expect(row).toHaveTextContent('repository api: folder (unchanged, per host): /work/api')
  expect(row).not.toHaveTextContent('→')
  expect(screen.getByText('repository api: main branch').parentElement).toHaveTextContent('main → dev')
})

describe('people in the content are named from the View\'s own workspace', () => {
  const ID = 'p_181d953f12ab34cd56ef78901234abcd'
  const content = { headline: 'x', fields_changed: [{ field: 'owner', before: null, after: ID }] }
  function named(dialogWs: string) {
    const client = new QueryClient()
    client.setQueryData(['workspaces'], [{ id: WS, members: [{ person: ID, name: 'Severin' }] }, { id: 'w_other', members: [{ person: ID, name: 'Impostor' }] }])
    render(<QueryClientProvider client={client}><ClickSignPanel answer={answer({ content })} problem={null} signing={false} onSign={vi.fn()} onCancel={vi.fn()} ws={dialogWs} /></QueryClientProvider>)
    return screen.getByText('owner').parentElement!
  }
  it('uses view.workspace, not the dialog\'s workspace', () => {
    const row = named('w_other')
    expect(row).toHaveTextContent('owner: — → Severin (p_181d953f12ab…)')
    expect(row).not.toHaveTextContent('Impostor')
  })
})
