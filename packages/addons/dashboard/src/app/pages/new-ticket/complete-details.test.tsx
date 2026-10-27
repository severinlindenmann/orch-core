import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ARM_MS, CLICK_RISK, type ClickViewAnswer } from '@/api/click'
import type { FollowUpAction } from '@/api/handoff'
import { ApiError } from '@/api/types'
import * as gesture from '@/components/sign/trustedPress'
import { fakeRequest, state } from '@/test/handoffHarness'
import { CompleteDetails } from './CompleteDetails'
import { EMPTY, handoffNewTicket, ownKey } from './index'

vi.mock('@/app/workspace', () => ({ useWorkspace: () => ({ workspaces: [{ id: 'w_1', prefix: 'DEMO', name: 'Demo', click: { enabled: true, available: true, custody: 'convenience' } }] }) }))
vi.mock('@/app/useMe', () => ({ useMe: () => ({ data: { person: 'p_sev' } }) }))

const T = { timeout: 5000 }
const KEY = 'DEMO-0044'
const REQ = (n: number) => 'hr_' + '0'.repeat(25) + n
const view = (line: string): ClickViewAnswer => ({
  view_digest: 'sha256:' + 'c'.repeat(64),
  view: {
    risk: CLICK_RISK, op: 'set', workspace: { id: 'w_1', name: 'Demo', prefix: 'DEMO' }, fingerprint: '0123456789ab', lines: [line], checks: [],
    confirm: { kind: 'click' }, expires_at: new Date(Date.now() + 600_000).toISOString(), auth: 'convenience', device: 'd_x', artifacts: null,
  },
})
let s: { prepare: ReturnType<typeof vi.spyOn>; view: ReturnType<typeof vi.spyOn>; sign: ReturnType<typeof vi.spyOn>; handoff: ReturnType<typeof vi.spyOn>; postAction: ReturnType<typeof vi.spyOn> }

/** The armed Sign of the open review, pressed (fireEvent stands in for a real press: trustedPress is mocked). */
async function signOpenReview(label: string) {
  const panel = await screen.findByTestId('click-panel', {}, T)
  const b = within(panel.closest('[role="dialog"]') as HTMLElement).getByRole('button', { name: label })
  await waitFor(() => expect(b).toBeEnabled(), { timeout: ARM_MS + 2000 })
  fireEvent.pointerDown(b)
  fireEvent.click(b)
}

const DETAILS: FollowUpAction = { action: 'details', key: '', priority: 'high', labels: ['api'], parent: 'DEMO-0003' }
const SECTION: FollowUpAction = { action: 'section', key: '', section: 'requirements', text: 'Empty months export a header row' }

function mount(steps: FollowUpAction[], onFinish = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <CompleteDetails ws="w_1" ticketKey={KEY} steps={steps} onFinish={onFinish} />
    </QueryClientProvider>,
  )
  return { onFinish }
}

beforeEach(() => {
  vi.spyOn(gesture, 'trustedPress').mockReturnValue(true) // fireEvent is untrusted: stand in for a real press of the outcome button
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([])
  vi.spyOn(api, 'cancelClick').mockResolvedValue({ id: REQ(1), state: 'cancelled' })
  s = {
    prepare: vi.spyOn(api, 'prepareClick'),
    view: vi.spyOn(api, 'viewClick'),
    sign: vi.spyOn(api, 'signClick'),
    // One click only: the terminal hand-off and an unsigned action are never used.
    handoff: vi.spyOn(api, 'prepareHandoff'),
    postAction: vi.spyOn(api, 'postAction'),
  }
})
afterEach(async () => {
  cleanup()
  await new Promise((r) => setTimeout(r, 20))
  vi.restoreAllMocks()
})

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
    expect(dialog).not.toHaveTextContent('orch sign')
    expect(s.prepare).not.toHaveBeenCalled()
  })

  it('prepares the follow-up for the created ticket on the press, signs it after the arm, then the next one, then finishes', async () => {
    s.prepare
      .mockResolvedValueOnce({ request_id: REQ(3), expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'set', confirm: 'click' })
      .mockResolvedValueOnce({ request_id: REQ(4), expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'section.set', confirm: 'click' })
    s.view.mockImplementation(async (_ws, id) => view(id === REQ(3) ? 'Set DEMO-0044 priority high' : 'Set DEMO-0044 requirements'))
    s.sign.mockResolvedValue({ state: 'submitted', duplicate: false, key: KEY, seq: 5 })
    const { onFinish } = mount([DETAILS, SECTION])
    fireEvent.click(await screen.findByRole('button', { name: 'Prepare details' }))
    await waitFor(() => expect(s.prepare).toHaveBeenNthCalledWith(1, 'w_1', { ...DETAILS, key: KEY }, 0))
    expect(s.sign).not.toHaveBeenCalled() // the press prepares; only the armed Sign signs
    await signOpenReview('Prepare details')
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Complete details (2 of 2)' })).toBeInTheDocument(), { timeout: 9000 })
    expect(screen.getByTestId('complete-details-lines')).toHaveTextContent('Requirements: Empty months export a header row')
    expect(onFinish).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Prepare section' }))
    await waitFor(() => expect(s.prepare).toHaveBeenNthCalledWith(2, 'w_1', { ...SECTION, key: KEY }, 0))
    await signOpenReview('Prepare section')
    await waitFor(() => expect(onFinish).toHaveBeenCalledWith(2), { timeout: 9000 })
    expect(s.sign).toHaveBeenCalledTimes(2)
    expect(s.handoff).not.toHaveBeenCalled()
    expect(s.postAction).not.toHaveBeenCalled()
  }, 30000)

  it('cancel signs nothing, prepares nothing and ends the offer', async () => {
    const { onFinish } = mount([DETAILS])
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(onFinish).toHaveBeenCalledWith(0) // cancelled with nothing signed: the caller says what is not saved
    expect(s.prepare).not.toHaveBeenCalled()
    expect(s.sign).not.toHaveBeenCalled()
    expect(s.postAction).not.toHaveBeenCalled()
  })

  it('shows the host refusing a follow-up and does not move on', async () => {
    s.prepare.mockRejectedValue(new ApiError(400, { code: 'invalid.input', message: 'a feature ticket has no section nope', retryable: false }))
    const { onFinish } = mount([SECTION, DETAILS])
    fireEvent.click(await screen.findByRole('button', { name: 'Prepare section' }))
    expect(await screen.findByText('a feature ticket has no section nope', {}, T)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Complete details (1 of 2)' })).toBeInTheDocument()
    expect(s.sign).not.toHaveBeenCalled()
    expect(onFinish).not.toHaveBeenCalled()
  })
})

