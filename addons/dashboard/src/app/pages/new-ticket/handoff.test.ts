import { describe, expect, it } from 'vitest'
import { EMPTY, handoffNewTicket } from './index'

describe('a new ticket through the hand-off', () => {
  it('is one intent: title, type, fields and written text', () => {
    const made = handoffNewTicket({ ...EMPTY, title: ' Fix it ', type: 'bug', priority: 'high', labels: ['api'], sections: { requirements: 'It must work' } })
    expect(made).toEqual({ action: { action: 'new_sequence', title: 'Fix it', type: 'bug', priority: 'high', labels: ['api'], sections: { requirements: 'It must work' } } })
    expect(handoffNewTicket({ ...EMPTY, title: 'Plain' })).toEqual({ action: { action: 'new_sequence', title: 'Plain', type: 'feature', sections: {} } })
  })
  it('says what a signed new ticket does not carry yet instead of dropping it', () => {
    for (const patch of [{ visibility: { restricted: ['p_x'] } }, { due: '2026-11-01' }]) {
      expect(handoffNewTicket({ ...EMPTY, title: 'T', ...patch })).toHaveProperty('problem')
    }
  })
  it('carries acceptance criteria and people in the same intent (N3)', () => {
    const me = 'p_' + 'a'.repeat(32)
    const quinn = 'p_' + 'b'.repeat(32)
    const sam = 'p_' + 'c'.repeat(32)
    const made = handoffNewTicket(
      { ...EMPTY, title: 'T', acceptance: [' It opens ', '', 'It saves'], people: { owner: quinn, assignees: [sam, quinn, sam], reviewers: [me] } },
      me,
    )
    expect(made).toEqual({
      action: { action: 'new_sequence', title: 'T', type: 'feature', sections: {}, acceptance: ['It opens', 'It saves'], people: { owner: quinn, assignees: [quinn, sam], reviewers: [me] } },
    })
  })
  it('does not sign the creator as the owner again, and sends no people when none are chosen', () => {
    const me = 'p_' + 'a'.repeat(32)
    expect(handoffNewTicket({ ...EMPTY, title: 'T', people: { owner: me, assignees: [], reviewers: [] } }, me)).toEqual({
      action: { action: 'new_sequence', title: 'T', type: 'feature', sections: {} },
    })
  })
  it('says so when one signing run cannot carry that many changes', () => {
    const many = { ...EMPTY, title: 'T', acceptance: Array.from({ length: 8 }, (_, i) => `c${i}`), sections: { summary: 'a', context: 'b', requirements: 'c', out_of_scope: 'd', plan: 'e', decisions: 'f', verification: 'g', current_state: 'h' } }
    expect(handoffNewTicket(many as never)).toHaveProperty('problem')
    expect(handoffNewTicket({ ...EMPTY, title: 'T', acceptance: Array.from({ length: 9 }, (_, i) => `c${i}`) })).toHaveProperty('problem')
  })
})
