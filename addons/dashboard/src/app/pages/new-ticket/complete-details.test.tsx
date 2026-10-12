import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fingerprintOf, reviewDigestOf, type FollowUpAction, type HandoffEvent, type HandoffRequest } from '@/api/handoff'
import { fakeRequest, spies, state } from '@/test/handoffHarness'
import { CompleteDetails } from './CompleteDetails'
import { EMPTY, handoffNewTicket } from './index'

const T = { timeout: 5000 }
const KEY = 'DEMO-0044'
let s: ReturnType<typeof spies>

/** A prepared `set` or `section.set` request as the host would send it, with hashes that recompute. */
function prepared(op: 'set' | 'section.set', extra: Record<string, unknown>, id: string): HandoffRequest {
  const event: HandoffEvent = { v: 2, id, type: 'ticket.updated', actor: { kind: 'person', id: 'p_sev', device: null }, auth: null, hash_v: 1, roster_v: 4, based_on: 'sha256:' + 'a'.repeat(64), ...extra }
  const payload = Object.fromEntries(Object.entries(event).filter(([k]) => !['v', 'id', 'type', 'actor', 'auth', 'hash_v', 'roster_v', 'based_on', 'sig'].includes(k)))
  const base = fakeRequest({ key: KEY, events: [event], material: { kind: 'payload', review: [{ event_id: id, type: 'ticket.updated', payload, derived: {} }] } })
  const material = { kind: 'payload' as const, ticket: base.material.ticket, review: base.material.review }
  const digest = reviewDigestOf(material)
  return { ...base, id: 'hr_' + '0'.repeat(25) + (op === 'set' ? '3' : '4'), op, material, review_digest: digest, fingerprint: fingerprintOf(digest, [event], op), gate_hash: null }
}

const DETAILS: FollowUpAction = { action: 'details', key: '', priority: 'high', labels: ['api'], parent: 'DEMO-0003' }
const SECTION: FollowUpAction = { action: 'section', key: '', section: 'requirements', text: 'Empty months export a header row' }

function mount(steps: FollowUpAction[], onFinish = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const user = userEvent.setup()
  render(
    <QueryClientProvider client={client}>
      <CompleteDetails ws="w_1" ticketKey={KEY} steps={steps} onFinish={onFinish} />
    </QueryClientProvider>,
  )
  return { user, onFinish }
}

beforeEach(() => {
  s = spies()
})
afterEach(() => vi.restoreAllMocks())

describe('Complete details after a signed new ticket', () => {
  it('is offered with what it will set, and prepares nothing until the person asks', async () => {
    mount([DETAILS, SECTION])
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: 'Complete details (1 of 2)' })).toBeInTheDocument()
    const lines = within(dialog).getByTestId('complete-details-lines')
    expect(lines).toHaveTextContent('Priority: high')
    expect(lines).toHaveTextContent('Labels: api')
    expect(lines).toHaveTextContent('Parent epic: DEMO-0003')
    expect(dialog).toHaveTextContent(`${KEY} is created`)
    expect(s.prepare).not.toHaveBeenCalled()
  })

  it('prepares the follow-up for the created ticket, then the next one after it is signed, then finishes', async () => {
    const first = prepared('set', { set: { 'ticket.priority': 'high' }, base_rev: { 'ticket.priority': 'sha256:' + 'c'.repeat(64) } }, '01J9ZP000000000000000000C1')
    const second = prepared('section.set', { sections: { requirements: { hash: 'sha256:' + 'd'.repeat(64), refs: [] } }, base_rev: { 'body.requirements': 'sha256:' + 'e'.repeat(64) } }, '01J9ZP000000000000000000C2')
    s.prepare.mockResolvedValueOnce(first).mockResolvedValueOnce(second)
    s.get.mockImplementation(async (_ws, id) => state(id === first.id ? first : second, { state: 'submitted', event_ids: ['x'], seq: 5 }))
    const { user, onFinish } = mount([DETAILS, SECTION])
    await user.click(await screen.findByRole('button', { name: 'Prepare details' }))
    expect(s.prepare).toHaveBeenNthCalledWith(1, 'w_1', { ...DETAILS, key: KEY }, expect.stringMatching(/^a_/))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Complete details (2 of 2)' })).toBeInTheDocument(), { timeout: 9000 })
    expect(screen.getByTestId('complete-details-lines')).toHaveTextContent('Requirements: Empty months export a header row')
    expect(onFinish).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Prepare section' }))
    expect(s.prepare).toHaveBeenNthCalledWith(2, 'w_1', { ...SECTION, key: KEY }, expect.stringMatching(/^a_/))
    await waitFor(() => expect(onFinish).toHaveBeenCalledTimes(1), { timeout: 9000 })
    expect(s.postAction).not.toHaveBeenCalled()
  }, 30000)

  it('cancel signs nothing, prepares nothing and ends the offer', async () => {
    const { user, onFinish } = mount([DETAILS])
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(onFinish).toHaveBeenCalledTimes(1)
    expect(s.prepare).not.toHaveBeenCalled()
    expect(s.postAction).not.toHaveBeenCalled()
  })

  it('shows the host refusing a follow-up and does not move on', async () => {
    const { ApiError } = await import('@/api/types')
    s.prepare.mockRejectedValue(new ApiError(400, { code: 'invalid.input', message: 'a feature ticket has no section nope', retryable: false }))
    const { user, onFinish } = mount([SECTION, DETAILS])
    await user.click(await screen.findByRole('button', { name: 'Prepare section' }))
    expect(await screen.findByText('Could not prepare', {}, T)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Complete details (1 of 2)' })).toBeInTheDocument()
    expect(onFinish).not.toHaveBeenCalled()
  })
})

describe('what a signed new ticket hands on', () => {
  it('turns priority, size, labels and the parent into one details step and each written section into a section step', () => {
    const made = handoffNewTicket({ ...EMPTY, title: 'T', type: 'bug', priority: 'high', size: 's', labels: ['api'], parent: 'DEMO-0003', sections: { requirements: ' Must work ', context: '' } })
    expect(made).toEqual({
      action: { action: 'new', title: 'T', type: 'bug' },
      details: [
        { action: 'details', key: '', priority: 'high', size: 's', labels: ['api'], parent: 'DEMO-0003' },
        { action: 'section', key: '', section: 'requirements', text: 'Must work' },
      ],
    })
  })
  it('offers nothing when there is nothing to hand on, and never a parent for an epic', () => {
    expect(handoffNewTicket({ ...EMPTY, title: 'Plain' })).toEqual({ action: { action: 'new', title: 'Plain', type: 'feature' }, details: [] })
    expect(handoffNewTicket({ ...EMPTY, title: 'E', type: 'epic', parent: 'DEMO-0003' })).toMatchObject({ details: [] })
  })
  it('keeps a spike\'s findings under the host\'s name', () => {
    const made = handoffNewTicket({ ...EMPTY, title: 'S', type: 'spike', sections: { verification: 'What we found' } })
    expect(made).toMatchObject({ details: [{ action: 'section', section: 'findings', text: 'What we found' }] })
  })
  it('refuses what the host has no signed operation for, instead of dropping it', () => {
    for (const patch of [{ visibility: { restricted: ['p_x'] } }, { due: '2026-11-01' }, { acceptance: ['x'] }, { people: { owner: 'p_x', assignees: [], reviewers: [] } }]) {
      const made = handoffNewTicket({ ...EMPTY, title: 'T', ...patch })
      expect(made).toHaveProperty('problem')
      expect((made as { problem: string }).problem).toMatch(/cannot carry/)
    }
  })
})
