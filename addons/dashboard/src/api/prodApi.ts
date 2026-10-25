// The production `api`: the same object the mockup's pages use, behind the fetch transport to the dashboard server
// (design 7). Reads the server serves are mapped to the UI's types; what a read-only M2 host does not serve answers
// neutrally (an empty list) where a page needs an answer to draw, and with `op.unsupported` everywhere else. Nothing
// here imports the mock.
import { clearSession, getToken } from './bootstrap'
import { refreshFeatures } from './features'
import { createFetchTransport } from './fetchTransport'
import type { MandatesPreviewState } from './mandatesPreview'
import type { Api, ListTicketsParams } from './apiFactory'
import type { SignDefault, SignMethod } from './signing'
import type { HandoffAction, HandoffRequest, HandoffState } from './handoff'
import type { ClickAction, ClickCard, ClickState, ClickPrepared, ClickSigned, ClickSwitch, ClickViewAnswer, CustodySealOffer, RotateBegin } from './click'
import type { HostGrant, HostReaders, HostSettings, RosterMember } from './settings'
import type { QuickPage, QuickParams, RecordsRun, RecordsStatus } from './quickRecords'
import { activityQuery, type ActivityPage, type ActivityParams } from './activity'
import type { MarketList, MarketInstalled, StageInput, StageResult } from './market'
import { uploadFor, type ArtifactImage, type PastedRef } from './pasteImage'
import {
  ApiError,
  type ApiErrorBody,
  type KnownPerson,
  type Me,
  type Member,
  type Role,
  type Status,
  type TicketChanges,
  type TicketDocument,
  type TicketSummary,
  type TodayDocument,
  type Workspace,
  type WorkspaceIdentity,
  type OrchEvent,
} from './types'

interface ServerWorkspace {
  id: string
  prefix: string
  name: string
  members: Member[]
  needs_you: number | null
  host_state: string
  host_message?: string | null
  features: string[]
  click?: Workspace['click']
  runner?: Workspace['runner']
  icon?: { icon: string; colour: string } | null
  icon_known?: boolean
}
interface ServerMe {
  person: string
  name: string
  role: Role
  tier: string
  workspace: string
}
interface ServerPage {
  rows: TicketSummary[]
  next_page: string | null
  total: number
  sub_cursor: string | null
}

const ROLE_RANK: Record<Role, number> = { viewer: 0, member: 1, maintainer: 2, owner: 3 }
/**
 * Pages the board, Today and the Tickets list read at most. A page may hold fewer rows than asked (rows drop out between
 * pages, a scan budget ends a page early): only `next_page` says whether more follow, never the size of a page.
 */
const MAX_PAGES = 25

async function call<T>(path: string, method: 'GET' | 'POST' | 'PUT' | 'DELETE' = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
  const token = getToken()
  const result = await createFetchTransport('', () => token).request(method, path, body, signal)
  if (result.status === 401) {
    clearSession(true, token)
    throw new ApiError(401, { code: 'session.required', message: 'Run orch dashboard in your terminal', retryable: false })
  }
  if (result.status >= 400) {
    const body = result.json as ApiErrorBody | null
    throw new ApiError(
      result.status,
      body?.error ?? { code: 'host.unavailable', message: 'Host not available', retryable: result.status >= 500 },
      body?.exit,
    )
  }
  return result.json as T
}

const unsupported = (what: string) =>
  Promise.reject(new ApiError(501, { code: 'op.unsupported', message: `${what} is not available in the read-only dashboard yet.`, retryable: false }))

const w = (ws: string) => `/api/w/${encodeURIComponent(ws)}`

function ticketQuery(p: ListTicketsParams, page?: string): string {
  const q = new URLSearchParams()
  // `waiting` is derived, not a host status: asking for only that matches nothing.
  const statuses = (Array.isArray(p.status) ? p.status : p.status ? [p.status] : []).filter((s) => s !== 'waiting')
  for (const s of statuses) q.append('status', s)
  if (p.q) q.set('q', p.q)
  if (p.type) q.set('type', p.type)
  if (p.parent) q.set('parent', p.parent)
  if (p.label) q.set('label', p.label)
  if (p.needs === 'me') q.set('needs_me', 'true')
  if (p.sort === 'priority' || p.sort === 'key') q.set('sort', p.sort)
  if (p.sort === 'key') q.set('order', 'asc')
  q.set('limit', '200')
  if (page) q.set('page', page)
  return q.toString()
}

