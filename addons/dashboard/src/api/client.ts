import { createMockTransport, type Transport } from './transport'
import { createMockStore } from '@/mocks/store'
import {
  ApiError,
  type ActionRequest,
  type ActionResult,
  type AddonActionResult,
  type AddonDecision,
  type AddonPackage,
  type InstalledAddon,
  type AddonOpRequest,
  type AgentActivityItem,
  type AgentSession,
  type GrantInfo,
  type ApiErrorBody,
  type Me,
  type NewTicketRequest,
  type Priority,
  type OrchEvent,
  type SavedView,
  type ViewParams,
  type Status,
  type TicketDocument,
  type TicketSummary,
  type TodayDocument,
  type Workspace,
  type WorkspaceIdentity,
  type SettingsRequest,
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
    listViews: (ws: string) => call<SavedView[]>('GET', `/api/workspaces/${ws}/views`),
    saveView: (ws: string, v: { name: string; shared: boolean; params: ViewParams }) => call<SavedView>('POST', `/api/workspaces/${ws}/views`, v),
    deleteView: (ws: string, id: string) => call<{ ok: true }>('POST', `/api/workspaces/${ws}/views/${id}/delete`),
    createTicket: (ws: string, req: NewTicketRequest) => call<{ ok: true; ticket: TicketDocument }>('POST', `/api/workspaces/${ws}/tickets`, req),
    getTicket: (key: string) => call<TicketDocument>('GET', `/api/tickets/${key}`),
    getEvents: (key: string, since = 0) => call<OrchEvent[]>('GET', `/api/tickets/${key}/events${qs({ since: String(since) })}`),
    postAction: (key: string, action: ActionRequest) => call<ActionResult>('POST', `/api/tickets/${key}/actions`, action),
    /** Every known addon package (global; no per-workspace state). */
    getAddons: () => call<AddonPackage[]>('GET', '/api/addons'),
    /** Addons installed in a workspace: the package plus that workspace's version, grant and status under `ws`. */
    getWorkspaceAddons: (ws: string) => call<InstalledAddon[]>('GET', `/api/workspaces/${ws}/addons`),
    /** Packages not installed in this workspace. */
    getAddonCatalog: (ws: string) => call<AddonPackage[]>('GET', `/api/workspaces/${ws}/addons/catalog`),
    /** Owner only. grant and update are signed in the dashboard; agents are refused (human_only). Returns the addon as it is now (as it was, for uninstall). */
    postAddonOp: (ws: string, name: string, req: AddonOpRequest) => call<InstalledAddon>('POST', `/api/workspaces/${ws}/addons/${name}`, req),
    getAddonDecisions: (ws: string) => call<AddonDecision[]>('GET', `/api/workspaces/${ws}/addons/decisions`),
    runAddonAction: (addon: string, action: string, body: Record<string, unknown> = {}) =>
      call<AddonActionResult>('POST', `/api/addons/${addon}/actions/${action}`, body),
    getAddonState: (ws: string, name: string) => call<Record<string, unknown>>('GET', `/api/workspaces/${ws}/addons/${name}/state`),
    getAgents: (workspaceId: string) => call<AgentSession[]>('GET', `/api/workspaces/${workspaceId}/agents`),
    getAgentActivity: (workspaceId: string) => call<AgentActivityItem[]>('GET', `/api/workspaces/${workspaceId}/agents/activity`),
    listGrants: (ws: string) => call<GrantInfo[]>('GET', `/api/workspaces/${ws}/grants`),
    /** Human only, signed in the dashboard. */
    issueGrant: (ws: string, req: { hours: number; scope: 'all' }) => call<GrantInfo>('POST', `/api/workspaces/${ws}/grants`, req),
    revokeGrant: (ws: string, id: string) => call<GrantInfo>('POST', `/api/workspaces/${ws}/grants/${id}/revoke`),
    getIdentity: (ws: string) => call<WorkspaceIdentity>('GET', `/api/workspaces/${ws}/identity`),
    /** Owner only. Everything except rename is signed in the dashboard. */
    postSettings: (ws: string, req: SettingsRequest) => call<{ ok: true; workspace: Workspace }>('POST', `/api/workspaces/${ws}/settings`, req),
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
