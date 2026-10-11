import { ApiError, type ApiErrorBody } from './types'
import type { Me, Member, NeedsYouItem, TicketSummary, Workspace, WorkspaceIdentity } from './types'
import { createFetchTransport } from './fetchTransport'
import { clearSession, getToken } from './bootstrap'

// Explicit M0 projections: later UI fields are unavailable, not fabricated defaults.
export type ShellWorkspace = Pick<Workspace, 'id' | 'prefix' | 'name'> & {
  members: Member[]; needs_you: number | null; features: string[]; host_state: string
}
export type ShellMe = Me & { tier: string; workspace: string }
export type ShellIdentity = Pick<WorkspaceIdentity, 'uuid' | 'prefix'> & {
  key_fingerprint: string | null; epoch: number | null; created_at: string | null; genesis: string | null; tier: string; me: ShellMe
}
export type ShellTicket = Pick<TicketSummary, 'key' | 'uid' | 'title' | 'type' | 'priority' | 'size' | 'labels' | 'parent' | 'owner' | 'assignees'> & {
  status: TicketSummary['status'] | 'closed'; updated_at: string | null
  claim: { session: string; agent: string | null } | null; gates: Record<string, string>; needs: string[]; head_seq: number; epic: string | null
}
export type ShellNeed = Pick<NeedsYouItem, 'text'> & {
  kind: NeedsYouItem['kind'] | 'no_eligible' | 'acknowledge' | 'addon'; ticket: string | null; uid: string | null
  ref: string | null; title: string | null; since: string | null; who: string[]
}
const transport = createFetchTransport('', getToken)
async function call<T>(path: string): Promise<T> {
  const result = await transport.request('GET', path)
  if (result.status === 401) { clearSession(); throw new Error('Run orch dashboard in your terminal') }
  if (result.status >= 400) {
    const body = result.json as ApiErrorBody
    throw new ApiError(result.status, body.error ?? { code: 'host.unavailable', message: 'Host not available', retryable: true }, body.exit)
  }
  return result.json as T
}
export const realApi = {
  session: () => call<{ features: string[]; workspaces: Record<string, string[]> }>('/api/session'),
  workspaces: () => call<ShellWorkspace[]>('/api/workspaces'),
  me: () => call<ShellMe[]>('/api/me'),
  identity: (ws: string) => call<ShellIdentity>(`/api/w/${encodeURIComponent(ws)}/identity`),
  today: (ws: string) => call<{ workspace: string; needs_you: ShellNeed[]; sub_cursor: string | null }>(`/api/w/${encodeURIComponent(ws)}/today`),
  tickets: (ws: string, page?: string) => call<{ rows: ShellTicket[]; next_page: string | null; total: number }>(`/api/w/${encodeURIComponent(ws)}/tickets${page ? '?page=' + encodeURIComponent(page) : ''}`),
  async logout() {
    const result = await transport.request('POST', '/auth/logout')
    if (result.status >= 400 && result.status !== 401) throw new Error('Logout failed; please try again')
    clearSession()
  },
}
