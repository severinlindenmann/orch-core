// The hub pages (V3 A7, orch-core #859). Mission Control explains and shows exactly what changes. Nothing here signs:
// the server relays the host's reads in a closed shape; a join, a mode switch and a leave are signed by click; stopping
// sharing (one ticket, or one rule) is a plain owner click (`HubReduced`). Values that come from the hub are labelled
// "from the hub, not signed" wherever they are shown.
import { queryOptions } from '@tanstack/react-query'
import { api } from '@/api/client'
import { hasFeature } from './features'

export type HubMode = 'standalone' | 'selective' | 'full' | 'unknown'

/** `GET /api/w/<ws>/hub`: the host's view of the hub (closed shape, see server hub.py `status_view`). */
export interface HubStatus {
  mode: HubMode
  joined: boolean
  hub_id: string | null
  member: boolean
  link: 'joining' | 'bound' | 'unbound' | 'switching' | null
  /** `false`: the host could not reach the hub. `null`: the host does not say (today's core): never guessed. */
  reachable: boolean | null
  /** Left the hub, but the hub has not been told yet. */
  not_told: boolean
  owed: number
  reseal_needed: boolean
  key_change_pending: boolean
  log_sync_stuck: boolean
  /** The version a hub admin asked this workspace to update to (HUB-WS-04); `null`: no request, or already there. */
  update_target?: string | null
  /** The host's own word that the hub is being removed (#1463): it serves no company content meanwhile. `null`: not closing. */
  closing?: { since_ms: number; ends_ms: number } | null
  /** From the hub, not signed: people per role in the pinned group snapshot. `null`: no snapshot yet. */
  groups: { read: number; edit: number; admin: number } | null
  snapshot_version: number | null
  /** The hub card the host pinned (public), as text: handed back by the one-click switch to full. */
  hub_card?: string | null
}

/** Who besides the owner may read the shared tickets on the hub: hub user ids and team names. */
export interface HubAudience { users: string[]; groups: string[] }

export type HubRule =
  | { kind: 'needs_you' | 'questions'; value: null }
  | { kind: 'type' | 'tag' | 'ticket'; value: string; audience?: HubAudience; description?: boolean }

/** `GET /api/w/<ws>/hub/shared`: tickets other workspaces of the hub shared with this owner. Read-only. */
export interface HubSharedTicket {
  workspace_id: string
  key: string
  title: string
  shared_by: string
  status: string
  type: string
  deadline: string | null
  description: string | null
}

/** `GET /api/w/<ws>/spaces`: the shared spaces this workspace is in. Name and kind are the hub's own display data
 * ("from the hub, not signed"): an empty name means the hub gave none the page may show. Read-only. */
export interface HubSpace { id: string; name: string; kind: string; members: number }
/** `GET /api/w/<ws>/spaces/<id>`: the item names of one space, written by other workspaces: data, never markup. */
export interface HubSpaceItems { id: string; items: { name: string; size: number }[]; refused: number; note: string }

/** `GET /api/w/<ws>/hub/share/audience`: the hub's people and teams to choose from. From the hub, not signed. */
export interface HubAudienceList {
  people: { user_id: string; name: string }[]
  teams: { group: string; members: number }[]
  note: string
}

export interface HubRuleRow {
  kind: HubRule['kind']
  value: string | null
  /** Items the rule matches now. */
  count: number
  describes: string | null
  /** People and teams besides you who can read what the rule shares (0: only you). */
  readers?: number
  /** The reader ids as core stores the rule (null: none, or not shown): Stop sharing sends the rule back exactly. */
  audience?: HubAudience | null
  /** false: the readers could not be read back, so the rule cannot be sent back exactly and Stop sharing is off. */
  removable?: boolean
  /** The ticket description is shared too. */
  description?: boolean
}

/** `GET /api/w/<ws>/hub/share`. */
export interface HubShare {
  /** `false`: this host has no share operations (an older core); the page says so. */
  available: boolean
  rules: HubRuleRow[]
  shared: number
  refused: { item: string; code: string; stale: boolean }[]
  /** Keys of the tickets the hub has; complete only when `tickets_complete`. */
  tickets: string[]
  tickets_complete: boolean
  tickets_state?: Record<string, string>
  not_sent_count?: number
}

