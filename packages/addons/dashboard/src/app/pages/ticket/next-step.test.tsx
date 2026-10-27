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

// The approval evidence (ported from the old gate popover) sits behind a quiet "i" on the card.
const viewer2 = { ...viewer, members: [...viewer.members, { person: 'p_mara', name: 'Mara', role: 'maintainer' as const }], name: (id: string | null | undefined) => (id === 'p_sev' ? 'Severin' : id === 'p_mara' ? 'Mara' : String(id)) }

async function planUp(gate: Partial<TicketDocument['gates']['plan']>) {
  const doc = await demo()
  doc.status = 'open'
  doc.claim = null
  doc.gates.requirements.state = 'approved'
  doc.gates.plan = { ...doc.gates.plan, state: 'pending', approvals: [], missing: [], needed: 2, ...gate }
  return doc
}

async function openDetails(doc: TicketDocument) {
  render(<TicketProgress ticket={structuredClone(doc)} viewer={viewer2 as never} sign={vi.fn()} jump={() => {}} />)
  const card = screen.getByTestId('next-step')
  expect(card).toHaveTextContent('Plan ready')
  await userEvent.setup().click(within(card).getByRole('button', { name: 'Details' }))
  return screen.findByRole('dialog', { name: /Plan/ })
}

it('the Details popover: policy, how many of the needed approvals, who signed when, what it covers, the hash folded', async () => {
  const doc = await planUp({ approvals: [{ by: 'p_mara', at: '2026-10-08T09:44:00Z', via: 'dashboard', presence: 'convenience', sig_ok: true }], covers: ['T1', 'T2'], hash: 'sha256:abc123' })
  const pop = await openDetails(doc)
  expect(pop).toHaveTextContent(/Plan needs/)
  expect(pop).toHaveTextContent('1 of 2 approvals')
  expect(within(pop).getByText(/Approved by Mara/)).toHaveTextContent('verified signature')
  expect(pop).toHaveTextContent('Covers T1 · T2')
  expect(within(pop).getByText('Details').closest('details')).toHaveTextContent('sha256:abc123')
  expect(screen.queryByRole('alert')).toBeNull()
})

it('says "unknown" for an approval without channel, presence or signature check, never a guessed default', async () => {
  const pop = await openDetails(await planUp({ approvals: [{ by: 'p_mara', at: '2026-10-08T09:44:00Z' }] }))
  expect(within(pop).getByText(/signature not checked/)).toBeInTheDocument()
  expect(within(pop).getByText(/via unknown · presence unknown/)).toBeInTheDocument()
  expect(within(pop).queryByText(/Touch ID|verified signature/)).toBeNull()
})

it('shows a convenience signature as "convenience (no presence)", never as a password; an unknown kind keeps its name', async () => {
  const pop = await openDetails(
    await planUp({
      approvals: [
        { by: 'p_mara', at: '2026-10-08T09:44:00Z', via: 'dashboard', presence: 'convenience' },
        { by: 'p_mara', at: '2026-10-08T09:45:00Z', via: 'cli', presence: 'hardware-key' },
      ],
      needed: 3,
    }),
  )
  expect(within(pop).getByText(/presence convenience \(no presence\)/)).toBeInTheDocument()
  expect(within(pop).getByText(/presence hardware-key/)).toBeInTheDocument()
  expect(within(pop).queryByText(/presence password/)).toBeNull()
})

it.each([{ via: 'phone' as const, sig_ok: true }, { presence: 'secure-enclave-unlocked' as const, sig_ok: false }])('shows phone decisions without authentication or strength labels: %j', async (metadata) => {
  const pop = await openDetails(await planUp({ approvals: [{ by: 'p_mara', at: '2026-10-08T09:44:00Z', ...metadata }] }))
  // The count line names "approvals"; the approval itself carries no auth labels.
  expect(pop.textContent).not.toMatch(/verified|presence|secure-enclave|signature|signed on phone/i)
  expect(within(pop).getByText(/Approved by Mara/)).toHaveTextContent('as reported by your computer')
  // A phone decision is never a failed signature on the card.
  expect(screen.queryByRole('alert')).toBeNull()
})

it('voided approvals of an invalidated gate stay visible, struck through', async () => {
  const pop = await openDetails(await planUp({ state: 'invalidated', approvals: [], voided: [{ by: 'p_mara', at: '2026-10-08T09:44:00Z', sig_ok: true }] }))
  expect(pop).toHaveTextContent('0 of 2 approvals')
  expect(within(pop).getByText(/Approved by Mara/)).toHaveClass('line-through')
})

it('a failed signature is said on the card itself, without opening Details', async () => {
  const doc = await planUp({ approvals: [{ by: 'p_mara', at: '2026-10-08T09:44:00Z', via: 'cli', sig_ok: false }] })
  show(doc)
  expect(within(screen.getByTestId('next-step')).getByRole('alert')).toHaveTextContent('A signature on this step failed to verify.')
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('the verdict card carries the Details too', async () => {
  const doc = await demo()
  doc.status = 'testing'
  doc.verdict = null as never
  doc.claim = null
  doc.gates.verify = { ...doc.gates.verify, state: 'pending', approvals: [], eligible: ['p_sev'] }
  render(<TicketProgress ticket={doc} viewer={viewer2 as never} sign={vi.fn()} jump={() => {}} />)
  const card = screen.getByTestId('next-step')
  expect(card).toHaveTextContent('Work is ready to check')
  await userEvent.setup().click(within(card).getByRole('button', { name: 'Details' }))
  expect(await screen.findByRole('dialog', { name: /Verify|Verdict/ })).toHaveTextContent(/0 of \d+ approvals/)
})

it('while a signature is being sent, every action on the card is disabled', async () => {
  const doc = await planUp({})
  const sign = vi.fn()
  render(<TicketProgress ticket={doc} viewer={viewer as never} sign={sign} jump={() => {}} signing />)
  const card = screen.getByTestId('next-step')
  const approve = within(card).getByRole('button', { name: 'Approve plan' })
  const change = within(card).getByRole('button', { name: 'Change something' })
  expect(approve).toBeDisabled()
  expect(change).toBeDisabled()
  const user = userEvent.setup()
  await user.click(approve)
  await user.click(change)
  expect(sign).not.toHaveBeenCalled()
})

it('while a signature is being sent, the verdict and answer buttons are disabled too', async () => {
  const doc = await demo()
  doc.status = 'testing'
  doc.verdict = null as never
  doc.claim = null
  doc.gates.verify = { ...doc.gates.verify, state: 'pending', approvals: [], eligible: ['p_sev'] }
  const view = render(<TicketProgress ticket={doc} viewer={viewer as never} sign={vi.fn()} jump={() => {}} signing />)
  expect(screen.getByRole('button', { name: 'Give verdict' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Send back' })).toBeDisabled()
  view.unmount()
  const withQ = await demo()
  withQ.status = 'open'
  withQ.gates.requirements.state = 'approved'
  withQ.gates.plan.state = 'approved'
  withQ.questions_state = [{ id: 'q1', state: 'open', to: 'p_sev' } as never]
  render(<TicketProgress ticket={withQ} viewer={viewer as never} sign={vi.fn()} jump={() => {}} signing />)
  expect(screen.getByTestId('next-step')).toHaveTextContent('A question needs you')
  expect(screen.getByRole('button', { name: 'Answer' })).toBeDisabled()
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
