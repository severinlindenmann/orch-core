import { createMockTransport, type Transport } from './transport'
import { createMockStore } from '@/mocks/store'
import { setClock } from '@/lib/time'
import {
  ApiError,
  type ActionRequest,
  type ActionResult,
  type AddonActionResult,
  type AddonDecision,
  type AddonPackage,
  type InstalledAddon,
  type KnownPerson,
  type AddonOpRequest,
  type AgentActivityItem,
  type AgentSession,
  type CoreLaunch,
  type GrantInfo,
  type ApiErrorBody,
  type Me,
  type NewTicketRequest,
  type Priority,
  type OrchEvent,
  type SavedView,
  type ViewParams,
  type Status,
  type TicketChanges,
  type CodeReviewApplies,
  type TicketDocument,
  type TicketSummary,
  type TodayDocument,
  type Workspace,
  type WorkspaceIdentity,
  type SettingsRequest,
  type ArtifactPage,
  type ArtifactQuery,
  type RelayRequest,
  type RelaySimRequest,
  type RelayState,
} from './types'
import type { MandatesPreviewRequest, MandatesPreviewState } from './mandatesPreview'
import { connectionInfo, connectionList, doctorReport, secretsFileInfo, skillInfo, skillList, type SkillGrantRequest } from './connections'