/** `POST /api/w/<ws>/hub/share/preview`: what the hub would see after ONE rule change. */
export interface HubPreview {
  count: number
  new: number
  revoked: number
  more: number
  items: { kind: 'ticket' | 'question' | 'summary'; line: string }[]
  rules: string[]
  /** The digest of exactly this selection; a rule removal sends it back (`expect`) and the host refuses any other. */
  plan?: string | null
  /** The host's own review of an adding change (hub, who can read there, only the new items, what leaves). */
  review?: HubShareReview | null
}

/** From the host's `share.preview`: every word of the review of an adding change; the page adds none. */
export interface HubShareReview {
  hub_id: string | null
  /** Who can read the new items on the hub, by name (from the hub, not signed). */
  readers: { people: string[]; teams: string[]; note: string | null; info: string | null }
  /** Items already shared that this change makes reach more people or carry their description. */
  widened: { line: string; people_added: string[]; teams_added: string[]; description_added: boolean }[]
  new_count: number
  new_more: number
  new_items: string[]
  leaves: string[]
  later_edits: string | null
  /** Short forms for the one-ticket dialog (the host words them too). */
  leaves_short?: string | null
  later_edits_short?: string | null
  /** The hub mode this review was made in; in full mode `scope_short` says what the hub already holds. */
  mode?: 'selective' | 'full'
  scope_short?: string | null
}

/** `POST /hub/share/rule-add` and `/hub/share/sync`: sharing more. `synced: false`: saved, sent when the hub is back. */
export interface HubAdded { synced: boolean; pending: string | null; put: number }

/**
 * `POST /hub/share/revoke` and `POST /hub/share/rule-remove`: the two share changes that only take sharing away. One
 * click, no signature (like the workspace name and icon): owner only, from a full session, as the session's person.
 * `synced: false`: saved, the hub is told on the next sync (`pending` says why).
 */
export interface HubReduced { synced: boolean; pending: string | null; revoked: number }

/** `POST /hub/connect` (#1198 C11): tell the hub about a join just signed by click. `idem` is `a_` + a ULID-shaped id. */
export interface HubConnectRequest { idem: string; invite?: string; hub_card: string; addr?: string }
/** `POST /hub/link` (run-9 feedback 6): the host read the connect link (fetched the hub card, checked it against the
 * link's hub id). `hub_card` is the card as text, to hand back to the join; nothing is signed or kept by this read. */
export interface HubLinkInfo { hub_id: string; name: string; code: string; endpoint: string | null; owner_user?: string | null; dev: boolean; hub_card: string }
/** `confirmed`: the hub's first settings snapshot is pinned, so the add-ons are no longer paused. False: joined, the hub
 * has not answered yet; `POST /hub/refresh` asks again. */
export interface HubConnected { connected: true; mode: HubMode | null; confirmed: boolean }
export interface HubRefreshed { confirmed: boolean }

/** Does this host serve the hub reads (and so the pages)? The mock lists every feature. */
export const hubServed = (ws: string | undefined): boolean => hasFeature('hub', ws)

const joinTries = new Map<string, number>()
/** Called when the hub confirmed a join: lets the status query re-ask a few times while the host still says standalone. */
export const startJoinPolling = (ws: string): void => { joinTries.set(ws, 0) }
function joinPolls(ws: string, mode: HubMode | undefined): boolean {
  const n = joinTries.get(ws)
  if (n === undefined) return false
  if (mode !== 'standalone' || n >= 5) { joinTries.delete(ws); return false }
  joinTries.set(ws, n + 1)
  return true
}

