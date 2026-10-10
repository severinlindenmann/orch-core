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
import { can, roleOf } from '@/api/permissions'
import type { AddonContribution, AddonPackage, AddonSlot, Workspace } from '@/api/types'
import { bindsAddonState } from '@/addon-ui/slots'
import { ticketsServerParams, type TicketsSearch } from './pages/tickets/search'
import type { UrlState } from './urls'
import { queries } from '@/api/queries'

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

/**
 * The longest a loader holds a page for its data. A request slower than this (a host under load, an addon that does
 * not answer) no longer keeps the page away: it shows with its own placeholders for what is still missing.
 */
export const LOADER_WAIT_MS = 1500
const atMost = (p: Promise<unknown>, ms = LOADER_WAIT_MS) => Promise.race([p, new Promise((res) => setTimeout(res, ms))]).then(() => undefined)

const me = (qc: QueryClient) => qc.ensureQueryData(queries.me())
const workspaces = (qc: QueryClient) => qc.ensureQueryData(queries.workspaces())
const addons = (qc: QueryClient) => qc.ensureQueryData(queries.addons())

/** The workspace the page will show, as WorkspaceProvider picks it: the address's, else the remembered one, else the first. */
function pickWorkspace(list: Workspace[], urls: UrlState): Workspace | undefined {
  const p = urls.prefix?.toUpperCase()
  return (p ? list.find((w) => w.prefix.toUpperCase() === p) : undefined) ?? list.find((w) => w.id === storedWorkspace()) ?? list[0]
}

/** The shell's own data (sidebar, topbar, dock): read before the first paint so the frame does not move after it. */
export async function loadShell(ctx: LoaderContext): Promise<void> {
  const qc = ctx.queryClient
  if (!qc) return
  await atMost(settle([me(qc), workspaces(qc), addons(qc), qc.ensureQueryData(queries.devDataset())]))
}

/** The workspace-scoped parts of the shell (sidebar grant line, the dock's sessions, addon nav badges). */
function shellScoped(qc: QueryClient, ws: Workspace, pkgs: AddonPackage[]): Ensure[] {
  return [
    qc.ensureQueryData(queries.grants(ws.id)),
    qc.ensureQueryData(queries.today(ws.id)),
    ...slotStates(qc, ws, pkgs, 'nav'),
  ]
}

/** Addon states a slot reads (as useSlot asks for them). */
function slotStates(qc: QueryClient, ws: Workspace, pkgs: AddonPackage[], slot: AddonSlot): Ensure[] {
  return pkgs
    .filter((a) => addonActive(ws, a.name) && (a.contributions as AddonContribution[]).some((c) => c.slot === slot && bindsAddonState(c)))
    .map((a) => addonState(qc, ws.id, a.name))
}

const addonState = (qc: QueryClient, ws: string, name: string, ticket?: string) => qc.ensureQueryData(queries.addonState(ws, name, ticket))

/** What a page loader gets: the query client, the workspace it will show and the viewer's role there. */
interface PageScope {
  qc: QueryClient
  ws: Workspace
  /** All of the viewer's workspaces (a ticket's key names its own). */
  list: Workspace[]
  pkgs: AddonPackage[]
  person?: string
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
    const data = async () => {
      const [person, list, pkgs] = await Promise.all([me(qc).catch(() => undefined), workspaces(qc).catch(() => []), addons(qc).catch(() => [])])
      const ws = pickWorkspace(list, context.urls)
      if (!ws) return
      const owner = can(roleOf(ws, person?.person), 'settings')
      await settle([...shellScoped(qc, ws, pkgs), ...parts({ qc, ws, list, pkgs, person: person?.person, owner })])
    }
    await Promise.all([code, atMost(data())])
  }
}

// ---- per page -------------------------------------------------------------------------------------------------

/** The tickets Today's rows open into (the first row is open from the start): one more round trip, in parallel. */
async function todayTickets(qc: QueryClient, ws: string, canAct: boolean): Promise<unknown> {
  const [today, decisions, agents] = await Promise.all([
    qc.ensureQueryData(queries.today(ws)),
    qc.ensureQueryData(queries.addonDecisions(ws)),
    qc.ensureQueryData(queries.agents(ws)),
  ])
  const keys = new Set([
    // The page shows one of the two lists, by role (TodayPage).
    ...(canAct ? today.needs_you : today.read_only_open).map((i) => i.ticket),
    ...decisions.flatMap((d) => (d.ticket ? [d.ticket] : [])),
    ...agents.flatMap((a) => a.claims.map((c) => c.ticket)),
  ])
  return settle([...keys].map((k) => qc.ensureQueryData(queries.ticket(k))))
}