export interface ListTicketsParams {
  repo?: string
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
    listPeople: (ws: string) => call<KnownPerson[]>('GET', `/api/workspaces/${ws}/people`),
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
            repo: p.repo,
            person: p.person,
            needs: p.needs,
            sort: p.sort,
            restricted: p.restricted === undefined ? undefined : String(p.restricted),
          }),
      ),
    /** Removes a ticket you just created, while nothing else has happened to it (409 `ticket.undo_too_late`). */
    undoCreateTicket: (ws: string, key: string) => call<{ ok: true }>('POST', `/api/workspaces/${ws}/tickets/${key}/undo-create`),
    listViews: (ws: string) => call<SavedView[]>('GET', `/api/workspaces/${ws}/views`),
    saveView: (ws: string, v: { name: string; shared: boolean; params: ViewParams }) => call<SavedView>('POST', `/api/workspaces/${ws}/views`, v),
    deleteView: (ws: string, id: string) => call<{ ok: true }>('POST', `/api/workspaces/${ws}/views/${id}/delete`),
    createTicket: (ws: string, req: NewTicketRequest) => call<{ ok: true; ticket: TicketDocument }>('POST', `/api/workspaces/${ws}/tickets`, req),
    getTicket: (key: string) => call<TicketDocument>('GET', `/api/tickets/${key}`),
    getEvents: (key: string, since = 0) => call<OrchEvent[]>('GET', `/api/tickets/${key}/events${qs({ since: String(since) })}`),
    postAction: (key: string, action: ActionRequest) => call<ActionResult>('POST', `/api/tickets/${key}/actions`, action),
    /** Dry run of a code review policy: the tickets it would move back to testing or to done (owners). */
    previewCodeReview: (ws: string, req: { count: number; applies: CodeReviewApplies }) =>
      call<{ back: { keys: string[]; hidden: number }; done: { keys: string[]; hidden: number } }>('POST', `/api/workspaces/${ws}/code-review-preview`, req),
    /** Core's diff of the ticket branch against its base. */
    getChanges: (key: string) => call<TicketChanges>('GET', `/api/tickets/${key}/changes`),
    /** Demo: the ticket's agent pushes a commit to its branch (a standing verdict is then void). */
    simulatePush: (key: string) => call<{ ok: true; sha: string; ticket: TicketDocument }>('POST', `/api/dev/tickets/${key}/push`),
    /** Every known addon package (global; no per-workspace state). */
    getAddons: () => call<AddonPackage[]>('GET', '/api/addons'),
    /** Addons installed in a workspace: the package plus that workspace's version, grant and status under `ws`. */
    getWorkspaceAddons: (ws: string) => call<InstalledAddon[]>('GET', `/api/workspaces/${ws}/addons`),
    /** Packages not installed in this workspace. */
    getAddonCatalog: (ws: string) => call<AddonPackage[]>('GET', `/api/workspaces/${ws}/addon-catalog`),
    /** Owner only. grant and update are signed in the dashboard; agents are refused (human_only). Returns the addon as it is now (as it was, for uninstall). */
    postAddonOp: (ws: string, name: string, req: AddonOpRequest) => call<InstalledAddon>('POST', `/api/workspaces/${ws}/addons/${name}`, req),
    getAddonDecisions: (ws: string) => call<AddonDecision[]>('GET', `/api/workspaces/${ws}/addon-decisions`),
    /** Runs an addon action in workspace `ws`. A `ticket` in the body must belong to `ws` (409 ticket.other_workspace). */
    runAddonAction: (ws: string, addon: string, action: string, body: Record<string, unknown> = {}) =>
      call<AddonActionResult>('POST', `/api/workspaces/${ws}/addons/${addon}/actions/${action}`, body),
    /** An addon's state in `ws`. With `ticket`, per-ticket data is computed for that ticket only (ticket panels). */
    getAddonState: (ws: string, name: string, ticket?: string) =>
      call<Record<string, unknown>>('GET', `/api/workspaces/${ws}/addons/${name}/state${ticket ? `?ticket=${encodeURIComponent(ticket)}` : ''}`),
    getAgents: (workspaceId: string) => call<AgentSession[]>('GET', `/api/workspaces/${workspaceId}/agents`),
    /** What core would start for this choice (core-computed: ticket, labels, command, model line). */
    previewLaunch: (ws: string, req: { ticket: string; mode: string; harness: string; where: string }) =>
      call<CoreLaunch>('GET', `/api/workspaces/${ws}/agents/launch${qs(req)}`),
    getAgentActivity: (workspaceId: string) => call<AgentActivityItem[]>('GET', `/api/workspaces/${workspaceId}/agents/activity`),
    listGrants: (ws: string) => call<GrantInfo[]>('GET', `/api/workspaces/${ws}/grants`),
    /** Human only, signed in the dashboard. */
    issueGrant: (ws: string, req: { hours: number; scope: 'all' | 'workable' }) => call<GrantInfo>('POST', `/api/workspaces/${ws}/grants`, req),
    revokeGrant: (ws: string, id: string) => call<GrantInfo>('POST', `/api/workspaces/${ws}/grants/${id}/revoke`),
    // Skills, connections and simple auth (D55–D57). Every answer is parsed with its zod schema; none carries a secret value.
    getSkills: async (ws: string) => skillList.parse(await call('GET', `/api/workspaces/${ws}/skills`)),
    getConnections: async (ws: string) => connectionList.parse(await call('GET', `/api/workspaces/${ws}/connections`)),
    /** Owners and maintainers: the secrets file's path, permissions and names (never values). */
    getSecretsFile: async (ws: string) => secretsFileInfo.parse(await call('GET', `/api/workspaces/${ws}/secrets`)),
    /** Members and above. `relogin`: the owner's "Run check again" after logging in (re-login prompts). */
    runConnectionCheck: async (ws: string, name: string, trigger: 'on_demand' | 'relogin' = 'on_demand') =>
      connectionInfo.parse(await call('POST', `/api/workspaces/${ws}/connections/${encodeURIComponent(name)}/check`, { trigger })),
    /** `orch doctor`: every check, skills with unknown needs, the secrets file. */
    runDoctor: async (ws: string) => doctorReport.parse(await call('POST', `/api/workspaces/${ws}/doctor`)),
    /** Owner only, signed in core's dialog: a credential grant (connection references, env names) to a workspace skill. */
    grantSkillCredentials: async (ws: string, skill: string, req: SkillGrantRequest) =>
      skillInfo.parse(await call('POST', `/api/workspaces/${ws}/skills/${encodeURIComponent(skill)}/grant`, req)),
    getIdentity: (ws: string) => call<WorkspaceIdentity>('GET', `/api/workspaces/${ws}/identity`),
    /** Owner only. Everything except rename is signed in the dashboard. */
    postSettings: (ws: string, req: SettingsRequest) => call<{ ok: true; workspace: Workspace }>('POST', `/api/workspaces/${ws}/settings`, req),
    /** Artifacts of the tickets the viewer can see in `ws`, filtered and paged by the host. */
    listArtifacts: (ws: string, q: ArtifactQuery = {}) =>
      call<ArtifactPage>(
        'GET',
        `/api/workspaces/${ws}/artifacts` +
          qs({ kind: q.kind, ticket: q.ticket, by: q.by, since: q.since, q: q.q, page: q.page === undefined ? undefined : String(q.page), per: q.per === undefined ? undefined : String(q.per) }),
      ),
    /** Relay link, devices, pairing and sync queue (simulated in the mockup). */
    getRelay: (ws: string) => call<RelayState>('GET', `/api/workspaces/${ws}/relay`),
    /** Owner only. connect, stop, pair.confirm and device.remove are signed in the dashboard first. */
    postRelay: (ws: string, req: RelayRequest) => call<RelayState>('POST', `/api/workspaces/${ws}/relay`, req),
    /** Mock only: a dropped connection, or a phone scanning the pairing code. */
    simulateRelay: (ws: string, req: RelaySimRequest) => call<RelayState>('POST', `/api/dev/relay/${ws}`, req),
    /** Mandates, PREVIEW ONLY (not part of the contract; served by the mock; nothing is signed). */
    getLocalMandatesPreview: (ws: string) => transport.previewState?.(ws),
    getMandatesPreview: async (ws: string) => {
      const local = transport.previewState?.(ws)
      if (!local) throw new Error('Mandates preview is unavailable on this transport')
      return local.on ? call<MandatesPreviewState>('GET', `/api/workspaces/${ws}/preview/mandates`) : local
    },
    /** Mandates, PREVIEW ONLY: turn the preview on or off, issue, stop, review, revoke. Never a signing path. */
    postMandatesPreview: async (ws: string, req: MandatesPreviewRequest) => {
      if (!transport.previewState) throw new Error('Mandates preview is unavailable on this transport')
      return call<MandatesPreviewState>('POST', `/api/workspaces/${ws}/preview/mandates`, req)
    },
    /** Mock only: switch the viewer (p_sev, p_mara, p_tom). */
    setViewer: (person: string) => call<{ ok: true }>('POST', '/api/dev/viewer', { person }),
    /** Mock only: restore the seeded demo data; `dataset` switches to the normal demo or the busy day. */
    resetDemo: (dataset?: 'normal' | 'busy') => call<{ ok: true }>('POST', '/api/dev/reset', dataset ? { dataset } : undefined),
    /** Mock only: which demo dataset is loaded. */
    getDataset: () => call<{ dataset: 'normal' | 'busy' }>('GET', '/api/dev/dataset'),
  }
}

export type Api = ReturnType<typeof createApi>

/** The app-wide api object. Today it talks to the in-process mock; swap the transport for createFetchTransport(url). */
const isTest = import.meta.env.MODE === 'test'
/** Exposed for tests only (renderApp resets it). */
export const mockStore = createMockStore({ persist: !isTest, live: !isTest })
export const api: Api = createApi(createMockTransport(mockStore, { latency: !isTest }))
// Relative times are measured against the host's clock (the mock's "now"), not the browser's.
setClock(() => Date.parse(mockStore.now()))
export function resetMockStoreForTests() {
  mockStore.reset('normal')
}
