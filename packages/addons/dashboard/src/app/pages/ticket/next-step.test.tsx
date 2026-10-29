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

const run = (state: 'starting' | 'running' | 'exited' | 'lost') => ({ state, started_at: '2026-10-29T08:00:00Z' })
const noSections = async () => {
  const doc = await demo()
  doc.status = 'open'
  doc.claim = null
  doc.gates.requirements = { ...doc.gates.requirements, state: 'pending', approvals: [], missing: ['context', 'out_of_scope'] }
  return doc
}
const showWith = (doc: TicketDocument, o: { run?: ReturnType<typeof run> | null; wait?: string | null }) =>
  render(<TicketProgress ticket={structuredClone(doc)} viewer={viewer as never} sign={vi.fn()} jump={() => {}} run={o.run} wait={o.wait} />)

it('a ticket that never ran shows no banner, however empty its sections are', async () => {
  showWith(await noSections(), { run: null })
  expect(screen.queryByTestId('next-step')).toBeNull()
})

it('a run that is starting or running shows no banner', async () => {
  for (const state of ['starting', 'running'] as const) {
    const view = showWith(await noSections(), { run: run(state) })
    expect(screen.queryByTestId('next-step')).toBeNull()
    view.unmount()
  }
})

it('a run that ended with the plan unfinished: says what is missing and offers the two ways on', async () => {
  const onFinish = vi.fn()
  const onEdit = vi.fn()
  render(<TicketProgress ticket={await noSections()} viewer={viewer as never} sign={vi.fn()} jump={() => {}} run={run('exited')} onFinish={onFinish} onEdit={onEdit} />)
  const card = screen.getByTestId('next-step')
  expect(card).toHaveTextContent('The requirements are not finished yet')
  expect(card).toHaveTextContent('Still missing: a short background and what is not included.')
  expect(card).not.toHaveTextContent('stopped early')
  const buttons = within(card).getAllByRole('button')
  expect(buttons.map((b) => b.textContent)).toEqual(['Ask the agent to finish', 'Add it myself'])
  const user = userEvent.setup()
  await user.click(buttons[0])
  expect(onFinish).toHaveBeenCalledOnce()
  await user.click(buttons[1])
  expect(onEdit).toHaveBeenCalledOnce()
})

it('the card shows no button the viewer cannot use', async () => {
  render(<TicketProgress ticket={await noSections()} viewer={viewer as never} sign={vi.fn()} jump={() => {}} run={run('exited')} />)
  expect(within(screen.getByTestId('next-step')).queryAllByRole('button')).toHaveLength(0)
})

it('a terminal wait on an unfinished plan: the not-finished card, not Open terminal', async () => {
  const doc = await demo()
  doc.status = 'open'
  doc.claim = null
  doc.gates.requirements.state = 'approved'
  doc.gates.plan = { ...doc.gates.plan, state: 'pending', approvals: [], missing: ['context', 'acceptance'] }
  render(<TicketProgress ticket={doc} viewer={viewer as never} sign={vi.fn()} jump={() => {}} run={run('running')} wait="run_7" onFinish={vi.fn()} onEdit={vi.fn()} openTerminal={vi.fn()} />)
  const card = screen.getByTestId('next-step')
  expect(card).toHaveTextContent('The plan is not finished yet')
  expect(card).toHaveTextContent('Still missing: a short background and one check that proves it works')
  expect(screen.queryByRole('button', { name: 'Open terminal' })).toBeNull()
  expect(screen.queryByText(/Agent is working/)).toBeNull()
})

it('once the gap is filled the Plan ready card takes its place', async () => {
  const doc = await demo()
  doc.status = 'open'
  doc.claim = null
  doc.gates.requirements.state = 'approved'
  doc.gates.plan = { ...doc.gates.plan, state: 'pending', approvals: [], missing: ['context'] }
  const view = render(<TicketProgress ticket={doc} viewer={viewer as never} sign={vi.fn()} jump={() => {}} wait="run_7" onFinish={vi.fn()} />)
  expect(screen.getByTestId('next-step')).toHaveTextContent('The plan is not finished yet')
  view.rerender(<TicketProgress ticket={{ ...doc, gates: { ...doc.gates, plan: { ...doc.gates.plan, missing: [] } } }} viewer={viewer as never} sign={vi.fn()} jump={() => {}} wait="run_7" onFinish={vi.fn()} />)
  expect(screen.getByTestId('next-step')).toHaveTextContent('Plan ready')
  expect(screen.getByRole('button', { name: 'Approve plan' })).toBeInTheDocument()
})

it('a terminal wait for the ticket with nothing missing: the idle card with Open terminal', async () => {
  const onOpen = vi.fn()
  const doc = await demo()
  doc.status = 'open'
  doc.claim = null
  render(<TicketProgress ticket={doc} viewer={viewer as never} sign={vi.fn()} jump={() => {}} run={run('running')} wait="run_7" openTerminal={onOpen} />)
  const card = screen.getByTestId('next-step')
  expect(card).toHaveTextContent('The agent is waiting for you')
  expect(card).toHaveTextContent('The agent is idle. Open it to see why.')
  await userEvent.setup().click(within(card).getByRole('button', { name: 'Open terminal' }))
  expect(onOpen).toHaveBeenCalledWith('run_7')
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

// run-9 J04: after "Approve plan" the agent's turn had ended; the ticket showed "Agent is working on it" for minutes.
const approvedIdle = async () => {
  const doc = await demo()
  doc.status = 'in_progress' as never
  doc.claim = { agent: 'claude-code', session: 's_1', for: 'p_sev', since: '2026-10-29T08:00:00Z', live: true } as never
  doc.gates.requirements = { ...doc.gates.requirements, state: 'approved', missing: [] }
  doc.gates.plan = { ...doc.gates.plan, state: 'approved', missing: [] }
  doc.tasks_state = doc.tasks_state.map((t) => ({ ...t, state: 'todo' as const, lease: null, receipt: null, done_at: null }))
  return doc
}

it('an idle agent after the plan approval: no spinner, one Start building button', async () => {
  const onFinish = vi.fn()
  render(<TicketProgress ticket={await approvedIdle()} viewer={viewer as never} sign={vi.fn()} jump={() => {}} run={run('running')} wait="r_1" onFinish={onFinish} />)
  const card = screen.getByTestId('next-step')
  expect(card).toHaveTextContent('Plan approved')
  expect(card).not.toHaveTextContent('working')
  expect(card).not.toHaveTextContent('stopped') // the idle source may be a quiet terminal: "may be waiting"
  expect(card).toHaveTextContent('may be waiting')
  expect(within(card).getAllByRole('button')).toHaveLength(1)
  await userEvent.setup().click(within(card).getByRole('button', { name: 'Start building' }))
  expect(onFinish).toHaveBeenCalledOnce()
})

it('the same ticket while the agent prints (no wait): the working note, no button', async () => {
  render(<TicketProgress ticket={await approvedIdle()} viewer={viewer as never} sign={vi.fn()} jump={() => {}} run={run('running')} wait={null} onFinish={vi.fn()} />)
  expect(screen.getByTestId('next-step')).toHaveTextContent('Agent is working on it')
  expect(screen.queryByRole('button', { name: 'Start building' })).toBeNull()
})

it('agentWorking: a live claim is working only while the agent is not idle', async () => {
  const { agentWorking } = await import('./progress')
  const t = await approvedIdle()
  expect(agentWorking(t)).toBe(true)
  expect(agentWorking(t, 'r_1')).toBe(false)
  expect(agentWorking({ claim: null })).toBe(false)
})