export const hubQueries = {
  status: (ws: string) => queryOptions({ queryKey: ['hub', ws, 'status'], queryFn: () => api.getHubStatus(ws), staleTime: 15_000, retry: false,
    // Right after a confirmed join the host status can lag: ask again every 2 s, at most 5 times, while it still says standalone.
    refetchInterval: (q) => (joinPolls(ws, q.state.data?.mode) ? 2_000 : false) }),
  shared: (ws: string) => queryOptions({ queryKey: ['hub', ws, 'shared'], queryFn: () => api.getHubShared(ws), staleTime: 3_000, refetchInterval: 5_000, retry: false, refetchOnWindowFocus: true }),
  spaces: (ws: string) => queryOptions({ queryKey: ['hub', ws, 'spaces'], queryFn: () => api.getSpaces(ws), staleTime: 15_000, refetchInterval: 30_000, retry: false, refetchOnWindowFocus: true }),
  spaceItems: (ws: string, id: string) => queryOptions({ queryKey: ['hub', ws, 'spaces', id], queryFn: () => api.getSpaceItems(ws, id), staleTime: 5_000, refetchInterval: 15_000, retry: false }),
  audience: (ws: string) => queryOptions({ queryKey: ['hub', ws, 'audience'], queryFn: () => api.getHubAudience(ws), staleTime: 60_000, retry: false }),
  share: (ws: string) => queryOptions({ queryKey: ['hub', ws, 'share'], queryFn: () => api.getHubShare(ws), retry: false }),
}

/** Can this workspace approve through its hub? Only once the host says it is joined in full mode: a `hub` entry in the
 * signed sign_methods alone (One click setup writes it) is not a hub. Not known yet counts as no. */
export const hubApproves = (s: HubStatus | null | undefined): boolean => !!s && s.joined && s.mode === 'full'

/** What the top-bar chip says (A5.1). Grey is quiet (standalone, or online and connected), amber needs attention. */
export type ChipTone = 'neutral' | 'amber'
export interface HubChip {
  tone: ChipTone
  /** Full label; `short` is the two-word phone form. */
  label: string
  short: string
  icon: 'none' | 'dot' | 'alert'
}

export function hubChip(s: HubStatus | null | undefined): HubChip | null {
  if (!s) return null
  if (s.not_told) return { tone: 'amber', label: 'Left the hub, hub not told yet', short: 'Hub not told', icon: 'alert' }
  if (s.mode === 'standalone') return { tone: 'neutral', label: 'Local only', short: 'Local', icon: 'none' }
  if (s.reachable === false) return { tone: 'amber', label: 'Hub offline, working locally', short: 'Hub offline', icon: 'alert' }
  if (s.reseal_needed || s.key_change_pending || s.link === 'joining') return { tone: 'amber', label: 'Hub needs attention', short: 'Hub: attention', icon: 'alert' }
  // Run-11 feedback 17: a healthy hub is quiet. The access level lives in Settings -> Hub and in the tooltip, not in the chip.
  if (s.mode === 'selective' || s.mode === 'full') return { tone: 'neutral', label: 'Online · connected to hub', short: 'Online', icon: 'dot' }
  return { tone: 'amber', label: 'Hub mode not known', short: 'Hub: unknown', icon: 'alert' }
}

/**
 * The hub a pasted hub card names (`{"o": {"hub_id": ...}}`), for the join by click. `id` goes to the signer with the
 * card; `short` is the form the person compares with what the hub admin says. Only a look: the host verifies the card.
 */
export function hubCardId(text: string): { id: string; short: string } | null {
  try {
    const card: unknown = JSON.parse(text)
    const o = card && typeof card === 'object' ? (card as { o?: unknown }).o : null
    const id = o && typeof o === 'object' ? (o as { hub_id?: unknown }).hub_id : null
    if (typeof id !== 'string' || !/^[0-9a-f]{8,64}$/.test(id)) return null
    return { id, short: id.slice(0, 16).replace(/(.{4})(?=.)/g, '$1 ') }
  } catch {
    return null
  }
}

/**
 * The hub id an invite names (`<hub_id>:<code>`, core#1110), in the short form the person compares: the first 16 hex
 * digits in groups of four. It comes only from what the person typed or pasted, never from the hub card file, so the
 * comparison cannot be void. `null`: the invite names no hub id (shown as such; the terminal decides).
 */
export function inviteHubId(invite: string): string | null {
  const m = /^([0-9a-f]{16,64}):[A-Za-z0-9._-]+$/.exec(invite.trim())
  return m ? m[1].slice(0, 16).replace(/(.{4})(?=.)/g, '$1 ') : null
}