/** What the host cannot filter or sort: the rest of the mock's list parameters, applied to the rows read. */
function narrow(rows: TicketSummary[], p: ListTicketsParams): TicketSummary[] {
  let out = rows
  if (p.priority?.length) out = out.filter((t) => t.priority !== null && p.priority!.includes(t.priority))
  if (p.person) out = out.filter((t) => t.owner === p.person || t.assignees.includes(p.person!) || t.claim?.for === p.person)
  if (p.needs === 'agent') out = out.filter((t) => t.turn.who.startsWith('agent:'))
  if (p.needs === 'nobody') out = out.filter((t) => t.turn.who === 'nobody')
  if (p.restricted !== undefined) out = out.filter((t) => t.restricted === p.restricted)
  if (p.sort === 'status') {
    const order: Status[] = ['backlog', 'open', 'in-progress', 'waiting', 'testing', 'done']
    out = [...out].sort((a, b) => order.indexOf(a.status) - order.indexOf(b.status))
  }
  return out
}

async function listTickets(ws: string, p: ListTicketsParams = {}, route = 'tickets'): Promise<TicketSummary[]> {
  // Only `waiting` asked for: a derived state the host has no status for, so nothing matches.
  if (p.status && (Array.isArray(p.status) ? p.status : [p.status]).every((s) => s === 'waiting')) return []
  const rows: TicketSummary[] = []
  let page: string | undefined
  for (let i = 0; i < MAX_PAGES; i++) {
    const data = await call<ServerPage>(`${w(ws)}/${route}?${ticketQuery(p, page)}`)
    rows.push(...data.rows)
    if (!data.next_page) break
    page = data.next_page
  }
  return narrow(rows, p)
}

/** Workspaces in the UI's shape. What the shell never fills for a read-only host (gates, addons, counts) stays empty. */
function workspace(s: ServerWorkspace): Workspace {
  return {
    id: s.id,
    prefix: s.prefix,
    name: s.name,
    members: s.members,
    gates: {} as Workspace['gates'],
    addons: {},
    counts: {},
    needs_you: s.needs_you ?? 0,
    ...(s.host_state === 'ok' ? {} : { host_state: s.host_state }),
    ...(s.host_state !== 'ok' && s.host_message ? { host_message: s.host_message } : {}),
    icon: s.icon ?? null,
    icon_known: s.icon_known === true,
    // Offered only when the server lists the feature: the flag is on AND the signer can sign in this workspace.
    ...(s.runner !== undefined ? { runner: s.runner } : {}),
    ...(s.click ? { click: { ...s.click, available: s.click.available && s.features.includes('click') } } : {}),
  }
}

/** Every workspace the server knows, running or not (the switcher shows the others as "host not running"). */
const listWorkspaces = async () => {
  const rows = await call<ServerWorkspace[]>('/api/workspaces')
  refreshFeatures(rows)
  return rows.map(workspace)
}

export async function logout() {
  const result = await createFetchTransport('', getToken).request('POST', '/auth/logout')
  if (result.status >= 400 && result.status !== 401) throw new Error('Logout failed; please try again')
  clearSession()
}

/** The first read of every page load. It ends by itself after `SESSION_READ_MS` (an error the person can retry), and when the
 * query is cancelled (Try again): a request that can wait for good is how a reload ended on the boot skeleton. */
export const SESSION_READ_MS = 20_000
export const session = async (cancel?: AbortSignal) => {
  const stop = cancel ? AbortSignal.any([cancel, AbortSignal.timeout(SESSION_READ_MS)]) : AbortSignal.timeout(SESSION_READ_MS)
  try {
    return await call<{ features: string[]; workspaces: Record<string, string[]>; login?: string }>('/api/session', 'GET', undefined, stop)
  } catch (e) {
    if (e instanceof ApiError || cancel?.aborted) throw e
    throw new ApiError(0, { code: 'host.unavailable', message: 'It did not answer in time. Try again.', retryable: true })
  }
}

