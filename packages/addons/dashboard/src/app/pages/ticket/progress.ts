import { useSyncExternalStore } from 'react'
import type { TicketDocument } from '@/api/types'
import type { StartInitial } from '@/app/terminal/real/StartAgentDialog'

/** The agent's progress on a ticket, in the one status pill. */
export type Phase =
  | { kind: 'idle' }
  | { kind: 'starting'; slow: boolean }
  | { kind: 'working' }
  | { kind: 'needs' }
  | { kind: 'failed'; reason: string; initial?: StartInitial }

export interface Mark {
  since: number
  failed?: { reason: string; initial?: StartInitial }
}

export interface ProgressRun {
  state: 'starting' | 'running' | 'exited' | 'lost'
  started_at: string
}

export const STARTING_SLOW_MS = 90_000
export const STARTING_MAX_MS = 180_000

// Local marks: "we asked for an agent here", until the host shows a live claim.
const marks = new Map<string, Mark>()
const listeners = new Set<() => void>()
const id = (ws: string, key: string) => `${ws}:${key}`

function set(ws: string, key: string, mark: Mark | null) {
  if (mark) marks.set(id(ws, key), mark)
  else if (!marks.delete(id(ws, key))) return
  listeners.forEach((l) => l())
}

export function startMark(ws: string, key: string, now: number = Date.now()) {
  set(ws, key, { since: now })
}

export function failMark(ws: string, key: string, reason: string, initial?: StartInitial) {
  set(ws, key, { since: marks.get(id(ws, key))?.since ?? Date.now(), failed: { reason, initial } })
}

export function clearMark(ws: string, key: string) {
  set(ws, key, null)
}

export function markOf(ws: string, key: string): Mark | undefined {
  return marks.get(id(ws, key))
}

function subscribe(l: () => void) {
  listeners.add(l)
  return () => {
    listeners.delete(l)
  }
}

export function useMark(ws: string, key: string): Mark | undefined {
  return useSyncExternalStore(subscribe, () => markOf(ws, key))
}

export function phaseOf(ticket: TicketDocument, o: { needsYou: boolean; mark?: Mark; run?: ProgressRun | null; now: number }): Phase {
  if (ticket.status === 'done' || ticket.status === 'closed') return { kind: 'idle' }
  if (o.needsYou) return { kind: 'needs' }
  if (ticket.claim && ticket.claim.live !== false) return { kind: 'working' }
  if (o.mark?.failed) return { kind: 'failed', reason: o.mark.failed.reason, initial: o.mark.failed.initial }
  const runStart = o.run ? Date.parse(o.run.started_at) : NaN
  const runStarting = o.run?.state === 'starting'
  const runRunning = o.run?.state === 'running' && o.now - runStart < STARTING_MAX_MS
  if (!o.mark && !runStarting && !runRunning) return { kind: 'idle' }
  const since = o.mark?.since ?? runStart
  return { kind: 'starting', slow: o.now - since >= STARTING_SLOW_MS }
}
