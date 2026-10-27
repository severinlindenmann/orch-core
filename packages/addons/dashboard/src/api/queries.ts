// One definition per query (G4 review I2): the key and the function that fills it. Pages use these with useQuery
// (adding `enabled` and their own options), and the route loaders (app/routeData.ts) warm the very same entries with
// ensureQueryData, so a page and its loader can never drift apart. Keys are unchanged from before.

import { withoutSigned } from './signedDrop'
import { infiniteQueryOptions, queryOptions } from '@tanstack/react-query'
import type { ActivityParams } from './activity'
import { api, type ListTicketsParams } from '@/api/client'
import type { ArtifactQuery } from './types'
import { running } from './workspaces'
import { hostInfoOf } from './hostInfo'
import type { HostGrant } from './settings'

/** Query key of an addon's state; per-ticket requests sit under the addon's key, so invalidating it covers them. */
/** Identity and the workspace list cost about ten host reads each (me, workspaces) and change only through events the live
 * stream (or its poll fallback) turns into an invalidation, which ignores staleTime. Page changes must not re-read them. */
const LIVE_STALE = 60_000

export function addonStateKey(ws: string | undefined, name: string, ticket?: string): unknown[] {
  return ticket ? ['addon-state', ws, name, { ticket }] : ['addon-state', ws, name]
}

