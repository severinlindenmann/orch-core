import { describe, expect, it } from 'vitest'
import { computeTurn } from './derive'

const people = { owner: 'p_sev', assignees: [], reviewers: [], watchers: [] }
describe('turn line for an open ticket', () => {
  it('names the plan approval only when there is a plan (tasks) to approve, as the blocked button does', () => {
    expect(computeTurn('open', people, null, undefined, null, { plan: 'pending', requirements: 'approved', planApprovers: 'owner', taskCount: 2 }).why).toBe('Plan needs approval (owners)')
    expect(computeTurn('open', people, null, undefined, null, { plan: 'pending', requirements: 'approved', planApprovers: 'owner', taskCount: 0 }).why).toBe('Ready to claim')
    expect(computeTurn('open', people, null, undefined, null, { plan: 'approved', requirements: 'approved', planApprovers: 'owner', taskCount: 2 }).why).toBe('Ready to claim')
  })
})
