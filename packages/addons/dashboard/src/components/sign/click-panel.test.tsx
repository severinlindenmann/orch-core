import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ARM_MS, CLICK_RISK, type ClickViewAnswer } from '@/api/click'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ClickSignPanel } from './ClickSignPanel'
import { storyOf } from './story'
import * as gesture from './trustedPress'

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
const wait = (ms: number) => act(() => void vi.advanceTimersByTime(ms))
const arm = () => act(() => void vi.advanceTimersByTime(ARM_MS + 10))
/** A press that begins armed: pointer down, then click (what a person's click is). */
const press = (el: HTMLElement) => {
  fireEvent.pointerDown(el)
  fireEvent.click(el)
}
const realTrusted = gesture.trustedPress
/** Every press from here on is a trusted, non-repeat gesture, judged by the real rule (a key repeat still blocks it). */
const trusted = () => vi.spyOn(gesture, 'trustedPress').mockImplementation((e) => realTrusted({ isTrusted: true, repeat: e.repeat }))

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('the sign panel shows only the signer’s View', () => {
  it('keeps the custody and technical fields in the one collapsed Details', () => {
    mount(answer({ artifacts: 'unseen', checks: [{ repo: 'code', clean: true, attested_by: 'worker', at: '2026-10-12T11:00:00Z' }] }))
    const panel = screen.getByTestId('click-panel')
    expect(screen.queryByRole('button', { name: 'One-click' })).toBeNull()
    expect(screen.getAllByText('Technical details')).toHaveLength(1) // U3: ONE Technical details
    expect(screen.getByText('Technical details').closest('details')).not.toHaveAttribute('open')
    // the custody is a technical fact now (no orange line in the main view); the risk notice lives in Settings
    expect(screen.queryByTestId('click-risk-more')).toBeNull()
    expect(panel.querySelectorAll('details')).toHaveLength(1)
    expect(screen.getByTestId('click-custody')).toHaveTextContent('One click on this computer, no fingerprint/passphrase')
    expect(screen.getByTestId('click-custody')).not.toBeVisible()
    expect(CLICK_RISK).toBe('Convenience signing: agents running as you, or anything that takes over this dashboard, could sign.')
    expect(panel).toHaveTextContent('approve')
    expect(screen.getByTestId('click-workspace')).toHaveTextContent('a94c935c')
    expect(screen.getByTestId('click-workspace')).toHaveTextContent('DEMO')
    expect(screen.getByTestId('click-workspace')).toHaveTextContent('billing')
    expect(screen.getByTestId('click-fingerprint')).toHaveTextContent('abcdef012345')
    expect(screen.getByTestId('click-lines')).toHaveTextContent('Approve the plan of DEMO-0001')
    expect(screen.getByTestId('click-checks')).toHaveTextContent('Repository code: clean, checked by worker')
    expect(screen.getByTestId('click-artifacts')).toHaveTextContent('The files could not be checked before signing.')
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

  it('says "Ready in a moment" while disabled, never signs by itself, and names the real button after a too-soon refusal', () => {
    const onSign = vi.fn()
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ClickSignPanel
          answer={answer()}
          signing={false}
          onSign={onSign}
          onCancel={vi.fn()}
          label="Approve and turn on"
          problem={{ title: 'Too soon', body: 'Nothing was signed. Press the button again.', next: 'retry' }}
        />
      </QueryClientProvider>,
    )
    const b = screen.getByRole('button', { name: 'Approve and turn on' })
    expect(b).toBeDisabled()
    expect(screen.getByTestId('sign-arming')).toHaveTextContent('Ready in a moment')
    expect(screen.getByText(/Press Approve and turn on again/)).toBeInTheDocument()
    arm()
    expect(b).toBeEnabled()
    expect(screen.queryByTestId('sign-arming')).toBeNull()
    expect(onSign).not.toHaveBeenCalled()
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
    trusted() // even trusted presses: the began-armed rule alone refuses these
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

  it('never signs on a synthetic (untrusted) click, even one that began armed', () => {
    const { onSign } = mount()
    arm()
    press(sign()) // jsdom events are untrusted: what a script's click is
    fireEvent.keyDown(sign(), { key: 'Enter' })
    fireEvent.click(sign())
    expect(onSign).not.toHaveBeenCalled()
  })

  it('never signs on a key auto-repeat, even a trusted one', () => {
    trusted()
    const { onSign } = mount()
    arm()
    fireEvent.keyDown(sign(), { key: 'Enter' }) // began armed
    fireEvent.keyDown(document, { key: 'Enter', repeat: true }) // the key is held: auto-repeat before the click lands
    fireEvent.click(sign())
    expect(onSign).not.toHaveBeenCalled()
    fireEvent.keyUp(document, { key: 'Enter' })
  })

  it('signs on a trusted press that began armed, by pointer or by key, once', () => {
    trusted()
    const { onSign } = mount()
    arm()
    press(sign())
    expect(onSign).toHaveBeenCalledTimes(1)
    expect(onSign).toHaveBeenCalledWith()
    fireEvent.click(sign()) // the same activation is not reusable
    expect(onSign).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(sign(), { key: 'Enter' })
    fireEvent.click(sign())
    expect(onSign).toHaveBeenCalledTimes(2)
  })

  it('a new View resets the confirmation: disabled again until armed again', () => {
    const { onSign, again } = mount(answer())
    arm()
    expect(sign()).toBeEnabled()
    again(answer({ lines: ['changed'] }, 'sha256:' + 'b'.repeat(64)))
    expect(sign()).toBeDisabled()
    wait(ARM_MS * 2)
    expect(sign()).toBeEnabled()
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

describe('C3: one press (a contract-4 View of a request prepared by a trusted press)', () => {
  const HEADLINE = 'Approve the plan of DEMO-0001'
  const v4 = (risk: string[] = [], over: Partial<ClickViewAnswer['view']> = {}) => answer({ headline: HEADLINE, risk_lines: risk, ...over })
  const armed = (a = v4(), props: Partial<React.ComponentProps<typeof ClickSignPanel>> = {}) => {
    const onDisarm = vi.fn()
    return { onDisarm, ...mount(a, { pressed: true, onDisarm, ...props }) }
  }
  const headline = () => screen.getByRole('button', { name: HEADLINE })

  it('expires an arm blocked at elapse even if the caller becomes ready later', () => {
    const props = { pressed: true, ready: false, onSign: vi.fn(), onCancel: vi.fn(), onDisarm: vi.fn() }
    const a = v4()
    const { rerender } = render(<ClickSignPanel answer={a} problem={null} signing={false} {...props} />)
    arm()
    expect(props.onDisarm).toHaveBeenCalledTimes(1)
    rerender(<ClickSignPanel answer={a} problem={null} signing={false} {...props} ready />)
    arm()
    expect(props.onSign).not.toHaveBeenCalled()
  })

  it('requires every risk line to intersect the viewport for a full dwell', () => {
    let notify: IntersectionObserverCallback = () => {}
    const observed: Element[] = []
    vi.stubGlobal('IntersectionObserver', class {
      constructor(callback: IntersectionObserverCallback) { notify = callback }
      observe(el: Element) { observed.push(el) }
      disconnect() {}
    })
    trusted()
    const { onSign } = armed(v4(['Risk one', 'Risk two']))
    const visibility = (shown: boolean) => act(() => notify(observed.map(target => ({
      target, isIntersecting: shown, intersectionRatio: shown ? 1 : 0,
    } as IntersectionObserverEntry)), {} as IntersectionObserver))
    arm()
    press(sign())
    expect(onSign).not.toHaveBeenCalled()
    visibility(true)
    wait(500)
    visibility(false)
    arm()
    visibility(true)
    wait(990)
    press(sign())
    expect(onSign).not.toHaveBeenCalled()
    wait(20)
    press(sign())
    expect(onSign).toHaveBeenCalledTimes(1)
  })

  it('the button shows the signer’s headline with a progress fill; the signature is asked for at 1.0 s, never at 0.99 s, once', () => {
    const { onSign, onDisarm } = armed()
    expect(headline()).toBeEnabled()
    expect(screen.getByTestId('sign-progress')).toBeInTheDocument()
    wait(990)
    expect(onSign).not.toHaveBeenCalled()
    wait(10)
    expect(onSign).toHaveBeenCalledTimes(1)
    wait(ARM_MS * 3)
    expect(onSign).toHaveBeenCalledTimes(1)
    expect(onDisarm).not.toHaveBeenCalled()
  })

  it.each<[string, () => void]>([
    ['Esc', () => void fireEvent.keyDown(document, { key: 'Escape' })],
    ['a second press', () => press(headline())],
    ['a press elsewhere', () => void fireEvent.pointerDown(document.body)],
    ['window blur', () => void window.dispatchEvent(new Event('blur'))],
    ['a hidden page', () => {
      vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
      document.dispatchEvent(new Event('visibilitychange'))
    }],
  ])('%s at 0.5 s cancels the arm: nothing is signed and the request is cancelled', (_name, trigger) => {
    const { onSign, onDisarm, onCancel } = armed()
    wait(ARM_MS / 2)
    act(() => {
      trigger()
    })
    expect(onDisarm).toHaveBeenCalledTimes(1)
    wait(ARM_MS * 3)
    expect(onSign).not.toHaveBeenCalled()
    expect(onCancel).not.toHaveBeenCalled() // the arm's own cancel (a tombstone), not the dialog's Cancel
  })

  it('a press inside the panel (reading, selecting) does not cancel the arm', () => {
    const { onSign, onDisarm } = armed()
    wait(ARM_MS / 2)
    fireEvent.pointerDown(screen.getByTestId('click-lines'))
    wait(ARM_MS / 2)
    expect(onDisarm).not.toHaveBeenCalled()
    expect(onSign).toHaveBeenCalledTimes(1)
  })

  it('opening "i" during the arm stops it; a trusted press inside "i" starts a new arm that signs 1 s later', () => {
    trusted()
    const { onSign, onDisarm } = armed()
    wait(ARM_MS / 2)
    const details = screen.getByText('Technical details').closest('details')!
    act(() => {
      details.open = true
      details.dispatchEvent(new Event('toggle'))
    })
    wait(ARM_MS * 3)
    expect(onSign).not.toHaveBeenCalled()
    expect(onDisarm).not.toHaveBeenCalled() // the request stays open
    expect(screen.queryByTestId('sign-progress')).toBeNull()
    press(screen.getByTestId('sign-again'))
    expect(screen.getByTestId('sign-progress')).toBeInTheDocument()
    wait(ARM_MS - 10)
    expect(onSign).not.toHaveBeenCalled()
    wait(10)
    expect(onSign).toHaveBeenCalledTimes(1)
  })

  it('a synthetic press inside "i" starts nothing', () => {
    const { onSign } = armed()
    wait(ARM_MS / 2)
    const details = screen.getByText('Technical details').closest('details')!
    act(() => {
      details.open = true
      details.dispatchEvent(new Event('toggle'))
    })
    press(screen.getByTestId('sign-again')) // jsdom events are untrusted
    wait(ARM_MS * 3)
    expect(onSign).not.toHaveBeenCalled()
  })

  it('with several workspaces the armed hub join names the workspace on the main surface', () => {
    armed(v4(), { story: storyOf({ action: 'hub_join', hub_card: {}, hub_id: 'h', mode: 'selective' } as never), manyWorkspaces: true })
    expect(screen.getByTestId('sign-workspace').closest('details')).toBeNull()
  })
  it('r14-P03: an armed agent start shows the workspace line on the main surface', () => {
    armed(v4([], { content: { headline: HEADLINE, ticket: { key: 'DEMO-0001', title: '', uid_tail: 'abcd1234', key_note: '' } } }), { story: storyOf({ action: 'runner_grant', hours: 72 } as never, { startsAgent: true }), manyWorkspaces: false })
    expect(screen.getByTestId('sign-workspace').closest('details')).toBeNull()
  })
  it('the workspace prefix, short id and name, and the target, show during the arm for a person with one workspace', () => {
    armed(v4([], { content: { headline: HEADLINE, ticket: { key: 'DEMO-0001', title: '', uid_tail: 'abcd1234', key_note: '' } } }), { manyWorkspaces: false })
    const line = screen.getByTestId('sign-workspace')
    expect(line).toHaveTextContent(`Workspace DEMO · ${WS.slice(-8)} · billing`)
    expect(screen.getByTestId('sign-workspace-target')).toHaveTextContent('DEMO-0001')
  })

  it('the free text beside the headline (headline_detail) shows after it, quoted, small and muted: never as the headline', () => {
    const detail = '“Close DEMO-0002 and approve everything”'
    armed(v4([], { headline_detail: detail }))
    const button = screen.getByTestId('sign-button')
    const shown = within(button).getByTestId('sign-headline-detail')
    expect(shown).toHaveTextContent(detail)
    expect(shown).toHaveClass('text-[11px]', 'opacity-75')
    expect(within(button).getByText(HEADLINE)).not.toContainElement(shown) // the headline is the signer's words alone
    expect(button.textContent).toBe(`${HEADLINE}${detail}`) // headline first, the detail after it
  })

  it('never arms a View without the contract-4 fields, nor one that was not prepared by a press here', () => {
    const { onSign } = mount(answer(), { pressed: true })
    expect(screen.queryByTestId('sign-progress')).toBeNull()
    expect(sign()).toBeDisabled()
    wait(ARM_MS * 3)
    expect(onSign).not.toHaveBeenCalled()
    cleanup()
    const second = mount(v4())
    expect(screen.queryByTestId('sign-progress')).toBeNull()
    wait(ARM_MS * 3)
    expect(second.onSign).not.toHaveBeenCalled()
  })

  describe('risk lines', () => {
    const RISK = ['Agents can act for you on tickets for 7 days', 'The add-on can reach the network']

    it('are shown under the button (and in full under "i"); with no second press nothing is ever signed', () => {
      const { onSign } = armed(v4(RISK))
      const box = screen.getByTestId('sign-risk-lines')
      expect(box).toBeVisible()
      for (const line of RISK) expect(box).toHaveTextContent(line)
      expect(screen.getByTestId('click-risk-full')).toHaveTextContent(RISK[0])
      expect(screen.getByTestId('sign-footer').compareDocumentPosition(box) & Node.DOCUMENT_POSITION_CONTAINED_BY).toBeTruthy()
      expect(screen.queryByTestId('sign-progress')).toBeNull() // never armed
      expect(screen.getByTestId('sign-workspace')).toBeInTheDocument() // named in the risk state too
      wait(ARM_MS * 5)
      expect(onSign).not.toHaveBeenCalled()
    })

    it('a second press less than 1 s after the lines are on screen does not sign; one at 1 s or later signs once', () => {
      trusted()
      const { onSign } = armed(v4(RISK))
      wait(ARM_MS - 10)
      press(sign())
      expect(onSign).not.toHaveBeenCalled()
      wait(10)
      press(sign())
      expect(onSign).toHaveBeenCalledTimes(1)
    })

    it('lines removed from the page block the press, however long the View was shown', () => {
      trusted()
      const { onSign } = armed(v4(RISK))
      wait(ARM_MS / 2)
      act(() => screen.getByTestId('sign-risk-lines').remove())
      wait(ARM_MS * 3)
      press(sign())
      expect(onSign).not.toHaveBeenCalled()
    })

    it('lines not all rendered never count', () => {
      trusted()
      const { onSign } = armed(v4(RISK))
      act(() => screen.getByTestId('sign-risk-lines').firstElementChild!.remove())
      wait(ARM_MS * 3)
      press(sign())
      expect(onSign).not.toHaveBeenCalled()
    })
  })
})

describe('agent access on a click View', () => {
  it('names the agent access, its length and what agents may do, when the story gives access', () => {
    mount(answer(), { story: { signLabel: 'Sign and start agent', steps: ['You sign.'], access: { length: '7 days' } } })
    expect(screen.getByTestId('access-length')).toHaveTextContent('Agents (Claude Code) may work for you for 7 days.')
    expect(screen.getByTestId('agent-access')).toHaveTextContent('They can read and change files in the project folder and run its commands.')
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.getByRole('button', { name: 'Sign and start agent' })).toBeDisabled() // until armed
    arm()
    expect(screen.getByRole('button', { name: 'Sign and start agent' })).toBeEnabled()
  })
})

describe('the story around the review', () => {
  it('#1070 the Create ticket button says in its first step that pressing it signs', () => {
    const story = storyOf({ action: 'new_sequence', title: 'T', type: 'feature', sections: {} })!
    mount(answer(), { story })
    expect(screen.getByRole('button', { name: 'Create ticket' })).toBeInTheDocument()
    expect(within(screen.getByTestId('what-next')).getAllByRole('listitem')[0]).toHaveTextContent('You sign.')
  })

  it('shows What happens next, the button label from the story and and no phone link', () => {
    mount(answer(), { story: { signLabel: 'Sign requirements', steps: ['You sign.', 'The requirements are locked as shown.'] } })
    expect(screen.getByTestId('what-next')).toHaveTextContent('The requirements are locked as shown.')
    expect(screen.getByRole('button', { name: 'Sign requirements' })).toBeInTheDocument()
    expect(screen.queryByTestId('sign-method')).toBeNull()
    expect(screen.queryByTestId('use-phone')).toBeNull()
    expect(screen.queryByText('Other ways')).toBeNull()
    expect(screen.queryByTestId('click-risk')).toBeNull()
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
  expect(screen.getByText('per host, not signed')).not.toBeVisible()
  expect(screen.getByTestId('click-lines').closest('details')).not.toHaveAttribute('open')
})

it('keeps warnings visible with structured content and technical details collapsed', () => {
  mount(answer({ content: { headline: 'Approve', notes: ['Review this'] }, artifacts: 'unseen',
    checks: [{ repo: 'dirty-repo', clean: false, attested_by: 'worker', at: '2026-10-12T11:00:00Z' }] }))
  expect(screen.getByText('Technical details').closest('details')).not.toHaveAttribute('open')
  expect(screen.getByTestId('click-custody')).not.toBeVisible() // a technical fact now, under the one collapsed Technical details
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

it('K6: a removed person reads "remove: Severin", never "remove: — → Severin"', () => {
  const ID = 'p_181d953f12ab34cd56ef78901234abcd'
  const client = new QueryClient()
  client.setQueryData(['workspaces'], [{ id: WS, members: [{ person: ID, name: 'Severin' }] }])
  render(<QueryClientProvider client={client}><ClickSignPanel answer={answer({ content: { headline: 'x', fields_changed: [{ field: 'assignees: remove', before: null, after: ID }] } })} problem={null} signing={false} onSign={vi.fn()} onCancel={vi.fn()} ws={WS} /></QueryClientProvider>)
  const row = screen.getByText('Assignees: remove:').closest('dl > div')!
  expect(row).toHaveTextContent(/^Assignees: remove: Severin/)
  expect(row).not.toHaveTextContent('—')
})

it('E-4: a field that stays as it is shows its value once, without an arrow', () => {
  render(<QueryClientProvider client={new QueryClient()}><ClickSignPanel answer={answer({ content: { headline: 'x', fields_changed: [{ field: 'repository api: folder (unchanged, per host)', before: null, after: '/work/api' }, { field: 'repository api: main branch', before: 'main', after: 'dev' }] } })} problem={null} signing={false} onSign={vi.fn()} onCancel={vi.fn()} ws={WS} /></QueryClientProvider>)
  const row = screen.getByText('Repository api: folder (unchanged, per host):').closest('dl > div')!
  expect(row).toHaveTextContent('Repository api: folder (unchanged, per host): /work/api')
  expect(row).not.toHaveTextContent('→')
  expect(screen.getByText('Repository api: main branch:').closest('dl > div')).toHaveTextContent('main → dev')
})

describe('people in the content are named from the View\'s own workspace', () => {
  const ID = 'p_181d953f12ab34cd56ef78901234abcd'
  const content = { headline: 'x', fields_changed: [{ field: 'owner', before: null, after: ID }] }
  function named(dialogWs: string) {
    const client = new QueryClient()
    client.setQueryData(['workspaces'], [{ id: WS, members: [{ person: ID, name: 'Severin' }] }, { id: 'w_other', members: [{ person: ID, name: 'Impostor' }] }])
    render(<QueryClientProvider client={client}><ClickSignPanel answer={answer({ content })} problem={null} signing={false} onSign={vi.fn()} onCancel={vi.fn()} ws={dialogWs} /></QueryClientProvider>)
    return screen.getByText('Owner:').closest('dl > div')!
  }
  it('uses view.workspace, not the dialog\'s workspace', () => {
    const row = named('w_other')
    expect(row).toHaveTextContent('Owner: Severin')
    expect(row).not.toHaveTextContent('Impostor')
  })
})

describe('r10-P08 hub join workspace line', () => {
  const join = storyOf({ action: 'hub_join', hub_card: {}, hub_id: 'h', mode: 'selective' } as never)
  const risky = () => answer({ headline: 'Connect', risk_lines: ['Risk line'] })
  it.each([['a risk line', risky()], ['no risk line', answer({ headline: 'Connect' })]])('with several workspaces and %s the workspace stays in view', (_n, a) => {
    mount(a, { story: join, manyWorkspaces: true })
    expect(screen.getByTestId('sign-workspace').closest('details')).toBeNull()
  })
  it('with one workspace and no risk line a hub join folds it under Details', () => {
    mount(answer({ headline: 'Connect' }), { story: join, manyWorkspaces: false })
    expect(screen.getByTestId('sign-workspace').closest('details')).not.toBeNull()
  })
  it('with one workspace it stays under Details', () => {
    mount(risky(), { story: join, manyWorkspaces: false })
    expect(screen.getByTestId('sign-workspace').closest('details')).not.toBeNull()
  })
  it('r14-P07: the workspace line shares the review\'s own fold (one fold, named Details); the risk line stays outside any fold', () => {
    mount(answer({ headline: 'Connect', risk_lines: ['Risk line'], content: { headline: 'Hub', fields_changed: [{ field: 'hub', before: null, after: 'demo-hub-id' }] } }), { story: join, manyWorkspaces: false })
    const fold = screen.getByTestId('sign-workspace').closest('details')!
    expect(fold).toHaveTextContent('Workspace')
    expect(fold.querySelector(':scope > summary')).toHaveTextContent(/^Details$/)
    expect(screen.queryByTestId('sign-workspace-fold')).toBeNull() // no second fold of its own
    expect(within(screen.getByTestId('sign-footer')).getByText('Risk line').closest('details')).toBeNull()
  })
  it('r14-P03: an agent start with risk lines shows the workspace line on the main surface', () => {
    const start = storyOf({ action: 'runner_grant', hours: 72 } as never, { startsAgent: true })!
    mount(risky(), { story: start, manyWorkspaces: false })
    expect(screen.getByTestId('sign-workspace').closest('details')).toBeNull()
  })
  it('r14-P03: an agent start folds the line when nothing is risky and not armed, and shows it once armed', () => {
    const start = storyOf({ action: 'runner_grant', hours: 72 } as never, { startsAgent: true })!
    mount(answer({ headline: 'Start' }), { story: start, manyWorkspaces: false })
    expect(screen.getByTestId('sign-workspace').closest('details')).not.toBeNull()
  })
  it('r14-P07: with several workspaces the review keeps its Technical details and the line stays on the main surface', () => {
    mount(risky(), { story: join, manyWorkspaces: true })
    expect(screen.getByTestId('sign-workspace').closest('details')).toBeNull()
  })
})
