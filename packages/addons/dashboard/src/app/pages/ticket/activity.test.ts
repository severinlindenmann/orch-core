import { expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import type { TicketDocument } from '@/api/types'
import { activityLine } from './activity'
import { stepStates } from './NextStep'

const NOW = Date.parse('2026-10-29T12:00:00Z')
const base = (): TicketDocument => {
  const d = structuredClone(mockStore.ticket('DEMO-0043')!)
  d.questions_state = []
  d.tasks_state = []
  d.status = 'in_progress' as never
  return d
}
const line = (d: TicketDocument, run?: { state: 'running'; started_at: string }) => activityLine(d, stepStates(d), run, NOW)

it('writing the plan while the requirements are approved and the plan is not', () => {
  const d = base()
  d.gates.requirements.state = 'approved'
  d.gates.plan.state = 'pending'
  expect(line(d)).toBe('Writing the plan')
})
it('building names the task with the lease and counts up', () => {
  const d = base()
  d.gates.requirements.state = 'approved'
  d.gates.plan.state = 'approved'
  d.tasks_state = [{ id: 'T1', text: 'Add hello.txt', verify: null, proves: [], state: 'doing', lease: { session: 's', agent: 'a', since: '2026-10-29T11:58:00Z' }, receipt: null, done_at: null }]
  expect(line(d)).toBe('Building: Add hello.txt · 2 min')
})
it('building without a lease falls back to the run time', () => {
  const d = base()
  d.gates.requirements.state = 'approved'
  d.gates.plan.state = 'approved'
  expect(line(d, { state: 'running', started_at: '2026-10-29T11:55:00Z' })).toBe('Working on it · 5 min')
})
it('checking the result in the check step', () => {
  const d = base()
  d.gates.requirements.state = 'approved'
  d.gates.plan.state = 'approved'
  d.status = 'testing' as never
  expect(line(d)).toBe('Checking the result')
})
it('an open question wins: waiting for your answer', () => {
  const d = base()
  d.questions_state = [{ id: 'Q1', state: 'open' } as never]
  expect(line(d)).toBe('Waiting for your answer')
})
