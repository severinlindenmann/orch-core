// Time helpers for generated histories. Everything is relative to the mock "now" (store.MOCK_EPOCH).
import type { Rng } from './rng'

export const NOW_MS = Date.parse('2026-10-09T11:30:00Z')
export const MIN = 60_000
export const HOUR = 3_600_000
export const DAY = 86_400_000
/** Start of the mock "today" (UTC). */
export const TODAY_MS = Date.parse('2026-10-09T00:00:00Z')

export const iso = (ms: number): string => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')

/** An event before it has a time: the type, the actor string and the payload. */
export interface Draft {
  type: string
  actor: string
  [k: string]: unknown
}
export interface GenEvent extends Draft {
  at: string
}

/**
 * Gives ordered drafts increasing times from `createdMs` (first) to `lastMs` (last). Times lean toward the end, so a
 * history is dense near its latest activity, like real work. Times are whole seconds and strictly increasing.
 */
export function stamp(rng: Rng, drafts: Draft[], createdMs: number, lastMs: number): GenEvent[] {
  const n = drafts.length
  const first = Math.min(createdMs, lastMs - n * 2000)
  const span = lastMs - first
  let prev = first - 1000
  return drafts.map((d, i) => {
    const f = n === 1 ? 1 : (i + rng.next() * 0.8) / (n - 1)
    let t = i === 0 ? first : i === n - 1 ? lastMs : first + Math.pow(Math.min(1, f), 0.55) * span
    t = Math.min(lastMs - (n - 1 - i) * 1000, Math.max(prev + 1000, Math.floor(t / 1000) * 1000))
    prev = t
    return { ...d, at: iso(t) }
  })
}
