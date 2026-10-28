// Hub signing from Mission Control (V3 C11, D78/D81). In a workspace joined to a hub in full mode this page signs
// nothing: it shows what the host says to check ("Approve on your phone or in the hub"), the 12 characters to compare
// and one link the HOST built. Nothing here builds a link, holds a hub credential or falls back to a local key.
import type { HandoffAction } from './handoff'

export type SigningMode = 'standalone' | 'selective' | 'full' | 'unknown'

export type HubSignRequest =
  | { kind: 'gate' | 'verdict'; ticket: string; gate: 'requirements' | 'plan' | 'code' | 'verify' }
  | { kind: 'question'; ticket: string; question: string }
  | { kind: 'status'; ticket: string }
  /** FB15: turn an add-on on through the hub; `request` is the prepared hand-off (op addon.grant) the link is for. */
  | { kind: 'addon'; request: string }
  /** A settings change the hub's channel may make (grant_hours, claim_ttl_min, lease_ttl_min); nothing is prepared at the host. */
  | { kind: 'settings'; set: Record<string, number> }

export interface HubSignInfo {
  mode: 'full' | 'unknown'
  kind?: 'addon' | 'settings'
  summary: { kind: string; set?: Record<string, number>; ticket: string; gate: string | null; gate_gen: number | null; question: string | null; status: string; title: string; name?: string; version?: string; capabilities?: string[] }
  /** Twelve lower-case hex digits in groups of four; null for a status move (no hash to compare). */
  short_hash: string | null
  /** Built by the host from its pinned hub address; the page only checks it and never makes one. */
  url: string
  /** Add-on only: the same page by the hub's address for other devices, when `url` is the form for this computer. */
  other_url?: string
}

/** FB15: an add-on grant prepared for the hub (`POST /hub/approve/prepare`): the request and what the hub panel shows. */
export interface HubApproval extends HubSignInfo {
  id: string
  state: string
  expires_at: string
}

/** Where that request is now (`GET /hub/approve/<id>`). */
export interface HubApprovalState {
  id: string
  state: 'prepared' | 'bound' | 'submitted' | 'cancelled' | 'expired'
  expires_at: string
  error: { code: string } | null
}

const SHORT = /^[0-9a-f]{4} [0-9a-f]{4} [0-9a-f]{4}$/
const URL_OK = /^https:\/\/(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:.]+\])(?::[0-9]{1,5})?\/#\/w\/[0-9a-f]{32}\/t\/[0-7][0-9A-HJKMNP-TV-Z]{25}(?:\/g\/(?:requirements|plan|code|verify))?$/

/**
 * The one display form of a hub content hash: the first 12 hex digits after `sha256:` in three groups of four
 * (`abcd ef01 2345`). Display only. Anything that is not `sha256:` plus 64 lower-case hex digits (or an already
 * grouped value) is `null`: the caller shows nothing rather than a guess.
 */
export function groupHash(h: unknown): string | null {
  if (typeof h !== 'string') return null
  if (SHORT.test(h)) return h
  const m = /^sha256:([0-9a-f]{12})[0-9a-f]{52}$/.exec(h)
  return m ? m[1].replace(/(.{4})(?=.)/g, '$1 ') : null
}

/** A host link is shown only when it is exactly the host's closed grammar. */
export const safeHubUrl = (u: unknown): string | null => (typeof u === 'string' && (URL_OK.test(u) || ADDON_URL_OK.test(u) || SETTINGS_URL_OK.test(u)) ? u : null)

const ADDON_URL_OK = /^https?:\/\/(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:.]+\])(?::[0-9]{1,5})?\/#\/w\/[0-9a-f]{32}\/addon\/[A-Za-z0-9_-]{1,8192}$/

const SETTINGS_URL_OK = /^https?:\/\/(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:.]+\])(?::[0-9]{1,5})?\/#\/w\/[0-9a-f]{32}\/settings\/[A-Za-z0-9_-]{1,512}$/

/** The settings the hub's channel may change, in plain words (hub protocol 8.8). Every other setting is signed where the person's key is. */
export const HUB_SETTING_WORDS: Record<string, { label: string; unit: string }> = {
  grant_hours: { label: 'Longest agent access', unit: 'hours' },
  claim_ttl_min: { label: 'Ticket reserved for', unit: 'minutes' },
  lease_ttl_min: { label: 'Single task held for', unit: 'minutes' },
}

/** What a `settings_set` action asks of the hub: the part the channel may set, and the names of the parts it may not. */
export function hubSettingsSplit(a: HandoffAction | null | undefined): { set: Record<string, number>; off: string[] } | null {
  if (a?.action !== 'settings_set') return null
  const set: Record<string, number> = {}
  const off: string[] = []
  for (const [k, v] of Object.entries(a)) {
    if (k === 'action' || v === undefined) continue
    if (k in HUB_SETTING_WORDS && typeof v === 'number') set[k] = v
    else off.push(k)
  }
  return { set, off }
}

/** The add-on actions that a full-mode workspace approves in the hub (FB15). */
export const isAddonGrant = (a: HandoffAction | null | undefined): a is Extract<HandoffAction, { action: 'addon_grant' | 'market_grant' | 'market_update' }> => a?.action === 'addon_grant' || a?.action === 'market_grant' || a?.action === 'market_update'

/** What the host is asked to describe for an action the hub can build (approve, request changes, verdict, answer); null otherwise. */
