// Usage (add-on, read only): agent session time per ticket, from the host's own run records. Tokens and cost are never
// invented: the server says `null` and the page says "Not reported". Real host only (the mockup keeps its own page).
import { queryOptions } from '@tanstack/react-query'
import { call } from './runs'
import { fmtSpan } from '@/lib/time'

export interface UsageTotals {
  runs: number
  /** Session time of the runs with a known end (a running one counts its time so far). */
  ms: number
  running: number
  /** Runs that ended without a usable end time: counted as runs, never as time. */
  unknown: number
}
export interface UsageTicketRow extends UsageTotals {
  /** null for the "No ticket" and "Other tickets" rows. */
  ticket: string | null
  title: string | null
  /** Runs on tickets this page cannot name any more (folded together). */
  other: boolean
  none: boolean
}
export interface UsageWindow extends UsageTotals {
  tickets: UsageTicketRow[]
  /** Set when the window is longer than what has been counted: the count starts here. */
  counted_since: string | null
}
export interface UsageRecentRun {
  started_at: string
  ended_at: string | null
  ms: number | null
  running: boolean
  harness: string | null
}
export type UsageData =
  | { available: false }
  | {
      available: true
      scope: 'your'
      windows: { week: UsageWindow; month: UsageWindow }
      recent: UsageRecentRun[]
      tokens: null
      cost: null
    }

const w = (ws: string) => `/api/w/${encodeURIComponent(ws)}`

export const usageQueries = {
  workspace: (ws: string) => queryOptions({ queryKey: ['usage', ws], queryFn: () => call<UsageData>('GET', `${w(ws)}/usage`), retry: false, refetchInterval: 60_000 }),
  ticket: (ws: string, ticket: string) =>
    queryOptions({ queryKey: ['usage', ws, ticket], queryFn: () => call<UsageData>('GET', `${w(ws)}/usage?ticket=${encodeURIComponent(ticket)}`), retry: false, refetchInterval: 60_000 }),
}

/** Session time in words; none counted is "None yet", never "0". */
export function spanText(t: UsageTotals): string {
  if (t.ms <= 0 && t.runs === 0) return 'None yet'
  if (t.ms <= 0) return 'Time not recorded'
  return fmtSpan(t.ms) + (t.running ? ' so far' : '')
}

export function runsText(t: UsageTotals): string {
  const n = t.runs === 1 ? '1 run' : `${t.runs} runs`
  return t.unknown ? `${n} · duration unavailable for ${t.unknown}` : n
}

export const NOT_REPORTED = 'Not reported'
/** One line for the whole page: tokens and cost are never shown, so they are not repeated per tile. */
export const NO_TOKENS_NOTE = "Token counts and cost aren't available."
export const WINDOW_LABEL = { week: 'Last 7 days', month: 'Last 30 days' } as const
