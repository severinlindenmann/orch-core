import { describe, expect, it } from 'vitest'
import { EMPTY, handoffNewTicket } from './index'

describe('a new ticket through the hand-off', () => {
  it('sends what differs from the defaults, and the first written section as the description', () => {
    const made = handoffNewTicket({ ...EMPTY, title: ' Fix it ', type: 'bug', priority: 'high', labels: ['api'], sections: { context: ' Why ' } })
    expect(made).toEqual({ action: { action: 'new', title: 'Fix it', type: 'bug', priority: 'high', labels: ['api'], message: 'Why' } })
    expect(handoffNewTicket({ ...EMPTY, title: 'Plain' })).toEqual({ action: { action: 'new', title: 'Plain', type: 'feature' } })
  })
  it('says what a signed new ticket does not carry yet instead of dropping it', () => {
    for (const patch of [{ visibility: { restricted: ['p_x'] } }, { due: '2026-11-01' }, { acceptance: ['x'] }, { people: { owner: 'p_x', assignees: [], reviewers: [] } }]) {
      expect(handoffNewTicket({ ...EMPTY, title: 'T', ...patch })).toHaveProperty('problem')
    }
  })
})