const reads = {
  /** One person has a different id in each workspace: with `ws` this is that workspace's own entry (person, name, role). */
  getMe: async (ws?: string): Promise<Me> => {
    const all = await call<ServerMe[]>('/api/me')
    if (ws) {
      const own = all.find((m) => m.workspace === ws)
      if (!own) throw new ApiError(404, { code: 'not_found', message: 'You are not a member of this workspace', retryable: false })
      return { person: own.person, name: own.name, role: own.role }
    }
    const first = all[0]
    if (!first) throw new ApiError(404, { code: 'not_found', message: 'No workspace is available', retryable: false })
    // The highest role across workspaces, as the mock's `Me` says.
    const role = all.reduce<Role>((best, m) => (ROLE_RANK[m.role] > ROLE_RANK[best] ? m.role : best), first.role)
    return { person: first.person, name: first.name, role }
  },
  getWorkspaces: listWorkspaces,
  getHostInfo: () => call<unknown>('/api/host'),
  listPeople: async (ws: string): Promise<KnownPerson[]> =>
    ((await listWorkspaces()).find((x) => x.id === ws)?.members ?? []).map((m) => ({ person: m.person, name: m.name, email: '' })),
  getToday: (ws: string) => call<TodayDocument>(`${w(ws)}/today`),
  listTickets: (ws: string, p?: ListTicketsParams) => listTickets(ws, p),
  listBoard: (ws: string) => listTickets(ws, {}, 'board'),
  getTicket: (key: string, ws?: string) =>
    ws ? call<TicketDocument>(`${w(ws)}/tickets/${encodeURIComponent(key)}`) : unsupported('Reading a ticket without its workspace'),
  getEvents: (key: string, since = 0, ws?: string) =>
    ws ? call<OrchEvent[]>(`${w(ws)}/tickets/${encodeURIComponent(key)}/events?since=${since}`) : unsupported('The log without its workspace'),
  getChanges: (key: string, ws?: string) =>
    ws ? call<TicketChanges>(`${w(ws)}/tickets/${encodeURIComponent(key)}/changes`) : unsupported('Changes without their workspace'),
  getIdentity: (ws: string) => call<WorkspaceIdentity>(`${w(ws)}/identity`),
  // Reduced workspace settings (M4): what the host gives this person, nothing more.
  getHostSettings: (ws: string) => call<HostSettings>(`${w(ws)}/settings`),
  getRoster: (ws: string) => call<RosterMember[]>(`${w(ws)}/members`),
  getAddOns: (ws: string) => call<import('./addOnSwitches').AddOnsData>(`${w(ws)}/add-ons`),
  getReaders: (ws: string) => call<HostReaders>(`${w(ws)}/readers`),
  listHostGrants: (ws: string, active = true) => call<HostGrant[]>(`${w(ws)}/grants?active=${active}`),
  // Quick tasks and records (M6). The host measures how big a quick task has grown.
  getQuick: (ws: string, p: QuickParams = {}) => {
    const q = new URLSearchParams()
    for (const s of p.status ?? []) q.append('status', s)
    q.set('limit', String(p.limit ?? 200))
    if (p.page) q.set('page', p.page)
    return call<QuickPage>(`${w(ws)}/quick?${q.toString()}`)
  },
  marketList: (ws: string) => call<MarketList>(`${w(ws)}/market`),
  marketInstalled: (ws: string) => call<MarketInstalled>(`${w(ws)}/market/installed`),
  marketAdd: (ws: string, url: string) => call<unknown>(`${w(ws)}/market/add`, 'POST', { url }),
  marketStage: (ws: string, input: StageInput) => call<StageResult>(`${w(ws)}/market/stage`, 'POST', input),
  marketUnstage: (ws: string, stage: string) => call<{ removed: boolean }>(`${w(ws)}/market/stage/${encodeURIComponent(stage)}`, 'DELETE'),
  marketUninstall: (ws: string, name: string) => call<{ name: string; removed: boolean }>(`${w(ws)}/market/uninstall`, 'POST', { name }),
  marketRefresh: (ws: string, input: { url?: string } = {}) => call<unknown>(`${w(ws)}/market/refresh`, 'POST', input),
  getActivity: (ws: string, p: ActivityParams = {}) => call<ActivityPage>(`${w(ws)}/activity?${activityQuery(p)}`),
  getRecordsCurrent: (ws: string) => call<RecordsStatus>(`${w(ws)}/records/current`),
  getRecords: (ws: string) => call<RecordsStatus>(`${w(ws)}/records`),
}

