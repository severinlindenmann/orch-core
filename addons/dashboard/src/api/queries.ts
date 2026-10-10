// One definition per query (G4 review I2): the key and the function that fills it. Pages use these with useQuery
// (adding `enabled` and their own options), and the route loaders (app/routeData.ts) warm the very same entries with
// ensureQueryData, so a page and its loader can never drift apart. Keys are unchanged from before.

import { queryOptions } from '@tanstack/react-query'
import { api, type ListTicketsParams } from './client'
import type { ArtifactQuery } from './types'

/** Query key of an addon's state; per-ticket requests sit under the addon's key, so invalidating it covers them. */
export function addonStateKey(ws: string | undefined, name: string, ticket?: string): unknown[] {
  return ticket ? ['addon-state', ws, name, { ticket }] : ['addon-state', ws, name]
}

export const queries = {
  me: () => queryOptions({ queryKey: ['me'], queryFn: api.getMe }),
  workspaces: () => queryOptions({ queryKey: ['workspaces'], queryFn: api.getWorkspaces }),
  addons: () => queryOptions({ queryKey: ['addons'], queryFn: api.getAddons, staleTime: 30_000 }),
  devDataset: () => queryOptions({ queryKey: ['dev-dataset'], queryFn: () => api.getDataset() }),
  today: (ws: string) => queryOptions({ queryKey: ['today', ws], queryFn: () => api.getToday(ws) }),
  agents: (ws: string) => queryOptions({ queryKey: ['agents', ws], queryFn: () => api.getAgents(ws) }),
  addonDecisions: (ws: string) => queryOptions({ queryKey: ['addon-decisions', ws], queryFn: () => api.getAddonDecisions(ws) }),
  grants: (ws: string) => queryOptions({ queryKey: ['grants', ws], queryFn: () => api.listGrants(ws) }),
  agentActivity: (ws: string) => queryOptions({ queryKey: ['agent-activity', ws], queryFn: () => api.getAgentActivity(ws) }),
  /** The board's own copy of the ticket list (moves update it optimistically). */
  board: (ws: string) => queryOptions({ queryKey: ['board', ws], queryFn: () => api.listTickets(ws) }),
  /** Every ticket of the workspace, unfiltered (options, counts, titles). */
  ticketsAll: (ws: string) => queryOptions({ queryKey: ['tickets', ws, 'all'], queryFn: () => api.listTickets(ws) }),
  /** The Tickets list for its server-side filters (see ticketsServerParams). */
  tickets: (ws: string, params: ListTicketsParams) => queryOptions({ queryKey: ['tickets', ws, params], queryFn: () => api.listTickets(ws, params) }),
  ticket: (key: string) => queryOptions({ queryKey: ['ticket', key], queryFn: () => api.getTicket(key), retry: false }),
  connections: (ws: string) => queryOptions({ queryKey: ['connections', ws], queryFn: () => api.getConnections(ws) }),
  skills: (ws: string) => queryOptions({ queryKey: ['skills', ws], queryFn: () => api.getSkills(ws) }),
  identity: (ws: string) => queryOptions({ queryKey: ['identity', ws], queryFn: () => api.getIdentity(ws) }),
  people: (ws: string) => queryOptions({ queryKey: ['people', ws], queryFn: () => api.listPeople(ws) }),
  /** The relay state with the time it was read (the page polls while something moves). */
  relay: (ws: string) => queryOptions({ queryKey: ['relay', ws], queryFn: async () => ({ state: await api.getRelay(ws), at: Date.now() }) }),
  workspaceAddons: (ws: string) => queryOptions({ queryKey: ['workspace-addons', ws], queryFn: () => api.getWorkspaceAddons(ws) }),
  /**
   * An addon's state; per-ticket requests sit under the addon's key, so invalidating it covers them. While the state
   * says `moving: true` (work the host runs in the background, e.g. a clone in progress) it is read again every second.
   */
  addonState: (ws: string, name: string, ticket?: string) =>
    queryOptions({ queryKey: addonStateKey(ws, name, ticket), queryFn: () => api.getAddonState(ws, name, ticket), staleTime: 10_000, refetchInterval: (q) => (q.state.data?.moving === true ? 1000 : false), retry: false }),
  /** Mandates, PREVIEW ONLY: the shell banner, Today's digest and Agents → Mandates read this one entry. */
  mandatesPreview: (ws: string) => queryOptions({ queryKey: ['mandates-preview', ws], queryFn: () => api.getMandatesPreview(ws), retry: false }),
  artifacts: (ws: string, query: ArtifactQuery) => queryOptions({ queryKey: ['artifacts', ws, query], queryFn: () => api.listArtifacts(ws, query) }),
}
