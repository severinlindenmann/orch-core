// The hub pages (V3 A7, orch-core #859). Mission Control explains and shows exactly what changes; the owner signs in the
// Terminal. Nothing here signs, joins or leaves: the server relays the host's reads in a closed shape and builds the
// Terminal command for a change (`POST /hub/command`). Values that come from the hub are labelled "from the hub, not
// signed" wherever they are shown.
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
  /** From the hub, not signed: people per role in the pinned group snapshot. `null`: no snapshot yet. */
  groups: { read: number; edit: number; admin: number } | null
  snapshot_version: number | null
}

export type HubRule =
  | { kind: 'needs_you' | 'questions'; value: null }
  | { kind: 'type' | 'tag' | 'ticket'; value: string }

export interface HubRuleRow {
  kind: HubRule['kind']
  value: string | null
  /** Items the rule matches now. */
  count: number
  describes: string | null
}

/** `GET /api/w/<ws>/hub/share`. */
export interface HubShare {
  rules: HubRuleRow[]
  shared: number
  refused: { item: string; code: string; stale: boolean }[]
  /** Keys of the tickets the hub has; complete only when `tickets_complete`. */
  tickets: string[]
  tickets_complete: boolean
}

/** `POST /api/w/<ws>/hub/share/preview`: what the hub would see after ONE rule change. */
export interface HubPreview {
  count: number
  new: number
  revoked: number
  more: number
  items: { kind: 'ticket' | 'question' | 'summary'; line: string }[]
  rules: string[]
}

export type HubCommandRequest =
  | { kind: 'join'; invite: string; card: string; mode: 'selective' | 'full' }
  | { kind: 'mode_full'; card: string }
  | { kind: 'leave' | 'leave_hub_down' | 'stop_telling' | 'share_sync' }
  | { kind: 'share_add' | 'share_remove'; rule: HubRule }
  | { kind: 'share_revoke'; ref: string }

export interface HubCommand {
  command: string
  kind: HubCommandRequest['kind']
}

/** Does this host serve the hub reads (and so the pages)? The mock lists every feature. */
export const hubServed = (ws: string | undefined): boolean => hasFeature('hub', ws)

export const hubQueries = {
  status: (ws: string) => queryOptions({ queryKey: ['hub', ws, 'status'], queryFn: () => api.getHubStatus(ws), staleTime: 15_000, retry: false }),
  share: (ws: string) => queryOptions({ queryKey: ['hub', ws, 'share'], queryFn: () => api.getHubShare(ws), retry: false }),
}

/** What the top-bar chip says (A5.1). Violet is a hub, amber needs attention, grey is standalone. */
export type ChipTone = 'neutral' | 'violet' | 'violet-solid' | 'amber'
export interface HubChip {
  tone: ChipTone
  /** Full label; `short` is the two-word phone form. */
  label: string
  short: string
  icon: 'none' | 'eye-off' | 'eye' | 'alert'
}

export function hubChip(s: HubStatus | null | undefined): HubChip | null {
  if (!s) return null
  if (s.not_told) return { tone: 'amber', label: 'Left the hub, hub not told yet', short: 'Hub not told', icon: 'alert' }
  if (s.mode === 'standalone') return { tone: 'neutral', label: 'Local only', short: 'Local', icon: 'none' }
  if (s.reachable === false) return { tone: 'amber', label: 'Hub offline, working locally', short: 'Hub offline', icon: 'alert' }
  if (s.reseal_needed || s.key_change_pending || s.link === 'joining') return { tone: 'amber', label: 'Hub needs attention', short: 'Hub: attention', icon: 'alert' }
  if (s.mode === 'selective') return { tone: 'violet', label: 'Hub sees only what you share', short: 'Shares some', icon: 'eye-off' }
  if (s.mode === 'full') return { tone: 'violet-solid', label: 'Hub can read everything', short: 'Hub reads all', icon: 'eye' }
  return { tone: 'amber', label: 'Hub mode not known', short: 'Hub: unknown', icon: 'alert' }
}

/** The words the owner must type to start the switch to full mode (A3.2). */
export const FULL_ACCESS_WORDS = 'full access'

/**
 * The hub id an invite names (`<hub_id>:<code>`, core#1110), in the short form the person compares: the first 16 hex
 * digits in groups of four. It comes only from what the person typed or pasted, never from the hub card file, so the
 * comparison cannot be void. `null`: the invite names no hub id (shown as such; the Terminal decides).
 */
export function inviteHubId(invite: string): string | null {
  const m = /^([0-9a-f]{16,64}):[A-Za-z0-9._-]+$/.exec(invite.trim())
  return m ? m[1].slice(0, 16).replace(/(.{4})(?=.)/g, '$1 ') : null
}