export const queries = {
  marketList: (ws: string) => queryOptions({ queryKey: ['market', ws], queryFn: () => api.marketList(ws), retry: false }),
  marketInstalled: (ws: string) => queryOptions({ queryKey: ['market-installed', ws], queryFn: () => api.marketInstalled(ws), retry: false }),
  /** Who the viewer is in `ws` (a person has a different id, name and role in each workspace); without `ws`, the first. */
  me: (ws?: string) => queryOptions({ queryKey: ws ? ['me', ws] : ['me'], queryFn: () => api.getMe(ws), staleTime: LIVE_STALE }),
  /** The workspaces whose host answers. One cache entry: `workspacesAll` reads the same one and keeps the others. */
  workspaces: () => queryOptions({ queryKey: ['workspaces'], queryFn: api.getWorkspaces, select: running, staleTime: LIVE_STALE }),
  workspacesAll: () => queryOptions({ queryKey: ['workspaces'], queryFn: api.getWorkspaces }),
  /** Core D1 (#582): is this host a dev sandbox host, and which alarms does it raise. Checked by `hostInfoOf`. */
  hostInfo: () => queryOptions({ queryKey: ['host-info'], queryFn: async () => hostInfoOf(await api.getHostInfo()), staleTime: 30_000, retry: false, refetchInterval: (q) => (q.state.status === 'error' ? 15_000 : false) }),
  addons: () => queryOptions({ queryKey: ['addons'], queryFn: api.getAddons, staleTime: 30_000 }),
  devDataset: () => queryOptions({ queryKey: ['dev-dataset'], queryFn: () => api.getDataset() }),
  today: (ws: string) => queryOptions({ queryKey: ['today', ws], queryFn: async () => withoutSigned(ws, await api.getToday(ws)) }),
  agents: (ws: string) => queryOptions({ queryKey: ['agents', ws], queryFn: () => api.getAgents(ws) }),
  addonDecisions: (ws: string) => queryOptions({ queryKey: ['addon-decisions', ws], queryFn: () => api.getAddonDecisions(ws) }),
  grants: (ws: string) => queryOptions({ queryKey: ['grants', ws], queryFn: () => api.listGrants(ws) }),
  agentActivity: (ws: string) => queryOptions({ queryKey: ['agent-activity', ws], queryFn: () => api.getAgentActivity(ws) }),
  /** The board's own copy of the ticket list (moves update it optimistically). */
  board: (ws: string) => queryOptions({ queryKey: ['board', ws], queryFn: () => api.listBoard(ws) }),
  /** Every ticket of the workspace, unfiltered (options, counts, titles). */
  ticketsAll: (ws: string) => queryOptions({ queryKey: ['tickets', ws, 'all'], queryFn: () => api.listTickets(ws), staleTime: 30_000 }),
  /** The Tickets list for its server-side filters (see ticketsServerParams). */
  // Without a filter this is the very same read as ticketsAll: one cache entry, one request (the Tickets page asks for both).
  tickets: (ws: string, params: ListTicketsParams) =>
    queryOptions({
      queryKey: ['tickets', ws, Object.values(params).every((v) => v === undefined) ? 'all' : params] as (string | ListTicketsParams)[],
      queryFn: () => api.listTickets(ws, params),
      staleTime: 30_000,
    }),
  /** Closed tickets (host v1.4 leaves them out unless asked): the Tickets list's Closed filter. */
  ticketsClosed: (ws: string, params: ListTicketsParams) =>
    queryOptions({ queryKey: ['tickets', ws, 'closed', params], queryFn: () => api.listTickets(ws, { ...params, status: ['closed'] }) }),
  /** One ticket, keyed by its workspace (the id a real host knows it by) and its key. */
  ticket: (ws: string, key: string) => queryOptions({ queryKey: ['ticket', ws, key], queryFn: () => api.getTicket(key, ws), retry: false }),
  connections: (ws: string) => queryOptions({ queryKey: ['connections', ws], queryFn: () => api.getConnections(ws) }),
  skills: (ws: string) => queryOptions({ queryKey: ['skills', ws], queryFn: () => api.getSkills(ws) }),
  /** Reduced workspace settings of a real host (M4). */
  signDefault: (ws: string) => queryOptions({ queryKey: ['sign-default', ws], queryFn: () => api.getSignDefault(ws), retry: false }),
  addOns: (ws: string) => queryOptions({ queryKey: ['add-ons', ws], queryFn: () => api.getAddOns(ws) }),
  hostSettings: (ws: string) => queryOptions({ queryKey: ['settings', ws], queryFn: () => api.getHostSettings(ws), staleTime: 60_000 }),
  roster: (ws: string) => queryOptions({ queryKey: ['members', ws], queryFn: () => api.getRoster(ws) }),
  /** H12d-2: reader levels and `required_reader` (Settings → Devices). */
  readers: (ws: string) => queryOptions({ queryKey: ['readers', ws], queryFn: () => api.getReaders(ws) }),
  /**
   * Grants as the host gives this person (an owner: all; anyone else: their own). A non-owner is not told when someone
   * else's grant changes (the live stream does not carry others' `grant.*` items), so the list is read again on focus
   * and every 30 seconds.
   */
  hostGrants: (ws: string, active: boolean) =>
    queryOptions<HostGrant[]>({ queryKey: ['settings-grants', ws, active], queryFn: () => api.listHostGrants(ws, active), refetchInterval: 30_000, refetchOnWindowFocus: true }),
  /** Quick tasks of a real host (M6): one group of statuses; the host measures size (no limits here). */
  quick: (ws: string, statuses: string[]) =>
    queryOptions({
      queryKey: ['quick', ws, statuses.join(',')],
      queryFn: () => api.getQuick(ws, { status: statuses }),
    }),
  /** The Activity page of a real host: pages of the timeline, newest first; follows `next_page`. */
  activity: (ws: string, filters: Omit<ActivityParams, 'page' | 'limit'>) =>
    infiniteQueryOptions({
      queryKey: ['activity', ws, filters.group ?? 'all', filters.who ?? '', filters.ticket ?? ''],
      queryFn: ({ pageParam }) => api.getActivity(ws, { ...filters, ...(pageParam ? { page: pageParam } : {}) }),
      initialPageParam: '',
      getNextPageParam: (last) => last.next_page || undefined,
      refetchInterval: 30_000,
      retry: false,
    }),
  /** Records status (owners, maintainers: the host refuses anyone else). Refused answers are not retried. */
  records: (ws: string) => queryOptions({ queryKey: ['records', ws], queryFn: () => api.getRecords(ws), retry: false }),
  identity: (ws: string) => queryOptions({ queryKey: ['identity', ws], queryFn: () => api.getIdentity(ws) }),
  people: (ws: string) => queryOptions({ queryKey: ['people', ws], queryFn: () => api.listPeople(ws) }),
  /** The relay state with the time it was read (the page polls while something moves). */
  relay: (ws: string) => queryOptions({ queryKey: ['relay', ws], queryFn: async () => ({ state: await api.getRelay(ws), at: Date.now() }) }),
  workspaceAddons: (ws: string) => queryOptions({ queryKey: ['workspace-addons', ws], queryFn: () => api.getWorkspaceAddons(ws) }),
  /**
   * An addon's state; per-ticket requests sit under the addon's key, so invalidating it covers them. While the state
   * says `moving: true` (work the host runs in the background, e.g. a clone in progress) it is read again every second;
   * otherwise after `nextRefreshMs` when the state asks for it (a scheduled check), bounded to 30 s – 1 h.
   */
  addonState: (ws: string, name: string, ticket?: string) =>
    queryOptions({ queryKey: addonStateKey(ws, name, ticket), queryFn: () => api.getAddonState(ws, name, ticket), staleTime: 10_000, refetchInterval: (q) => addonRefresh(q.state.data), retry: false }),
  /** Mandates, PREVIEW ONLY: the shell banner, Today's digest and Agents → Mandates read this one entry. */
  mandatesPreview: (ws: string) => queryOptions({ queryKey: ['mandates-preview', ws], queryFn: () => api.getMandatesPreview(ws), enabled: !!api.getLocalMandatesPreview(ws)?.on, initialData: () => api.getLocalMandatesPreview(ws), retry: false }),
  artifacts: (ws: string, query: ArtifactQuery) => queryOptions({ queryKey: ['artifacts', ws, query], queryFn: () => api.listArtifacts(ws, query) }),
}

/** How soon the shared addon-state query reads again: the generic `moving` / `nextRefreshMs` contract (no addon names). */
export function addonRefresh(data: Record<string, unknown> | undefined): number | false {
  if (data?.moving === true) return 1000
  const next = data?.nextRefreshMs
  return typeof next === 'number' && Number.isFinite(next) ? Math.min(3_600_000, Math.max(30_000, next)) : false
}
