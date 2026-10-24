import { activityQuery, type ActivityPage, type ActivityParams } from './activity'
import { uploadFor, type ArtifactImage, type PastedRef } from './pasteImage'
import type { Transport } from './transport'
import type { MarketList, MarketInstalled, StageInput, StageResult } from './market'
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
  type TicketRefusal,
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
import type { HandoffAction, HandoffRequest, HandoffState } from './handoff'
import type { ClickAction, ClickCard, ClickState, ClickPrepared, ClickSigned, ClickSwitch, ClickViewAnswer } from './click'
import type { HostGrant, HostReaders, HostSettings, RosterMember } from './settings'
import type { QuickPage, QuickParams, RecordsRun, RecordsStatus } from './quickRecords'
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
  async function call<T>(method: 'GET' | 'POST' | 'PUT' | 'DELETE', path: string, body?: unknown): Promise<T> {
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

  const listTickets = (workspaceId: string, p: ListTicketsParams = {}) =>
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
    )

  return {
    listTickets,
    getMe: (_ws?: string) => call<Me>('GET', '/api/me'),
    listPeople: (ws: string) => call<KnownPerson[]>('GET', `/api/workspaces/${ws}/people`),
    getWorkspaces: () => call<Workspace[]>('GET', '/api/workspaces'),
    /** Core D1 (#582): the host's dev facts (`dev`, `alarms`); `{}` on a normal host and in the mock. Read it through `hostInfoOf`. */
    getHostInfo: () => call<unknown>('GET', '/api/host'),
    getToday: (workspaceId: string) => call<TodayDocument>('GET', `/api/workspaces/${workspaceId}/today`),
    getCursor: (workspaceId: string) => call<{ cursor: number }>('GET', `/api/workspaces/${workspaceId}/cursor`),
    /** The board's own list: every ticket the board draws. The mock serves it as the plain list. */
    listBoard: (workspaceId: string): Promise<TicketSummary[]> => listTickets(workspaceId),
    /** Removes a ticket you just created, while nothing else has happened to it (409 `ticket.undo_too_late`). */
    undoCreateTicket: (ws: string, key: string) => call<{ ok: true }>('POST', `/api/workspaces/${ws}/tickets/${key}/undo-create`),
    listViews: (ws: string) => call<SavedView[]>('GET', `/api/workspaces/${ws}/views`),
    saveView: (ws: string, v: { name: string; shared: boolean; params: ViewParams }) => call<SavedView>('POST', `/api/workspaces/${ws}/views`, v),
    deleteView: (ws: string, id: string) => call<{ ok: true }>('POST', `/api/workspaces/${ws}/views/${id}/delete`),
    createTicket: (ws: string, req: NewTicketRequest) => call<{ ok: true; ticket: TicketDocument }>('POST', `/api/workspaces/${ws}/tickets`, req),
    /** `ws` is used by a real host (workspace UUID); the mock finds the ticket by key. */
    getTicket: (key: string, _ws?: string) => call<TicketDocument>('GET', `/api/tickets/${key}`),
    getEvents: (key: string, since = 0, _ws?: string) => call<OrchEvent[]>('GET', `/api/tickets/${key}/events${qs({ since: String(since) })}`),
    postAction: (key: string, action: ActionRequest) => call<ActionResult>('POST', `/api/tickets/${key}/actions`, action),
    /**
     * The terminal hand-off (M3, real host only): `idem` is one id per click, reused on a retry. The server prepares the
     * sign request; the person signs in a terminal. Nothing here signs.
     */
    checkRepo: (ws: string, action: HandoffAction) => call<{ ok: true }>('POST', `/api/w/${ws}/repos/check`, action),
    prepareHandoff: (ws: string, action: HandoffAction, retry = 0) => call<HandoffRequest>('POST', `/api/w/${ws}/handoff`, { ...action, ...(retry ? { retry } : {}) }),
    getHandoff: (ws: string, id: string) => call<HandoffState>('GET', `/api/w/${ws}/handoff/${id}`),
    /** Click-to-sign (H5c, real host only; the mock never offers it): the signer's View is shown as it is. */
    prepareClick: async (ws: string, action: ClickAction, retry = 0) => {
      await uploadFor(action as { images?: PastedRef[] }, (b) => call('POST', `/api/w/${ws}/click/images`, b)) // R3 part B
      return call<ClickPrepared>('POST', `/api/w/${ws}/click/prepare`, { ...action, ...(retry ? { retry } : {}) })
    },
    /** R3 part B: the bytes of a picture file of a ticket (the page hashes them before showing them). */
    getArtifactImage: (ws: string, key: string, name: string) => call<{ image: ArtifactImage | null }>('GET', `/api/w/${ws}/tickets/${key}/artifacts/${encodeURIComponent(name)}`),
    viewClick: (ws: string, requestId: string) => call<ClickViewAnswer>('POST', `/api/w/${ws}/click/view`, { request_id: requestId }),
    signClick: (ws: string, requestId: string, viewDigest: string, revealKey?: string, sealed?: Record<string, import('./sealed').Envelope>) =>
      call<ClickSigned>('POST', `/api/w/${ws}/click/sign`, { request_id: requestId, view_digest: viewDigest, ...(revealKey ? { reveal_key: revealKey } : {}), ...(sealed ? { sealed } : {}) }),
    stateClick: (ws: string, requestId: string) => call<ClickState>('POST', `/api/w/${ws}/click/state`, { request_id: requestId }),
    hubSign: (ws: string, body: import('./hubsign').HubSignRequest) => call<import('./hubsign').HubSignInfo>('POST', `/api/w/${ws}/hubsign`, body),
    /** V3 A7: the hub pages' reads and the Terminal command of a change (the server signs nothing and sends no hub write). */
    getHubStatus: (ws: string) => call<import('./hub').HubStatus>('GET', `/api/w/${ws}/hub`),
    getHubShare: (ws: string) => call<import('./hub').HubShare>('GET', `/api/w/${ws}/hub/share`),
    getHubPreview: (ws: string, body: { add?: import('./hub').HubRule; remove?: import('./hub').HubRule }) =>
      call<import('./hub').HubPreview>('POST', `/api/w/${ws}/hub/share/preview`, body),
    getHubCommand: (ws: string, body: import('./hub').HubCommandRequest) => call<import('./hub').HubCommand>('POST', `/api/w/${ws}/hub/command`, body),
    cancelClick: (ws: string, requestId: string) => call<{ id: string; state: string }>('POST', `/api/w/${ws}/click/cancel`, { request_id: requestId }),
    clickStatus: (ws: string) => call<ClickCard>('GET', `/api/w/${ws}/click/status`),
    switchClick: (ws: string, body: ClickSwitch) => call<ClickCard>('POST', `/api/w/${ws}/click/custody`, body),
    cancelHandoff: (ws: string, id: string) => call<{ id: string; state: string }>('DELETE', `/api/w/${ws}/handoff/${id}`),
    /** Reduced settings of a real host (M4): what the host says, `available: false` without `settings/1`. */
    phoneAvailable: (ws: string) => call<{ phones: number }>('GET', `/api/w/${ws}/phone/available`),
    phoneRequest: (ws: string, body: import('./phone').PhoneRequest & { idem: string }) => call<import('./phone').PhoneHint & { phones: number }>('POST', `/api/w/${ws}/phone/request`, body),
    phoneCancel: (ws: string, id: string) => call<{ id: string; state: import('./phone').PhoneHint['state'] }>('POST', `/api/w/${ws}/phone/cancel`, { id }),
    phoneStatus: (ws: string, id: string) => call<{ hints: import('./phone').PhoneHint[] }>('GET', `/api/w/${ws}/phone/status/${id}`),
    getSignDefault: (ws: string) => call<import('./signing').SignDefault>('GET', `/api/w/${ws}/me/sign_default`),
    putSignDefault: (ws: string, body: { method: import('./signing').SignMethod; fallback?: import('./signing').SignMethod }) => call<import('./signing').SignDefault>('PUT', `/api/w/${ws}/me/sign_default`, body),
    getHostSettings: (ws: string) => call<HostSettings>('GET', `/api/w/${ws}/settings`),
    getRoster: (ws: string) => call<RosterMember[]>('GET', `/api/w/${ws}/members`),
    getAddOns: (ws: string) => call<import('./addOnSwitches').AddOnsData>('GET', `/api/w/${ws}/add-ons`),
    /** H12d-2: every device's reader level and the workspace's `required_reader` (real host only). */
    getReaders: (ws: string) => call<HostReaders>('GET', `/api/w/${ws}/readers`),
    /** `active: false` adds the grants that ended. An owner is given every entry, anyone else their own. */
    listHostGrants: (ws: string, active = true) => call<HostGrant[]>('GET', `/api/w/${ws}/grants?active=${active}`),
    /** Quick tasks of a real host (M6): `available: false` without `quick/1`. */
    getQuick: (ws: string, _p: QuickParams = {}) => call<QuickPage>('GET', `/api/w/${ws}/quick`),
    /** Marketplace reads and owner-only writes; approval uses the Terminal hand-off. */
    marketList: (ws: string) => call<MarketList>('GET', `/api/w/${ws}/market`),
    marketInstalled: (ws: string) => call<MarketInstalled>('GET', `/api/w/${ws}/market/installed`),
    marketStage: (ws: string, input: StageInput) => call<StageResult>('POST', `/api/w/${ws}/market/stage`, input),
    marketUnstage: (ws: string, stage: string) => call<{ removed: boolean }>('DELETE', `/api/w/${ws}/market/stage/${encodeURIComponent(stage)}`),
    marketUninstall: (ws: string, name: string) => call<{ name: string; removed: boolean }>('POST', `/api/w/${ws}/market/uninstall`, { name }),
    marketRefresh: (ws: string, input: { url?: string } = {}) => call<unknown>('POST', `/api/w/${ws}/market/refresh`, input),
    /** The Activity page of a real host: `available: false` without `activity/1`. */
    getActivity: (ws: string, p: ActivityParams = {}) => call<ActivityPage>('GET', `/api/w/${ws}/activity?${activityQuery(p)}`),
    /** Records of a real host (M6): the host's status for owners and maintainers; `available: false` without `records/1`. */
    getRecordsCurrent: (ws: string) => call<RecordsStatus>('GET', `/api/w/${ws}/records/current`),
    getRecords: (ws: string) => call<RecordsStatus>('GET', `/api/w/${ws}/records`),
    recordsCommit: (ws: string, push: boolean) => call<RecordsRun>('POST', `/api/w/${ws}/records/commit`, { push }),
    recordsAutoOff: (ws: string) => call<RecordsRun>('POST', `/api/w/${ws}/records/auto-off`),
    /** Dry run of a code review policy: the tickets it would move back to testing or to done (owners). */
    previewCodeReview: (ws: string, req: { count: number; applies: CodeReviewApplies }) =>
      call<{ back: { keys: string[]; hidden: number }; done: { keys: string[]; hidden: number } }>('POST', `/api/workspaces/${ws}/code-review-preview`, req),
    /** Core's diff of the ticket branch against its base. */
    getChanges: (key: string, _ws?: string) => call<TicketChanges>('GET', `/api/tickets/${key}/changes`),
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
    getTicketRefusals: (key: string) => call<TicketRefusal[]>('GET', `/api/tickets/${key}/refusals`),
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
