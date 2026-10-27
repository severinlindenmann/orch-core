// Hub signing from Mission Control (V3 C11, D78/D81). In a workspace joined to a hub in full mode this page signs
// nothing: it shows what the host says to check ("Approve on your phone or in the hub"), the 12 characters to compare
// and one link the HOST built. Nothing here builds a link, holds a hub credential or falls back to a local key.
import type { HandoffAction } from './handoff'
import type { Workspace } from './types'

export type SigningMode = 'standalone' | 'selective' | 'full' | 'unknown'

/** The host's signing mode for a workspace; `full` and `unknown` (fail closed) take no local key. */
export const hubOnly = (w: Pick<Workspace, 'signing'> | undefined | null): boolean => w?.signing === 'full' || w?.signing === 'unknown'

export type HubSignRequest =
  | { kind: 'gate' | 'verdict'; ticket: string; gate: 'requirements' | 'plan' | 'code' | 'verify' }
  | { kind: 'question'; ticket: string; question: string }
  | { kind: 'status'; ticket: string }
  /** FB15: turn an add-on on through the hub; `request` is the prepared hand-off (op addon.grant) the link is for. */
  | { kind: 'addon'; request: string }

export interface HubSignInfo {
  mode: 'full' | 'unknown'
  kind?: 'addon'
  summary: { kind: string; ticket: string; gate: string | null; gate_gen: number | null; question: string | null; status: string; title: string; name?: string; version?: string; capabilities?: string[] }
  /** Twelve lower-case hex digits in groups of four; null for a status move (no hash to compare). */
  short_hash: string | null
  /** Built by the host from its pinned hub address; the page only checks it and never makes one. */
  url: string
  /** Add-on only: the same page by the hub's address for other devices, when `url` is the form for this computer. */
  other_url?: string
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
export const safeHubUrl = (u: unknown): string | null => (typeof u === 'string' && (URL_OK.test(u) || ADDON_URL_OK.test(u)) ? u : null)

const ADDON_URL_OK = /^https?:\/\/(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:.]+\])(?::[0-9]{1,5})?\/#\/w\/[0-9a-f]{32}\/addon\/[A-Za-z0-9_-]{1,8192}$/

/** The add-on actions that a full-mode workspace approves in the hub (FB15). */
export const isAddonGrant = (a: HandoffAction | null | undefined): a is Extract<HandoffAction, { action: 'addon_grant' | 'market_grant' }> => a?.action === 'addon_grant' || a?.action === 'market_grant'

/** What the host is asked to describe for an action the hub can build (approve, request changes, verdict, answer); null otherwise. */
export function hubRequestOf(a: HandoffAction | null): HubSignRequest | null {
  if (!a) return null
  switch (a.action) {
    case 'approve':
    case 'request_changes':
      return { kind: 'gate', ticket: a.key, gate: a.gate }
    case 'verdict':
      return { kind: 'verdict', ticket: a.key, gate: 'verify' }
    case 'answer':
      return { kind: 'question', ticket: a.key, question: a.question }
    default:
      return null
  }
}
