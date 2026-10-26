// Test harness for the terminal hand-off (M3): the real-host mode of core's signing dialog, with the three hand-off
// api calls replaced by spies. The ticket comes from the mock store, so a test can prove the preview is NOT built from
// it: the host's `material` says other things than the cached ticket does.
import { useSigningMethodRefresh } from '@/api/signingRefresh'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import { render } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { api, mockStore, resetMockStoreForTests } from '@/api/client'
import { setFeatures } from '@/api/features'
import { fingerprintOf, reviewDigestOf, type HandoffEvent, type HandoffMaterial, type HandoffRequest, type HandoffState } from '@/api/handoff'
import { ApiError, type TicketDocument } from '@/api/types'
import type { HumanAction } from '@/app/pages/ticket/shared'
import { SignDialog } from '@/app/pages/ticket/SignDialog'

export const CODE = '7F3K-Q2MA'

/** A request whose hashes recompute, as the dashboard server would send it. */
export function fakeRequest(over: { material?: Partial<HandoffMaterial>; events?: HandoffEvent[]; code?: string | null; id?: string; expiresInMs?: number; gate_hash?: string | null; key?: string } = {}): HandoffRequest {
  const key = over.key ?? 'DEMO-0044'
  const events: HandoffEvent[] = over.events ?? [
    {
      v: 2,
      id: '01J9ZP0000000000000000000B',
      type: 'gate.approved',
      actor: { kind: 'person', id: 'p_sev', device: null },
      auth: null,
      hash_v: 1,
      roster_v: 4,
      based_on: 'sha256:' + 'a'.repeat(64),
      gate: 'plan',
      gate_gen: 2,
      hash: 'sha256:' + 'b'.repeat(64),
      policy_hash: 'sha256:' + 'c'.repeat(64),
    },
  ]
  const material: HandoffMaterial = {
    kind: 'gate',
    gate: 'plan',
    input: { tasks: [{ id: 'T1', text: 'HOST-MATERIAL task text' }], fields: { ticket_type: 'feature', acceptance: [{ id: 'AC1', text: 'From the host ‮ only' }] } },
    policy: { approvers: 'owner', count: 1 },
    texts: {},
    ticket: { key, uid: '01J9ZP0000000000000000000A' },
    review: [{ event_id: events[0].id as string, type: events[0].type, payload: { gate: 'plan', hash: events[0].hash, policy_hash: events[0].policy_hash }, derived: { ticket_key: key } }],
    ...over.material,
  }
  const digest = reviewDigestOf(material)
  return {
    id: over.id ?? 'hr_' + '0'.repeat(25) + '1',
    code: over.code === undefined ? CODE : over.code,
    expires_at: new Date(Date.now() + (over.expiresInMs ?? 600_000)).toISOString().replace(/\.\d+Z$/, 'Z'),
    state: 'prepared',
    op: 'approve',
    key,
    fingerprint: fingerprintOf(digest, events, 'approve'),
    review_digest: digest,
    gate_hash: over.gate_hash === undefined ? 'sha256:' + 'b'.repeat(64) : over.gate_hash,
    material,
    events,
    origin: { uid: 501, pid: 4312, client: 'orch-dashboard', created_at: new Date().toISOString() },
  }
}

export const state = (request: HandoffRequest, over: Partial<HandoffState> = {}): HandoffState => ({
  id: request.id,
  state: 'prepared',
  op: request.op,
  key: request.key,
  expires_at: request.expires_at,
  event_ids: [],
  seq: null,
  ...over,
})

export const refusal = (code: string, status = 409, retryable = false) => new ApiError(status, { code, message: `Host said ${code}`, retryable })

export function spies() {
  return {
    prepare: vi.spyOn(api, 'prepareHandoff'),
    get: vi.spyOn(api, 'getHandoff'),
    cancel: vi.spyOn(api, 'cancelHandoff').mockResolvedValue({ id: 'x', state: 'cancelled' }),
    // The dashboard has no way to sign or to post a signed action: any of these being called is a bug.
    postAction: vi.spyOn(api, 'postAction'),
  }
}

function Refresh() { useSigningMethodRefresh(); return null }

export async function mount(action: HumanAction, opts: { key?: string; onClose?: () => void } = {}) {
  resetMockStoreForTests()
  mockStore.setViewer('p_sev')
  const ticket: TicketDocument = await api.getTicket(opts.key ?? 'DEMO-0044')
  const ws = mockStore.workspaces.find((w) => ticket.key.startsWith(w.prefix + '-'))!.id
  // A real host that serves the signed actions, and nothing about `move`.
  setFeatures({ features: ['identity', 'today', 'tickets', 'board', 'ticket', 'actions'], workspaces: { [ws]: ['identity', 'today', 'tickets', 'board', 'ticket', 'actions'] } })
  const onClose = opts.onClose ?? vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const root = createRootRoute({
    component: () => (
      <QueryClientProvider client={client}>
        <Refresh />
        <SignDialog ticket={ticket} action={action} onClose={onClose} />
      </QueryClientProvider>
    ),
  })
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ['/'] }) })
  const user = userEvent.setup()
  const view = render(<RouterProvider router={router} />)
  return { ...view, user, ws, ticket, onClose, client }
}
