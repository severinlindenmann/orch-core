import { describe, expect, it } from 'vitest'
import { EMPTY, handoffNewTicket } from './index'

describe('a new ticket through the hand-off', () => {
  it('is one intent: title, type, fields and written text', () => {
    const made = handoffNewTicket({ ...EMPTY, title: ' Fix it ', type: 'bug', priority: 'high', labels: ['api'], sections: { requirements: 'It must work' } })
    expect(made).toEqual({ action: { action: 'new_sequence', title: 'Fix it', type: 'bug', priority: 'high', labels: ['api'], sections: { requirements: 'It must work' } } })
    expect(handoffNewTicket({ ...EMPTY, title: 'Plain' })).toEqual({ action: { action: 'new_sequence', title: 'Plain', type: 'feature', sections: {} } })
  })
  it('says what a signed new ticket does not carry yet instead of dropping it', () => {
    for (const patch of [{ visibility: { restricted: ['p_x'] } }, { due: '2026-11-01' }, { acceptance: ['x'] }, { people: { owner: 'p_x', assignees: [], reviewers: [] } }]) {
      expect(handoffNewTicket({ ...EMPTY, title: 'T', ...patch })).toHaveProperty('problem')
    }
  })
})
