// C3, click contract 4: the View's headline and risk lines, and the refusal of another contract.
import { describe, expect, it } from 'vitest'
import { CLICK_CONTRACT, CLICK_RISK, clickProblem, contractViewOk, riskLinesOf, type ClickView } from './click'
import { ApiError } from './types'

const view = (over: Partial<ClickView> = {}): ClickView => ({
  headline: 'Close DEMO-0001',
  risk_lines: [],
  risk: CLICK_RISK,
  op: 'close',
  workspace: { id: 'a'.repeat(32), name: 'billing', prefix: 'DEMO' },
  fingerprint: '0123456789ab',
  lines: ['Close DEMO-0001'],
  checks: [],
  confirm: { kind: 'click' },
  expires_at: '2099-01-01T00:00:00Z',
  auth: 'convenience',
  device: 'd_x',
  artifacts: null,
  ...over,
})

describe('click contract 4', () => {
  it('is the contract this page speaks', () => {
    expect(CLICK_CONTRACT).toBe(4)
  })

  it('a View is of contract 4 only with a headline and a list of risk lines (empty or not)', () => {
    expect(contractViewOk(view())).toBe(true)
    expect(contractViewOk(view({ risk_lines: ['Agents can act for you for 7 days'] }))).toBe(true)
    expect(contractViewOk(view({ headline: undefined }))).toBe(false)
    expect(contractViewOk(view({ headline: '  ' }))).toBe(false)
    expect(contractViewOk(view({ risk_lines: undefined }))).toBe(false)
    expect(contractViewOk(view({ risk_lines: 'x' as never }))).toBe(false)
    expect(contractViewOk(view({ risk_lines: [''] }))).toBe(false)
    expect(contractViewOk(view({ risk_lines: [1] as never }))).toBe(false)
    expect(contractViewOk(null)).toBe(false)
  })

  it('the risk lines shown are the signer’s, never invented', () => {
    expect(riskLinesOf(view({ risk_lines: ['a', 'b'] }))).toEqual(['a', 'b'])
    expect(riskLinesOf(view({ risk_lines: undefined }))).toEqual([])
  })

  it('Already done for a new ticket names it and opens it, with no Try again', () => {
    const p = clickProblem(new ApiError(409, { code: 'intent.done', message: 'x', retryable: false, summary: '[{"raw":1}]', ticket: { key: 'DEMO-0007', title: 'Plan the launch' } }))
    expect(p.next).toBe('open')
    expect(p.ticket).toEqual({ key: 'DEMO-0007', title: 'Plan the launch' })
    expect(p.title).toBe('You already have this ticket')
    expect(p.body).toBe('“Plan the launch” (DEMO-0007) already exists with this text. Open it, or make another one like it.')
    expect(p.body).not.toContain('raw')
  })

  it('a contract.mismatch refusal is one plain line, with nothing to retry', () => {
    for (const e of [
      new ApiError(409, { code: 'contract.mismatch', message: 'client 4, signer 3', retryable: false }),
      new ApiError(409, { code: 'human.click_refused', message: 'x', retryable: false, reason: 'contract.mismatch' }),
    ]) {
      const p = clickProblem(e)
      expect(p.next).toBe('unavailable')
      expect(p.body).toBe('This page and the signing helper are different versions. Nothing was signed. Reload the page; if it stays, update orch-core.')
      expect(p.body).not.toContain('signer 3')
    }
  })
})

describe('P02: a code is never the sentence', () => {
  const PLAIN = 'That could not be used. Check it and try again.'
  it.each([
    ['invalid.input', 'invalid.input'],
    ['invalid.input', 'That could not be used. Check it and try again.'],
    ['some.new_code', 'some.new_code'], // a retryable message equal to its own code
  ])('%s with message %j says one plain sentence and offers Try again', (code, message) => {
    const p = clickProblem(new ApiError(400, { code, message, retryable: code === 'some.new_code' }))
    expect(p.body).toBe(PLAIN)
    expect(p.next).toBe('retry')
    expect(`${p.title} ${p.body}`).not.toContain(code)
  })
  it.each(['session.readonly', 'switch.unverified', 'passkey.invalid', 'enroll.expired', 'internal'])(
    'a non-retryable %s with only its code says a plain sentence and never offers Try again',
    (code) => {
      const p = clickProblem(new ApiError(400, { code, message: code, retryable: false }))
      expect(p.next).toBe('unavailable')
      expect(p.body).toBe('This could not be done here. Nothing was signed.')
      expect(`${p.title} ${p.body}`).not.toContain(code)
    },
  )
  it('an invalid.input with real words from the host says those words, never the code', () => {
    const p = clickProblem(new ApiError(400, { code: 'invalid.input', message: 'a feature ticket has no section nope', retryable: false }))
    expect(p.body).toBe('a feature ticket has no section nope')
    expect(p.next).toBe('unavailable')
  })
  it('keeps a real message of an unknown code', () => {
    expect(clickProblem(new ApiError(400, { code: 'some.new_code', message: 'The folder is gone.', retryable: false })).body).toBe('The folder is gone.')
  })
})
