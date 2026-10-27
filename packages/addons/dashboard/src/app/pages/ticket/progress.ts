import type { QueryClient } from '@tanstack/react-query'
import { useSyncExternalStore } from 'react'
import { toast } from 'sonner'
import type { TicketDocument } from '@/api/types'
import { startAfterCreate, type StartOutcome, type StartRequest } from '@/app/pages/new-ticket/useStartAfterCreate'
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

/**
 * Start the agent without making anyone wait: the ticket page shows "Getting started…" from the mark. A started run keeps
 * the mark (the header clears it when the agent claims the ticket); a start that needs a person fails the mark with the reason.
 */
export async function startInBackground(qc: QueryClient, r: StartRequest): Promise<StartOutcome> {
  startMark(r.ws, r.key)
  try {
    const out = await startAfterCreate(r)
    if (out.kind === 'started') {
      void qc.invalidateQueries({ queryKey: ['runs', r.ws] })
      void qc.invalidateQueries({ queryKey: ['ticket', r.ws, r.key] })
      void qc.invalidateQueries({ queryKey: ['today', r.ws] })
    } else {
      failMark(r.ws, r.key, out.reason, out.initial)
      toast.warning(out.reason)
    }
    return out
  } catch (e) {
    console.error(e)
    const reason = 'The agent was not started.'
    toast.warning(reason)
    const { harness, mode, model } = r.reviewed.choice
    const initial: StartInitial = { harness, mode, ...(model ? { model } : {}) }
    failMark(r.ws, r.key, reason, initial)
    return { kind: 'ask', reason, initial }
  }
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
  // A run of this ticket that ended before an agent claimed it, newer than our ask: the start did not work.
  const runDead = o.run?.state === 'lost' || o.run?.state === 'exited'
  if (runDead && (o.mark ? runStart > o.mark.since : o.now - runStart < STARTING_MAX_MS))
    return { kind: 'failed', reason: 'The agent stopped before it began. Start it again.' }
  const runStarting = o.run?.state === 'starting'
  const runRunning = o.run?.state === 'running' && o.now - runStart < STARTING_MAX_MS
  // A mark nobody answered for 3 minutes is dropped, so the pill cannot stick.
  const liveMark = !!o.mark && o.now - o.mark.since < STARTING_MAX_MS
  if (!liveMark && !runStarting && !runRunning) return { kind: 'idle' }
  const since = o.mark?.since ?? runStart
  return { kind: 'starting', slow: o.now - since >= STARTING_SLOW_MS }
}
