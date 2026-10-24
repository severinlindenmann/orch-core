import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import type { TicketDocument, AcceptanceStatus } from '@/api/types'
import { ManualDialog, ManualAcceptanceReview, mayAccept } from './ManualAcceptance'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { queries } from '@/api/queries'
import { api } from '@/api/client'
import { acState, ReceiptLine, AcceptanceTasks } from './AcceptanceTasks'
import type { Viewer } from './shared'
import { clickEligible } from '@/api/click'
import { eventDetail } from './History'

vi.mock('@/components/sign/SignPopover', () => ({ SignPopover: (p: { action: unknown; hint?: string; children: ReactNode }) => <div><pre data-testid="action">{JSON.stringify(p.action)}</pre><p>{p.hint}</p>{p.children}</div> }))
vi.mock('@/api/features', async orig => ({ ...(await orig<typeof import('@/api/features')>()), handoffMode: () => true }))
afterEach(() => { cleanup(); vi.restoreAllMocks() })
const ac: AcceptanceStatus = { id: 'AC2', text: 'The exact criterion', state: 'unproven', evidence: [], proof: 'accepted', accepted: { by: 'p_a', at: '2026-10-19T12:02:00Z' } }
const ticket = { key: 'DEMO-0001', status: 'open', head: { seq: 1 }, people: { owner: 'p_a', reviewers: ['p_b'] }, acceptance_state: [ac], gates: { verify: { state: 'pending' } } } as TicketDocument
const viewer = { person: 'p_a', role: 'member', name: () => 'Severin', members: [], ready: true } as Viewer
const sent = () => JSON.parse(screen.getByTestId('action').textContent!)