describe('what a signed new ticket hands on', () => {
  it('sends the title, type, fields and each written section as ONE intent (one review, one confirmation)', () => {
    const made = handoffNewTicket({ ...EMPTY, title: 'T', type: 'bug', priority: 'high', size: 's', labels: ['api'], parent: 'DEMO-0003', sections: { requirements: ' Must work ', context: '' } })
    expect(made).toEqual({
      action: { action: 'new_sequence', title: 'T', type: 'bug', priority: 'high', size: 's', labels: ['api'], parent: 'DEMO-0003', sections: { requirements: 'Must work' } },
    })
  })
  it('sends only the title and type when nothing else is written, and never a parent for an epic', () => {
    expect(handoffNewTicket({ ...EMPTY, title: 'Plain' })).toEqual({ action: { action: 'new_sequence', title: 'Plain', type: 'feature', sections: {} } })
    const epic = handoffNewTicket({ ...EMPTY, title: 'E', type: 'epic', parent: 'DEMO-0003' }) as { action: Record<string, unknown> }
    expect(epic.action).not.toHaveProperty('parent')
  })
  it('keeps a spike\'s findings under the host\'s name', () => {
    const made = handoffNewTicket({ ...EMPTY, title: 'S', type: 'spike', sections: { verification: 'What we found' } })
    expect(made).toMatchObject({ action: { sections: { findings: 'What we found' } } })
  })
  it('refuses what the host has no signed operation for, instead of dropping it', () => {
    for (const patch of [{ visibility: { restricted: ['p_x'] } }, { due: '2026-11-01' }]) {
      const made = handoffNewTicket({ ...EMPTY, title: 'T', ...patch })
      expect(made).toHaveProperty('problem')
      expect((made as { problem: string }).problem).toMatch(/cannot carry/)
    }
  })
})

describe('Complete details is bound to the new ticket this dialog signed', () => {
  const own = fakeRequest({ id: 'hr_' + '0'.repeat(25) + '7', key: 'DEMO-0044' })
  const asNew = { ...own, op: 'new' }
  it('takes the key only from the state of the very request that was prepared, an op new', () => {
    expect(ownKey(state(asNew, { state: 'submitted', key: 'DEMO-0044' }), asNew)).toBe('DEMO-0044')
  })
  it('gives no key for another request id, another op, or no request at all', () => {
    const other = { ...state(asNew, { state: 'submitted', key: 'DEMO-0099' }), id: 'hr_' + '0'.repeat(25) + '8' }
    expect(ownKey(other, asNew)).toBeNull()
    expect(ownKey(state(own, { state: 'submitted' }), own)).toBeNull() // not a new
    expect(ownKey(state(asNew, { state: 'submitted' }), null)).toBeNull()
    expect(ownKey(null, asNew)).toBeNull()
  })
  it('counts a written section in UTF-8 bytes, like the server', () => {
    const text = 'ä'.repeat(40000) // 40000 characters, 80000 bytes
    expect(handoffNewTicket({ ...EMPTY, title: 'T', sections: { requirements: text } })).toHaveProperty('problem')
    expect(handoffNewTicket({ ...EMPTY, title: 'T', sections: { requirements: 'ä'.repeat(32768) } })).not.toHaveProperty('problem') // exactly 65536 bytes
  })
})
