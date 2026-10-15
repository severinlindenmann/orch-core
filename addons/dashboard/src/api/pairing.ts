// "Pair a phone" (Settings → Devices): the PROPOSED host ops of cap `pair/1` (proposal-dashboard-pairing-ops, 15 Oct
// 2026, not agreed). This module is the one seam: the page calls `pairingApi` and nothing else. Until the ops are
// agreed the real implementation answers "not available" for every call and no server route exists; the dev layout
// preview and the tests swap in a fixture (src/dev/pairFixture.ts).
//
// The pairing link carries the offer's secret. It is returned only by `offer()`, straight to the caller, and the page
// keeps it in component state: never in the query cache (no query or mutation holds it), storage, the URL, a log or a
// global. Everything else here is secret-free and may be cached.
import { queryOptions } from '@tanstack/react-query'
import { hasFeature } from './features'
import { ApiError } from './types'

export type PairState =
  | 'open'
  | 'pending'
  | 'confirmed'
  | 'cert_pending'
  | 'allocated'
  | 'published'
  | 'approved'
  | 'rejected'
  | 'expired'
  | 'cancelled'
  | 'failed'

export const TERMINAL_STATES: ReadonlySet<PairState> = new Set(['approved', 'rejected', 'expired', 'cancelled', 'failed'])
export const isTerminal = (s: PairState) => TERMINAL_STATES.has(s)

export interface PairBlocked {
  code: string
  message: string
  /**
   * ASSUMPTION (not in the proposal): for `sign.convenience_readers`, whether click-to-sign was ever used here (`true`:
   * turning it off does not help) or only switched on (`false`). Absent: unknown, the page explains both cases.
   */
  history?: boolean
}

/** `pair.preview {}`: a pure read, the same checks `pair.offer` runs first. */
export interface PairPreview {
  blocked: PairBlocked | null
  warnings: string[]
  scopes_default: string[]
  ttl_max_ms: number
  /** The workspace's one live offer, if any. Its link cannot be shown again. */
  live_offer: string | null
}

/** `pair.offer`: the only answer that carries the link. Never cached, stored or logged. */
export interface PairOffer {
  offer_id: string
  expires_ms: number
  /** `orch://pair?relay=…#v2.…`: what the QR carries (orch-mobile parses it). */
  native_link: string
  /** `https://<relay>/pair#v2.…`: a typed fallback only, never a clickable link. */
  link: string
}

/** One row of `pair.list {}`. No secrets. */
export interface PairOfferStatus {
  offer_id: string
  state: PairState
  created_ms: number
  expires_ms: number
  scopes: string[]
  /** The host's cleaned, device-chosen label: untrusted text, "named by the device". */
  label?: string | null
  /** Only while `pending`: the code the phone also shows. The page never displays it; the person types the phone's. */
  sas?: string | null
  /** Only while `cert_pending`: the code the Mac's primary shows. */
  cert_code?: string | null
  /** For `rejected`/`failed`: the host's reason (untrusted text). */
  reason?: string | null
  /** ASSUMPTION: `cert_pending` with the primary's certificate in hand, ready for the `relay.pair_device` signature. */
  cert_ready?: boolean
  /** ASSUMPTION: `failed` after `device.added`: the device stays and needs removing. */
  needs_remove?: string | null
}

/** One paired device (`device.list`, or `member.list` under a `read_ext`, proposed). */
export interface PairDevice {
  device: string
  /** The host's cleaned label, untrusted text. */
  label: string | null
  paired_ms: number | null
  relay: boolean
  cert_expires: string | null
  reader: number | null
}

/** A `pair` sub item: no secrets, so the page doesn't poll. */
export interface PairItem {
  t: 'pair'
  offer_id: string
  state: PairState
}

export interface PairingApi {
  /** Does this build talk to a host for pairing at all? The real one: not until the ops are agreed. */
  served: (ws: string) => boolean
  preview: (ws: string) => Promise<PairPreview>
  offer: (ws: string, args?: { scopes?: string[]; ttl_ms?: number }) => Promise<PairOffer>
  list: (ws: string) => Promise<{ offers: PairOfferStatus[] }>
  answer: (ws: string, offer_id: string, approve: boolean) => Promise<{ state: PairState }>
  cancel: (ws: string, offer_id: string) => Promise<{ state: PairState }>
  devices: (ws: string) => Promise<{ devices: PairDevice[] }>
  /** `sub.open {topics:["pair"]}`; returns the close function. */
  subscribe: (ws: string, onItem: (item: PairItem) => void) => () => void
}

export const PAIR_UNAVAILABLE = 'Pairing a phone is not available on this host yet.'

const unavailable = (): Promise<never> =>
  Promise.reject(new ApiError(404, { code: 'pair.unavailable', message: PAIR_UNAVAILABLE, retryable: false }))

/** The real implementation: "not available" until the proposed ops are agreed and the server serves them. */
export const realPairingApi: PairingApi = {
  served: () => false,
  preview: unavailable,
  offer: unavailable,
  list: unavailable,
  answer: unavailable,
  cancel: unavailable,
  devices: unavailable,
  subscribe: () => () => {},
}

/** The api the page uses. A mutable object so the dev preview and tests can swap methods (like `runsApi`). */
export const pairingApi: PairingApi = { ...realPairingApi }

/** The gate: the server lists feature `pair` (the host serves `pair/1` in `welcome.ops`) and this build talks to it. */
export const pairServed = (ws: string): boolean => hasFeature('pair', ws) && pairingApi.served(ws)

/** Secret-free reads only. The offer (the link) is never a query. */
export const pairQueries = {
  preview: (ws: string) => queryOptions({ queryKey: ['pair', ws, 'preview'], queryFn: () => pairingApi.preview(ws), staleTime: 0 }),
  list: (ws: string) => queryOptions({ queryKey: ['pair', ws, 'list'], queryFn: () => pairingApi.list(ws) }),
  devices: (ws: string) => queryOptions({ queryKey: ['pair', ws, 'devices'], queryFn: () => pairingApi.devices(ws) }),
}

/** The SAS as typed or served: base32, case-insensitive, spaces and dashes ignored. */
export const normaliseSas = (s: string): string => s.toUpperCase().replace(/[\s-]/g, '')

export type SasCheck = 'empty' | 'partial' | 'match' | 'differ'

/** Compare what the person typed (the phone's code) with the host's `sas`. Only `match` enables Approve. */
export function checkSas(typed: string, sas: string | null | undefined): SasCheck {
  const t = normaliseSas(typed)
  const s = normaliseSas(sas ?? '')
  if (!t) return 'empty'
  if (!s) return 'differ'
  if (t.length < s.length) return 'partial'
  return t === s ? 'match' : 'differ'
}

/** Where the person confirms on the Mac's primary (ASSUMPTION: the CLI command is not fixed yet). */
export const PRIMARY_COMMAND = 'orch keys primary approve'
export const convenienceOffCommand = (ws: string) => `orch host convenience off --workspace ${ws}`