/**
 * The two things the dashboard may run for records (12.11): `commit [--push]` and `auto off`, each a fixed child of the
 * server. No signature; the host and the server decide who may. `auto-off` posts nothing at all.
 */
const records = {
  recordsCommit: (ws: string, push: boolean) => call<RecordsRun>(`${w(ws)}/records/commit`, 'POST', { push }),
  recordsAutoOff: (ws: string) => call<RecordsRun>(`${w(ws)}/records/auto-off`, 'POST'),
  /** #1198 C15: the admin clicks. The address is the one the review showed; the tool refuses if git now resolves to another. */
  recordsInit: (ws: string) => call<RecordsRun>(`${w(ws)}/records/init`, 'POST', {}),
  recordsMark: (ws: string, url: string | null) => call<RecordsRun>(`${w(ws)}/records/remote`, 'POST', url === null ? { on: false } : { on: true, url }),
  recordsPushInitial: (ws: string, url: string) => call<RecordsRun>(`${w(ws)}/records/push-initial`, 'POST', { url }),
}

/** Reads a page needs an answer to even when the host has nothing to say yet: an empty list, never a made-up one. */
const empty = {
  getAddons: async () => [],
  getWorkspaceAddons: async () => [],
  getAddonCatalog: async () => [],
  getAddonDecisions: async () => [],
  getAgents: async () => [],
  getAgentActivity: async () => [],
  listGrants: async () => [],
  getConnections: async () => [],
  getSkills: async () => [],
  getTicketRefusals: async () => [],
  listViews: async () => [],
  getDataset: async () => ({ dataset: 'normal' as const }),
  // Mandates are a mock-only preview: off, so no page waits for them.
  getLocalMandatesPreview: (): MandatesPreviewState => ({ preview: true, on: false, preflight: [], orchestrators: [], may: [], always_human: [], protected_paths: [], mandate: null }),
}

/**
 * The signed actions (M3): the server prepares a sign request and the person signs in a terminal. These three calls are
 * the whole write surface of the dashboard; none of them signs, and nothing here ever submits a signature.
 */
const handoff = {
  prepareHandoff: (ws: string, action: HandoffAction, retry = 0) => call<HandoffRequest>(`${w(ws)}/handoff`, 'POST', { ...action, ...(retry ? { retry } : {}) }),
  /** The host's look at a repository path before the form closes (a dry run: nothing is stored or signed). */
  checkRepo: (ws: string, action: HandoffAction) => call<{ ok: true }>(`${w(ws)}/repos/check`, 'POST', action),
  getHandoff: (ws: string, id: string) => call<HandoffState>(`${w(ws)}/handoff/${encodeURIComponent(id)}`),
  cancelHandoff: (ws: string, id: string) => call<{ id: string; state: string }>(`${w(ws)}/handoff/${encodeURIComponent(id)}`, 'DELETE'),
}

/**
 * Click-to-sign (H5c): prepare, look at the signer's View, sign, cancel. The server passes the View through unchanged;
 * the key is in core's signer process and never here.
 */
