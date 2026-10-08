import { createMockTransport, type Transport } from './transport'
import { createMockStore } from '@/mocks/store'
import {
  ApiError,
  type ActionRequest,
  type ActionResult,
  type AddonActionResult,
  type AddonDecision,
  type AddonManifest,
  type AgentInfo,
  type ApiErrorBody,
  type Me,
  type Priority,
  type OrchEvent,
  type Status,
  type TicketDocument,
  type TicketSummary,
  type TodayDocument,
  type Workspace,
} from './types'

export interface ListTicketsParams {
  status?: Status | Status[]
  q?: string
  type?: string
  parent?: string
  priority?: Priority[]
  label?: string
  /** Owner, assignee or claim `for`. */
  person?: string
  /** Whose turn it is: the viewer, an agent, or nobody. */
  needs?: 'me' | 'agent' | 'nobody'
  sort?: 'updated' | 'priority' | 'key' | 'status'
  restricted?: boolean
}

export function createApi(transport: Transport) {
  async function call<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
    const res = await transport.request(method, path, body)
    if (res.status >= 400) {
      const err = (res.json as ApiErrorBody | null)?.error ?? {
        code: 'http_' + res.status,
        message: 'Request failed',
        retryable: res.status >= 500,
      }
      throw new ApiError(res.status, err)
    }
    return res.json as T
  }

  const qs = (params: Record<string, string | undefined>) => {
    const s = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== '')
      .map(([k, v]) => `${k}=${encodeURIComponent(v as string)}`)
      .join('&')
    return s ? '?' + s : ''
  }

  return {
    getMe: () => call<Me>('GET', '/api/me'),
    getWorkspaces: () => call<Workspace[]>('GET', '/api/workspaces'),
    getToday: (workspaceId: string) => call<TodayDocument>('GET', `/api/workspaces/${workspaceId}/today`),
    getCursor: (workspaceId: string) => call<{ cursor: number }>('GET', `/api/workspaces/${workspaceId}/cursor`),
    listTickets: (workspaceId: string, p: ListTicketsParams = {}) =>
      call<TicketSummary[]>(
        'GET',
        `/api/workspaces/${workspaceId}/tickets` +
          qs({
            status: Array.isArray(p.status) ? p.status.join(',') : p.status,
            q: p.q,
            type: p.type,
            parent: p.parent,
            priority: p.priority?.join(','),
            label: p.label,
            person: p.person,
            needs: p.needs,
            sort: p.sort,
            restricted: p.restricted === undefined ? undefined : String(p.restricted),
          }),
      ),
    getTicket: (key: string) => call<TicketDocument>('GET', `/api/tickets/${key}`),
    getEvents: (key: string, since = 0) => call<OrchEvent[]>('GET', `/api/tickets/${key}/events${qs({ since: String(since) })}`),
    postAction: (key: string, action: ActionRequest) => call<ActionResult>('POST', `/api/tickets/${key}/actions`, action),
    getAddons: () => call<AddonManifest[]>('GET', '/api/addons'),
    getAddonDecisions: () => call<AddonDecision[]>('GET', '/api/addons/decisions'),
    runAddonAction: (addon: string, action: string, body: Record<string, unknown> = {}) =>
      call<AddonActionResult>('POST', `/api/addons/${addon}/actions/${action}`, body),
    getAddonState: (ws: string, name: string) => call<Record<string, unknown>>('GET', `/api/workspaces/${ws}/addons/${name}/state`),
    getAgents: (workspaceId: string) => call<AgentInfo[]>('GET', `/api/workspaces/${workspaceId}/agents`),
    /** Mock only: switch the viewer (p_sev, p_mara, p_tom). */
    setViewer: (person: string) => call<{ ok: true }>('POST', '/api/dev/viewer', { person }),
    /** Mock only: restore the seeded demo data. */
    resetDemo: () => call<{ ok: true }>('POST', '/api/dev/reset'),
  }
}

export type Api = ReturnType<typeof createApi>

/** The app-wide api object. Today it talks to the in-process mock; swap the transport for createFetchTransport(url). */
const isTest = import.meta.env.MODE === 'test'
/** Exposed for tests only (renderApp resets it). */
export const mockStore = createMockStore({ persist: !isTest })
export const api: Api = createApi(createMockTransport(mockStore, { latency: !isTest }))
export function resetMockStoreForTests() {
  mockStore.reset()
}
