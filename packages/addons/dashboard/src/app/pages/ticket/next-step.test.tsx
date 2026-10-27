import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import type { TicketDocument } from '@/api/types'
import { TicketProgress } from './NextStep'

afterEach(cleanup)

// B6: one plain stepper and one next-step card instead of the Requirements/Plan/Verify bar (owner feedback 5/14).
const viewer = { person: 'p_sev', role: 'owner' as const, members: [{ person: 'p_sev', name: 'Severin', role: 'owner' as const }], name: (id: string | null | undefined) => (id === 'p_sev' ? 'Severin' : String(id)), ready: true }

async function demo() {
  const doc = structuredClone((await import('@/api/client')).mockStore.ticket('DEMO-0043')!)
  doc.questions_state = []
  return doc
}

const show = (doc: TicketDocument, sign = vi.fn()) => render(<TicketProgress ticket={structuredClone(doc)} viewer={viewer as never} sign={sign} jump={() => {}} />)
const states = () => ['understand', 'build', 'check', 'done'].map((s) => screen.getByTestId(`step-${s}`).getAttribute('data-state'))

it('a plan the viewer may approve: one card with Approve plan and Change something', async () => {
  const doc = await demo()
  doc.status = 'open'
  doc.claim = null
  doc.gates.requirements.state = 'approved'
  doc.gates.plan = { ...doc.gates.plan, state: 'pending', approvals: [], missing: [] }
  const sign = vi.fn()
  show(doc, sign)
  const card = screen.getByTestId('next-step')
  expect(card).toHaveTextContent('Plan ready')
  const user = userEvent.setup()
  await user.click(within(card).getByRole('button', { name: 'Approve plan' }))
  expect(sign).toHaveBeenLastCalledWith({ kind: 'approve', gate: 'plan' })
  await user.click(within(card).getByRole('button', { name: 'Change something' }))
  expect(sign).toHaveBeenLastCalledWith({ kind: 'request_changes', gate: 'plan' })
  expect(screen.getByTestId('step-understand')).toHaveAttribute('data-state', 'current')
})

it('the agent stopped early: says what to add first and to start the agent again, with no button', async () => {
  const doc = await demo()
  doc.status = 'open'
  doc.claim = null
  doc.gates.requirements = { ...doc.gates.requirements, state: 'pending', approvals: [], missing: ['context', 'out_of_scope'] }
  show(doc)
  const card = screen.getByTestId('next-step')
  expect(card).toHaveTextContent('The agent stopped early')
  expect(card).toHaveTextContent('Add Context and Out of scope first. Start the agent again to finish it.')
  expect(within(card).queryAllByRole('button')).toHaveLength(0)
})

it('the stepper: done steps, the first open one is current, the rest still to do', async () => {
  const doc = await demo()
  doc.gates.requirements.state = 'approved'
  doc.gates.plan.state = 'approved'
  doc.gates.verify = { ...doc.gates.verify, state: 'pending', approvals: [] }
  doc.status = 'in-progress'
  let view = show(doc)
  expect(within(screen.getByTestId('stepper')).queryAllByRole('button')).toHaveLength(0)
  expect(states()).toEqual(['done', 'current', 'todo', 'todo'])
  view.unmount()
  doc.status = 'testing'
  view = show(doc)
  expect(states()).toEqual(['done', 'done', 'current', 'todo'])
  view.unmount()
  doc.status = 'done'
  show(doc)
  expect(states()).toEqual(['done', 'done', 'done', 'done'])
  expect(screen.queryByTestId('next-step')).toBeNull()
})

it('while the agent works: one calm line, no buttons', async () => {
  const doc = await demo()
  expect(doc.claim).toBeTruthy()
  doc.status = 'in-progress'
  doc.gates.requirements.state = 'approved'
  doc.gates.plan.state = 'approved'
  show(doc)
  const line = screen.getByTestId('next-step')
  expect(line).toHaveTextContent('Agent is working on it')
  expect(within(line).queryAllByRole('button')).toHaveLength(0)
  expect(line).not.toHaveTextContent('The agent stopped early')
})
