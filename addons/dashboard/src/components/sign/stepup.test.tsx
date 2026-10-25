import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { CLICK_RISK, type ClickViewAnswer } from '@/api/click'
import { setFeatures } from '@/api/features'
import { noteStepUpRefused, resetStepUp } from '@/api/passkey'
import { ClickSignPanel } from './ClickSignPanel'
import { StepUpNote } from './StepUp'

const answer = (over: Partial<ClickViewAnswer['view']> = {}): ClickViewAnswer => ({
  view_digest: 'sha256:' + 'a'.repeat(64),
  view: {
    risk: CLICK_RISK, op: 'access', workspace: { id: 'w'.repeat(32), name: 'billing', prefix: 'DEMO' }, fingerprint: 'abcdef012345',
    lines: ['Give agents access for 8 hours'], checks: [], confirm: { kind: 'click' }, expires_at: '2026-10-12T12:10:00Z',
    auth: 'convenience', device: 'd_x', artifacts: null, seal: { v: 1, alg: 'x', purposes: ['grant.secret'] }, ...over,
  },
})
const panel = (a = answer()) => render(<ClickSignPanel answer={a} problem={null} signing={false} onSign={() => {}} onCancel={() => {}} />)

beforeEach(() => resetStepUp())
afterEach(() => { setFeatures(null); resetStepUp() })

describe('step-up in the sign panel (remote session)', () => {
  it('the confirm button becomes the touch, and the whole review stays on screen', () => {
    setFeatures({ features: [], login: 'remote' })
    panel()
    expect(screen.getByRole('button', { name: /Touch your passkey to confirm/ })).toBeInTheDocument()
    expect(screen.getByTestId('click-panel')).toHaveTextContent('Give agents access for 8 hours')
    expect(screen.getByTestId('sign-method')).toHaveTextContent('Check the details above.')
  })
  it('an expired check says nothing was changed and keeps the review', () => {
    setFeatures({ features: [], login: 'remote' })
    panel()
    act(() => { noteStepUpRefused({ code: 'passkey.stepup_required' }) })
    expect(screen.getByTestId('stepup-note')).toHaveAttribute('data-phase', 'expired')
    expect(screen.getByTestId('stepup-note')).toHaveTextContent('Your passkey check ran out (10 minutes)')
    expect(screen.getByTestId('click-panel')).toHaveTextContent('Give agents access for 8 hours')
  })
  it('on this computer the button is unchanged', () => {
    setFeatures({ features: [], login: 'local' })
    panel()
    expect(screen.queryByText(/Touch your passkey/)).toBeNull()
    expect(screen.getByTestId('sign-method')).toHaveTextContent('On this computer')
  })
  it('a note renders nothing when there is nothing to say', () => {
    const { container } = render(<StepUpNote />)
    expect(container).toBeEmptyDOMElement()
  })
})