export const todayData = ({ qc, ws, pkgs, person, owner }: PageScope): Ensure[] => [
  todayTickets(qc, ws.id, can(roleOf(ws, person), 'ticket.act')),
  qc.ensureQueryData(queries.devDataset()),
  ...(owner ? [qc.ensureQueryData(queries.connections(ws.id))] : []),
  ...slotStates(qc, ws, pkgs, 'today.card'),
]

export const boardData = ({ qc, ws, pkgs }: PageScope): Ensure[] => [
  qc.ensureQueryData(queries.board(ws.id)),
  qc.ensureQueryData(queries.agents(ws.id)),
  ...slotStates(qc, ws, pkgs, 'board.lane'),
  ...slotStates(qc, ws, pkgs, 'board.card_field'),
]

/** The Tickets list: the unfiltered list (filter options, counts) and the list for the address's filters. */
export const ticketsData =
  (search: TicketsSearch) =>
  ({ qc, ws }: PageScope): Ensure[] => {
    return [qc.ensureQueryData(queries.ticketsAll(ws.id)), qc.ensureQueryData(queries.tickets(ws.id, ticketsServerParams(search)))]
  }

export const agentsData = ({ qc, ws, owner }: PageScope): Ensure[] => [
  qc.ensureQueryData(queries.agents(ws.id)),
  qc.ensureQueryData(queries.ticketsAll(ws.id)),
  qc.ensureQueryData(queries.agentActivity(ws.id)),
  qc.ensureQueryData(queries.addonDecisions(ws.id)),
  ...(owner ? [qc.ensureQueryData(queries.connections(ws.id))] : []),
]

export const settingsData =
  (tab: string, addon?: string) =>
  ({ qc, ws }: PageScope): Ensure[] => {
    const id = ws.id
    switch (addon ? 'addons' : tab) {
      case 'general':
        return [qc.ensureQueryData(queries.identity(id))]
      case 'members':
        return [qc.ensureQueryData(queries.people(id))]
      case 'gates':
        return [qc.ensureQueryData(queries.ticketsAll(id))]
      case 'relay':
        return [qc.ensureQueryData(queries.relay(id))]
      case 'addons':
        return [
          qc.ensureQueryData(queries.workspaceAddons(id)),
          ...(addon ? [addonState(qc, id, addon)] : []),
        ]
      case 'skills':
        return [
          qc.ensureQueryData(queries.skills(id)),
          qc.ensureQueryData(queries.connections(id)),
        ]
      case 'connections':
        return [
          qc.ensureQueryData(queries.connections(id)),
          qc.ensureQueryData(queries.skills(id)),
        ]
      default:
        return []
    }
  }

export const addonPageData =
  (name: string) =>
  ({ qc, ws }: PageScope): Ensure[] =>
    addonActive(ws, name) ? [addonState(qc, ws.id, name)] : []

/** The workspace a ticket key names (`OPS-0001` → OPS), as the ticket page makes it current. */
export function workspaceOfKey(key: string, list: Workspace[]): Workspace | undefined {
  const k = key.toUpperCase()
  return list.find((w) => k.startsWith(`${w.prefix.toUpperCase()}-`))
}

/** A ticket page: the ticket itself and its addon panels, in the ticket's own workspace. */
export const ticketData =
  (key: string) =>
  ({ qc, ws: shown, list, pkgs }: PageScope): Ensure[] => {
    const ws = workspaceOfKey(key, list) ?? shown
    return [
      qc.ensureQueryData(queries.ticket(key)),
      // The ticket's addon panels (their count shows in the header's Panels button).
      ...pkgs
        .filter((a) => addonActive(ws, a.name) && (a.contributions as AddonContribution[]).some((c) => c.slot === 'ticket.panel' && bindsAddonState(c)))
        .map((a) => addonState(qc, ws.id, a.name, key)),
    ]
  }

/** Artifacts: the first page of the unfiltered list, the query the page starts with (G3's `{ ...filters, page }`). */
export const artifactsData = ({ qc, ws }: PageScope): Ensure[] => [qc.ensureQueryData(queries.artifacts(ws.id, { page: 1 }))]