describe('manual authority', () => {
  it('allows only an owner, reviewer or workspace manager, with editing and handoff available', () => {
    for (const person of ['p_a', 'p_b']) expect(mayAccept(ticket, { ...viewer, person }, true)).toBe(true)
    expect(mayAccept(ticket, { ...viewer, person: 'p_c' }, true)).toBe(false)
    for (const role of ['owner', 'maintainer'] as const) expect(mayAccept(ticket, { ...viewer, person: 'p_c', role }, true)).toBe(true)
    expect(mayAccept(ticket, { ...viewer, role: 'viewer' }, true)).toBe(false)
    expect(mayAccept(ticket, viewer, false)).toBe(false)
    expect(mayAccept({ ...ticket, status: 'done' }, viewer, true)).toBe(false)
    expect(mayAccept({ ...ticket, head: { seq: 0, hash: '' } }, viewer, true)).toBe(false)
  })
  it('shows exact wording and sends a trimmed optional note', () => {
    render(<ManualDialog ticket={ticket} choice={{ kind: 'accept', ac }} onClose={() => {}} />)
    expect(screen.getByText(ac.text)).toBeVisible()
    expect(screen.getByText('No automated check backs this: you vouch for it yourself.')).toBeVisible()
    expect(sent()).toEqual({ action: 'accept_criterion', key: ticket.key, ac: 'AC2' })
    fireEvent.change(screen.getByLabelText('Note (optional)'), { target: { value: ' Checked by hand ' } })
    expect(sent().note).toBe('Checked by hand')
    expect(clickEligible(sent())).toBe(true)
    expect(screen.getByRole('textbox')).toHaveAttribute('maxlength', '1000')
  })
  it('requires a reason to withdraw', () => {
    render(<ManualDialog ticket={ticket} choice={{ kind: 'withdraw', ac }} onClose={() => {}} />)
    expect(screen.getByText('Give a reason to continue')).toBeVisible()
    fireEvent.change(screen.getByLabelText('Reason (required)'), { target: { value: ' Wording changed ' } })
    expect(sent()).toEqual({ action: 'withdraw_acceptance', key: ticket.key, ac: 'AC2', reason: 'Wording changed' })
    expect(screen.queryByText('Give a reason to continue')).not.toBeInTheDocument()
  })
  it('explains task completion and ending an active lease without claiming evidence', () => {
    const task = { id: 'T2', text: 'Check it', state: 'doing', receipt: null, lease: { session: 's', agent: 'a', since: '' } } as TicketDocument['tasks_state'][number]
    render(<ManualDialog ticket={ticket} choice={{ kind: 'done', task }} onClose={() => {}} />)
    expect(screen.getByText('No receipt is recorded, so this proves no criterion. Accepting a criterion is a separate step.')).toBeVisible()
    expect(screen.getByText('An agent is working on it; marking it done ends its lease.')).toBeVisible()
    expect(sent()).toEqual({ action: 'mark_task_done', key: ticket.key, task: 'T2' })
  })
  it('uses host evidence state, never infers it from manual acceptance', () => {
    expect(acState(ticket, ac)).toBe('open')
    expect(acState(ticket, { ...ac, state: 'proven' })).toBe('evidenced')
    render(<ManualAcceptanceReview ticket={{ ...ticket, acceptance_state: [ac, { ...ac, id: 'AC3', accepted_stale: true }] }} name={viewer.name} />)
    expect(screen.getByText(/accepted manually by/)).toHaveTextContent('AC2: accepted manually by Severin (no automated check)')
    expect(screen.getByText(/accepted for an earlier wording/)).toHaveTextContent('AC3: accepted for an earlier wording, no longer counts')
  })
  it('shows the no-receipt fallback and plain history wording', () => {
    render(<ReceiptLine t={{ state: 'done', receipt: null } as TicketDocument['tasks_state'][number]} />)
    expect(screen.getByText('Marked done, no receipt')).toBeVisible()
    const person = { kind: 'person', id: 'p_a' }
    const detail = (e: Record<string, unknown>) => eventDetail({ type: 'artifact.added', ...e } as Parameters<typeof eventDetail>[0], viewer)
    expect(detail({ actor: person, manual_acceptance: { kind: 'accepted', ac: 'AC2' } })).toBe('accepted AC2 manually')
    expect(detail({ actor: person, type: 'artifact.replaced', manual_acceptance: { kind: 'withdrawn', ac: 'AC2' } })).toBe('withdrew the manual acceptance of AC2')
    // words alone are not a marker: an agent's event, a bad marker or only a summary is shown as an ordinary event
    const agent = { kind: 'agent', id: 'x', session: 's', for: 'p_a' }
    expect(detail({ actor: agent, manual_acceptance: { kind: 'accepted', ac: 'AC2' } })).not.toContain('manually')
    expect(detail({ actor: person, manual_acceptance: { kind: 'accepted', ac: 'AC2; drop' } })).not.toContain('manually')
    expect(detail({ actor: agent, summary: 'accepted AC2 manually' })).toBe('artifact.added: accepted AC2 manually')
  })
})


it('renders manual proof with a person icon, local time and stale reacceptance, without increasing progress', async () => {
  const ws = { id: 'a'.repeat(32), prefix: 'DEMO', members: [] } as unknown as Awaited<ReturnType<typeof api.getWorkspaces>>[number]
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([ws])
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(queries.workspaces().queryKey, [ws])
  const { fmtDateTime } = await import('@/lib/time')
  const view = render(<QueryClientProvider client={client}><AcceptanceTasks ticket={{ ...ticket, tasks_state: [], artifacts: [], acceptance_state: [
    { ...ac, accepted: { ...ac.accepted!, note: 'Checked it', auth: 'convenience' } },
    { ...ac, id: 'AC3', proof: 'pending', accepted: undefined, accepted_stale: true },
  ] }} viewer={viewer} jump={() => {}} sign={() => {}} /></QueryClientProvider>)
  expect(screen.getByRole('img', { name: 'Accepted manually' }).querySelector('svg')).toBeTruthy()
  expect(view.container).toHaveTextContent(`Accepted manually by Severin · ${fmtDateTime(ac.accepted!.at)} · one click · “Checked it”`)
  expect(view.container).toHaveTextContent('0/2 evidenced')
  expect(screen.getByRole('button', { name: 'Withdraw' })).toBeVisible()
  expect(screen.getByRole('button', { name: 'Accept again' })).toBeVisible()
})
