import type { QueryClient } from '@tanstack/react-query'
import { useSyncExternalStore } from 'react'
import { toast } from 'sonner'
import type { TicketDocument } from '@/api/types'
import { startAfterCreate, type StartOutcome, type StartRequest } from '@/app/pages/new-ticket/useStartAfterCreate'
import type { StartInitial } from '@/app/terminal/real/StartAgentDialog'

/** The agent's progress on a ticket, in the one status pill. */
export type Phase =
  | { kind: 'idle' }
  | { kind: 'starting'; slow: boolean; /** a run of this ticket still lives: never offer a second start next to it (run 16 P01) */ live?: true }
  | { kind: 'open'; /** a run lives but claims nothing: the way into it */ run?: string }
  | { kind: 'waiting'; /** the run the agent waits in (open it with openDockOn) */ run: string }
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
  /** the run id (openDockOn) */
  run?: string
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
      if (r.noticeRead) void qc.invalidateQueries({ queryKey: ['risk-notices', r.ws] })
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

/**
 * The one "the agent is working" rule (run-9 J04), for the header, the next step, Today and every spinner: an agent
 * holds a live claim AND does not wait. `wait` is the run the host marks waiting (Today's `terminal_wait` of this
 * ticket). Its first source is the host's own record of the agent inside `orch wait` (run-10 J04: the harness ticks a
 * timer there, so the terminal is never quiet); a hook marker or a quiet terminal is only the fallback for agents
 * outside orch wait. A live process or claim alone never means working.
 */
export function agentWorking(ticket: Pick<TicketDocument, 'claim'>, wait?: string | null): boolean {
  return !!ticket.claim && ticket.claim.live !== false && !wait
}

/**
 * One rule for Terminals, the dock and the ticket page: an agent session that still runs although its ticket is done or
 * closed is "finished" (the process ends when the person presses Close). An unknown status (list not loaded) is not finished.
 */
export function agentFinished(run: { kind: string; state: string }, ticketStatus: string | null | undefined): boolean {
  return run.kind === 'agent' && (run.state === 'running' || run.state === 'starting') && (ticketStatus === 'done' || ticketStatus === 'closed')
}

export function phaseOf(ticket: TicketDocument, o: { needsYou: boolean; mark?: Mark; run?: ProgressRun | null; now: number; wait?: string | null }): Phase {
  if (ticket.status === 'done' || ticket.status === 'closed') return { kind: 'idle' }
  const runStart = o.run ? Date.parse(o.run.started_at) : NaN
  const runDead = o.run?.state === 'lost' || o.run?.state === 'exited'
  const runLive = o.run?.state === 'starting' || o.run?.state === 'running'
  // The agent waits for the person (a question or permission prompt the host detects) but holds no claim: say so, with
  // the way to it. A wait counts only while its run is live: a stale wait of a dead run is no wait (review P01).
  const claimLive = !!ticket.claim && ticket.claim.live !== false
  if (o.wait && !claimLive && !runDead) return { kind: 'waiting', run: o.wait }
  if (o.needsYou) return { kind: 'needs' }
  if (agentWorking(ticket, o.wait)) return { kind: 'working' }
  // A current live run beats an older failed mark: the agent runs, so nothing failed.
  if (o.mark?.failed && !runLive) return { kind: 'failed', reason: o.mark.failed.reason, initial: o.mark.failed.initial }
  // A run of this ticket that ended before an agent claimed it, newer than our ask: the start did not work.
  if (runDead && (o.mark ? runStart > o.mark.since : o.now - runStart < STARTING_MAX_MS))
    return { kind: 'failed', reason: 'The agent stopped before it began. Start it again.' }
  // A mark nobody answered for 3 minutes is dropped, so the pill cannot stick.
  const liveMark = !!o.mark && o.now - o.mark.since < STARTING_MAX_MS
  if (!liveMark && !runLive) return { kind: 'idle' }
  const since = o.mark?.since ?? runStart
  if (!runLive) return { kind: 'starting', slow: o.now - since >= STARTING_SLOW_MS }
  // A claim that is no longer live is history: nobody is working and the run is not ours to describe (review P01).
  if (ticket.claim && ticket.claim.live === false) return { kind: 'idle' }
  // The run lives but claims nothing and waits for nobody we can see: after the start window say it is open (never
  // "Taking longer" for ever, never Try again next to a live agent).
  if (o.now - (Number.isNaN(runStart) ? since : runStart) >= STARTING_MAX_MS) return { kind: 'open', ...(o.run?.run ? { run: o.run.run } : {}) }
  return { kind: 'starting', slow: o.now - since >= STARTING_SLOW_MS, live: true }
}
