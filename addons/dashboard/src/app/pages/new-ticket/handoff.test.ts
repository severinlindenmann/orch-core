import { describe, expect, it } from 'vitest'
import { EMPTY, handoffNewTicket } from './index'

describe('a new ticket through the hand-off', () => {
  it('signs the title and type; priority, size and labels follow as details', () => {
    const made = handoffNewTicket({ ...EMPTY, title: ' Fix it ', type: 'bug', priority: 'high', labels: ['api'] })
    expect(made).toEqual({ action: { action: 'new', title: 'Fix it', type: 'bug' }, details: [{ action: 'details', key: '', priority: 'high', labels: ['api'] }] })
    expect(handoffNewTicket({ ...EMPTY, title: 'Plain' })).toEqual({ action: { action: 'new', title: 'Plain', type: 'feature' }, details: [] })
  })
  it('says what a signed new ticket does not carry yet instead of dropping it', () => {
    for (const patch of [{ visibility: { restricted: ['p_x'] } }, { due: '2026-11-01' }, { acceptance: ['x'] }, { people: { owner: 'p_x', assignees: [], reviewers: [] } }]) {
      expect(handoffNewTicket({ ...EMPTY, title: 'T', ...patch })).toHaveProperty('problem')
    }
  })
})
