// DEV/TEST ONLY: an in-memory stand-in for the PROPOSED `pair/1` host ops (src/api/pairing.ts). It never talks to a
// host. The layout preview and the tests install it over `pairingApi` and drive the offer through its states with
// `emit`, the way `sub.open {topics:["pair"]}` items would arrive. Links use `*.example.test` and `demo-not-a-real-*`.
import { pairingApi, realPairingApi, type PairMember, type PairItem, type PairOfferStatus, type PairPreview, type PairState, type PairingApi } from '@/api/pairing'
import { ApiError } from '@/api/types'

export interface PairFixtureOptions {
  served?: boolean
  preview?: Partial<PairPreview>
  offers?: PairOfferStatus[]
  /** `member.list` with the device fields of the `read_ext` bump. */
  members?: PairMember[]
  /** Lifetime of a new offer (the host's `expires_ms` class: 5 minutes). */
  ttlMs?: number
  /** Make `offer()` fail with this code. */
  offerError?: { code: string; message: string }
}

export const FIXTURE_RELAY = 'relay.example.test'
const SECRET = 'demo-not-a-real-pair-secret-0000000000000'
const hex = (n: number) => n.toString(16).padStart(32, '0')

export const fixtureLinks = (n: number) => {
  const fragment = `v2.${'a'.repeat(32)}.${hex(n)}.${SECRET}.demo-not-a-real-wsk-pin.demo-not-a-real-pk-pin`
  return { native_link: `orch://pair?relay=https%3A%2F%2F${FIXTURE_RELAY}#${fragment}`, link: `https://${FIXTURE_RELAY}/pair#${fragment}` }
}

export const BASE_PREVIEW: PairPreview = {
  blocked: null,
  warnings: ['Click-to-sign stops working for this workspace once a phone is paired.'],
  scopes_default: ['look', 'decide'],
  ttl_max_ms: 300_000,
  live_offer: null,
}

export interface PairFixture {
  api: PairingApi
  /** Move an offer to a state (patching its fields) and send the `pair` sub item. */
  emit: (offer_id: string, state: PairState, patch?: Partial<PairOfferStatus>) => void
  offers: () => PairOfferStatus[]
  calls: { op: string; args: unknown[] }[]
}

export function createPairFixture(opts: PairFixtureOptions = {}): PairFixture {
  let offers: PairOfferStatus[] = (opts.offers ?? []).map((o) => ({ ...o }))
  const members = structuredClone(opts.members ?? [])
  const subs = new Set<(item: PairItem) => void>()
  const calls: PairFixture['calls'] = []
  let n = 0
  const preview = (): PairPreview => {
    const live = offers.find((o) => !['approved', 'rejected', 'expired', 'cancelled', 'failed'].includes(o.state))
    return { ...BASE_PREVIEW, ...opts.preview, live_offer: opts.preview?.live_offer !== undefined ? opts.preview.live_offer : (live?.offer_id ?? null) }
  }
  const emit: PairFixture['emit'] = (offer_id, state, patch = {}) => {
    offers = offers.map((o) => (o.offer_id === offer_id ? { ...o, ...patch, state } : o))
    for (const fn of [...subs]) fn({ t: 'pair', offer_id, state })
  }
  const log = (op: string, ...args: unknown[]) => calls.push({ op, args })
  const api: PairingApi = {
    served: () => opts.served ?? true,
    preview: async () => (log('preview'), preview()),
    offer: async (ws, args) => {
      log('offer', ws, args)
      const refused = opts.offerError
      if (refused) {
        // Like the host: a refusal at offer time is what the next preview reports as `blocked`.
        opts = { ...opts, preview: { ...opts.preview, blocked: { code: refused.code, message: refused.message } } }
        throw new ApiError(409, { ...refused, retryable: false })
      }
      n += 1
      const offer_id = hex(n)
      const now = Date.now()
      const expires_ms = now + (opts.ttlMs ?? 300_000)
      offers = [...offers, { offer_id, state: 'open', created_ms: now, expires_ms, scopes: ['look', 'decide'] }]
      return { offer_id, expires_ms, ...fixtureLinks(n) }
    },
    list: async () => (log('list'), { offers: offers.map((o) => ({ ...o })) }),
    answer: async (_ws, offer_id, approve, o) => {
      log('answer', offer_id, approve, ...(o ? [o] : []))
      const state: PairState = approve ? 'confirmed' : 'rejected'
      const reason = (o?.mismatches ?? 0) >= 3 ? 'The codes differed 3 times: the offer is refused.' : 'Rejected on this computer.'
      emit(offer_id, state, approve ? { sas: null } : { sas: null, reason })
      return { state }
    },
    cancel: async (_ws, offer_id) => {
      log('cancel', offer_id)
      emit(offer_id, 'cancelled')
      return { state: 'cancelled' }
    },
    members: async () => (log('members'), { members: structuredClone(members) }),
    subscribe: (_ws, onItem) => {
      subs.add(onItem)
      return () => subs.delete(onItem)
    },
  }
  return {
    api,
    emit,
    offers: () => offers,
    calls,
  }
}

/** Swap the fixture into `pairingApi`; returns the undo. */
export function installPairFixture(fixture: PairFixture): () => void {
  Object.assign(pairingApi, fixture.api)
  return () => Object.assign(pairingApi, realPairingApi)
}