const click = {
  prepareClick: async (ws: string, action: ClickAction, retry = 0) => {
    await uploadFor(action as { images?: PastedRef[] }, (b) => call(`${w(ws)}/click/images`, 'POST', b)) // R3 part B
    return call<ClickPrepared>(`${w(ws)}/click/prepare`, 'POST', { ...action, ...(retry ? { retry } : {}) })
  },
  /** R3 part B: the bytes of a picture file of a ticket (the page hashes them before showing them). */
  getArtifactImage: (ws: string, key: string, name: string) =>
    call<{ image: ArtifactImage | null }>(`${w(ws)}/tickets/${encodeURIComponent(key)}/artifacts/${encodeURIComponent(name)}`),
  viewClick: (ws: string, requestId: string) => call<ClickViewAnswer>(`${w(ws)}/click/view`, 'POST', { request_id: requestId }),
  signClick: (ws: string, requestId: string, viewDigest: string, revealKey?: string, sealed?: Record<string, import('./sealed').Envelope>) =>
    call<ClickSigned>(`${w(ws)}/click/sign`, 'POST', { request_id: requestId, view_digest: viewDigest, ...(revealKey ? { reveal_key: revealKey } : {}), ...(sealed ? { sealed } : {}) }),
  stateClick: (ws: string, requestId: string) => call<ClickState>(`${w(ws)}/click/state`, 'POST', { request_id: requestId }),
  /** V3 C11: what the host says to check when the hub's channel signs (a read; nothing is prepared or signed here). */
  hubSign: (ws: string, body: import('./hubsign').HubSignRequest) => call<import('./hubsign').HubSignInfo>(`${w(ws)}/hubsign`, 'POST', body),
  getHubStatus: (ws: string) => call<import('./hub').HubStatus>(`${w(ws)}/hub`),
  getHubShare: (ws: string) => call<import('./hub').HubShare>(`${w(ws)}/hub/share`),
  getHubPreview: (ws: string, body: { add?: import('./hub').HubRule; remove?: import('./hub').HubRule }) =>
    call<import('./hub').HubPreview>(`${w(ws)}/hub/share/preview`, 'POST', body),
  getHubCommand: (ws: string, body: import('./hub').HubCommandRequest) => call<import('./hub').HubCommand>(`${w(ws)}/hub/command`, 'POST', body),
  connectHub: (ws: string, body: import('./hub').HubConnectRequest) => call<import('./hub').HubConnected>(`${w(ws)}/hub/connect`, 'POST', body),
  cancelClick: (ws: string, requestId: string) => call<{ id: string; state: string }>(`${w(ws)}/click/cancel`, 'POST', { request_id: requestId }),
  // How I approve (B6): the person's own default way to sign, and whether a phone is paired. Both were missing here, so
  // every real host looked as if it "does not provide personal approval preferences" (B11 10). An older server answers
  // 404 and the page then hides the controls.
  getSignDefault: (ws: string) => call<SignDefault>(`${w(ws)}/me/sign_default`),
  putSignDefault: (ws: string, body: { method: SignMethod; fallback?: SignMethod }) => call<SignDefault>(`${w(ws)}/me/sign_default`, 'PUT', body),
  phoneAvailable: (ws: string) => call<{ phones: number }>(`${w(ws)}/phone/available`),
  clickStatus: (ws: string) => call<ClickCard>(`${w(ws)}/click/status`),
  switchClick: (ws: string, body: ClickSwitch) => call<ClickCard>(`${w(ws)}/click/custody`, 'POST', body),
  custodySeal: (ws: string) => call<CustodySealOffer>(`${w(ws)}/click/custody/seal`, 'POST', {}),
  rotateSeal: (ws: string) => call<RotateBegin>(`${w(ws)}/click/rotate/seal`, 'POST', {}),
  rotateKey: (ws: string, body: { request_id: string; view_digest: string; sealed: Record<string, import('./sealed').Envelope> }) =>
    call<{ done: true } & ClickCard>(`${w(ws)}/click/rotate`, 'POST', body),
}

const implemented: Record<string, unknown> = { ...reads, ...empty, ...handoff, ...click, ...records }

/** Every other method of the mock's api refuses plainly: a control that reaches one is a bug, not a silent no-op. */
export const api = new Proxy(implemented, {
  get: (target, name) => (typeof name === 'symbol' || name === 'then' ? undefined : (target[name] ?? ((..._args: unknown[]) => unsupported(name)))),
}) as unknown as Api
