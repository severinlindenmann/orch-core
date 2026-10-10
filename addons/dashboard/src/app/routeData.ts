// Route loaders (G4): before the router shows a page it warms the data that page gates on, with the same query keys
// and query functions as the page's own useQuery calls. Loaders return nothing: the components keep reading the
// cache through useQuery, so there is one source of truth. The router keeps the old page on screen while a loader
// runs (up to `defaultPendingMs`), then shows the page's skeleton; a page whose data arrives within that time appears
// in one step.
//
// A loader never fails: a request that fails is left to the page, which shows its own error (with Retry). A real host
// replaces the mock transport only (api/client.ts); these loaders map one to one to its GET endpoints.

import type { QueryClient } from '@tanstack/react-query'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import { can, roleOf } from '@/api/permissions'
import type { AddonContribution, AddonPackage, AddonSlot, Workspace } from '@/api/types'
import { addonStateKey, bindsAddonState } from '@/addon-ui/slots'
import type { UrlState } from './urls'

export interface LoaderContext {
  queryClient?: QueryClient
  urls: UrlState
}

type Ensure = Promise<unknown>

const STORAGE_KEY = 'orch.workspace'
const storedWorkspace = (): string | null => {
  try {
    return localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

/** Waits for all, never rejects (each page shows its own error for a request that failed). */
const settle = (parts: Ensure[]) => Promise.allSettled(parts).then(() => undefined)

const me = (qc: QueryClient) => qc.ensureQueryData({ queryKey: ['me'], queryFn: api.getMe })
const workspaces = (qc: QueryClient) => qc.ensureQueryData({ queryKey: ['workspaces'], queryFn: api.getWorkspaces })
const addons = (qc: QueryClient) => qc.ensureQueryData({ queryKey: ['addons'], queryFn: api.getAddons, staleTime: 30_000 })

/** The workspace the page will show, as WorkspaceProvider picks it: the address's, else the remembered one, else the first. */
function pickWorkspace(list: Workspace[], urls: UrlState): Workspace | undefined {
  const p = urls.prefix?.toUpperCase()
  return (p ? list.find((w) => w.prefix.toUpperCase() === p) : undefined) ?? list.find((w) => w.id === storedWorkspace()) ?? list[0]
}

/** The shell's own data (sidebar, topbar, dock): read before the first paint so the frame does not move after it. */
export async function loadShell(ctx: LoaderContext): Promise<void> {
  const qc = ctx.queryClient
  if (!qc) return
  await settle([me(qc), workspaces(qc), addons(qc), qc.ensureQueryData({ queryKey: ['dev-dataset'], queryFn: () => api.getDataset() })])
}

/** The workspace-scoped parts of the shell (sidebar grant line, the dock's sessions, addon nav badges). */
function shellScoped(qc: QueryClient, ws: Workspace, pkgs: AddonPackage[]): Ensure[] {
  return [
    qc.ensureQueryData({ queryKey: ['grants', ws.id], queryFn: () => api.listGrants(ws.id) }),
    qc.ensureQueryData({ queryKey: ['today', ws.id], queryFn: () => api.getToday(ws.id) }),
    ...slotStates(qc, ws, pkgs, 'nav'),
  ]
}

/** Addon states a slot reads (as useSlot asks for them). */
function slotStates(qc: QueryClient, ws: Workspace, pkgs: AddonPackage[], slot: AddonSlot): Ensure[] {
  return pkgs
    .filter((a) => addonActive(ws, a.name) && (a.contributions as AddonContribution[]).some((c) => c.slot === slot && bindsAddonState(c)))
    .map((a) => addonState(qc, ws.id, a.name))
}

const addonState = (qc: QueryClient, ws: string, name: string) =>
  qc.ensureQueryData({ queryKey: addonStateKey(ws, name), queryFn: () => api.getAddonState(ws, name), staleTime: 10_000 })

/** What a page loader gets: the query client, the workspace it will show and the viewer's role there. */
interface PageScope {
  qc: QueryClient
  ws: Workspace
  pkgs: AddonPackage[]
  owner: boolean
}

/** Builds a page loader: shell data first (one round trip), then the page's own queries in parallel. */
export function pageLoader(parts: (s: PageScope) => Ensure[], chunks: (() => Promise<unknown>)[] = []) {
  return async ({ context }: { context: LoaderContext }): Promise<void> => {
    const code = Promise.all(chunks.map((c) => c()))
    const qc = context.queryClient
    if (!qc) {
      await code
      return
    }
    const [person, list, pkgs] = await Promise.all([me(qc).catch(() => undefined), workspaces(qc).catch(() => []), addons(qc).catch(() => [])])
    const ws = pickWorkspace(list, context.urls)
    if (!ws) {
      await code
      return
    }
    const owner = can(roleOf(ws, person?.person), 'settings')
    const scope = { qc, ws, pkgs, owner }
    await Promise.all([code, settle([...shellScoped(qc, ws, pkgs), ...parts(scope)])])
  }
}

// ---- per page -------------------------------------------------------------------------------------------------

export const todayData = ({ qc, ws, pkgs, owner }: PageScope): Ensure[] => [
  qc.ensureQueryData({ queryKey: ['agents', ws.id], queryFn: () => api.getAgents(ws.id) }),
  qc.ensureQueryData({ queryKey: ['addon-decisions', ws.id], queryFn: () => api.getAddonDecisions(ws.id) }),
  qc.ensureQueryData({ queryKey: ['dev-dataset'], queryFn: () => api.getDataset() }),
  ...(owner ? [qc.ensureQueryData({ queryKey: ['connections', ws.id], queryFn: () => api.getConnections(ws.id) })] : []),
  ...slotStates(qc, ws, pkgs, 'today.card'),
]

export const boardData = ({ qc, ws, pkgs }: PageScope): Ensure[] => [
  qc.ensureQueryData({ queryKey: ['board', ws.id], queryFn: () => api.listTickets(ws.id) }),
  qc.ensureQueryData({ queryKey: ['agents', ws.id], queryFn: () => api.getAgents(ws.id) }),
  ...slotStates(qc, ws, pkgs, 'board.lane'),
  ...slotStates(qc, ws, pkgs, 'board.card_field'),
]

export const ticketsData = ({ qc, ws }: PageScope): Ensure[] => [
  qc.ensureQueryData({ queryKey: ['tickets', ws.id, 'all'], queryFn: () => api.listTickets(ws.id) }),
]

export const agentsData = ({ qc, ws, owner }: PageScope): Ensure[] => [
  qc.ensureQueryData({ queryKey: ['agents', ws.id], queryFn: () => api.getAgents(ws.id) }),
  qc.ensureQueryData({ queryKey: ['tickets', ws.id, 'all'], queryFn: () => api.listTickets(ws.id) }),
  qc.ensureQueryData({ queryKey: ['agent-activity', ws.id], queryFn: () => api.getAgentActivity(ws.id) }),
  qc.ensureQueryData({ queryKey: ['addon-decisions', ws.id], queryFn: () => api.getAddonDecisions(ws.id) }),
  ...(owner ? [qc.ensureQueryData({ queryKey: ['connections', ws.id], queryFn: () => api.getConnections(ws.id) })] : []),
]

export const settingsData =
  (tab: string, addon?: string) =>
  ({ qc, ws }: PageScope): Ensure[] => {
    const id = ws.id
    switch (addon ? 'addons' : tab) {
      case 'general':
        return [qc.ensureQueryData({ queryKey: ['identity', id], queryFn: () => api.getIdentity(id) })]
      case 'members':
        return [qc.ensureQueryData({ queryKey: ['people', id], queryFn: () => api.listPeople(id) })]
      case 'gates':
        return [qc.ensureQueryData({ queryKey: ['tickets', id, 'all'], queryFn: () => api.listTickets(id) })]
      case 'relay':
        return [qc.ensureQueryData({ queryKey: ['relay', id], queryFn: async () => ({ state: await api.getRelay(id), at: Date.now() }) })]
      case 'addons':
        return [
          qc.ensureQueryData({ queryKey: ['workspace-addons', id], queryFn: () => api.getWorkspaceAddons(id) }),
          ...(addon ? [addonState(qc, id, addon)] : []),
        ]
      case 'skills':
        return [
          qc.ensureQueryData({ queryKey: ['skills', id], queryFn: () => api.getSkills(id) }),
          qc.ensureQueryData({ queryKey: ['connections', id], queryFn: () => api.getConnections(id) }),
        ]
      case 'connections':
        return [
          qc.ensureQueryData({ queryKey: ['connections', id], queryFn: () => api.getConnections(id) }),
          qc.ensureQueryData({ queryKey: ['skills', id], queryFn: () => api.getSkills(id) }),
        ]
      default:
        return []
    }
  }

export const addonPageData =
  (name: string) =>
  ({ qc, ws }: PageScope): Ensure[] =>
    addonActive(ws, name) ? [addonState(qc, ws.id, name)] : []

/** A ticket page: the ticket itself (its key names its workspace, so it does not wait for the page's workspace). */
export const ticketData =
  (key: string) =>
  ({ qc }: PageScope): Ensure[] => [qc.ensureQueryData({ queryKey: ['ticket', key], queryFn: () => api.getTicket(key), retry: false })]
