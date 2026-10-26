// One terminal hand-off from click to outcome (design 4.1, 4.3). It prepares a request once per click, follows it by
// polling `GET /handoff/<id>` and by the live stream (a query update triggers an early check), and never
// prepares again on its own. The server derives the host's idem from the action itself, so a lost answer, a second tab
// or a new login all reach the SAME request; only the person's explicit "Do it again" / "Start again" advances `retry`.
import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import { ALREADY_DONE, CHANGED, INTENT_OPEN, requestIsConsistent, type HandoffAction, type HandoffRequest, type HandoffState } from '@/api/handoff'
import { ApiError } from '@/api/types'

export type HandoffPhase =
  | 'idle'
  | 'preparing'
  /** The request is open: the person signs in the terminal. */
  | 'waiting'
  /** The signer submitted: the event is in the log. */
  | 'done'
  | 'cancelled'
  | 'expired'
  /** The host says the thing is already done (idempotency, a second approval): nothing to sign. */
  | 'already'
  /** The ticket changed between prepare and sign: refetched; the person prepares again. */
  | 'changed'
  /** The answer to the prepare was lost: "Try again" asks the host for the same request (the same intent, never a second). */
  | 'unknown'
  /** The host confirms an open request for this op+key; no new prepare until it ends. */
  | 'attempt_open'
  | 'failed'
  | 'blocked'
  /** The host's answer does not recompute to its own fingerprint: never shown as valid. */
  | 'invalid'
  | 'error'

/** What a failed submit's catalog code means to the person; an unknown code is shown as it is. */
export function failedSubmit(code: string): string {
  if (code === 'lock.busy' || code === 'host.busy') return 'The host is busy. Try again shortly.'
  if (code === 'gate.stale' || code === 'question.stale' || code === 'members.stale') return 'The ticket changed before it was signed, so the host refused the signature. Prepare again to sign what is there now.'
  if (code === 'human.request_expired') return 'The request ran out of time before the signature reached the host.'
  return `The host refused the signature (${code}). Nothing was signed.`
}

export const POLL_MS = 2000
/** A sequence (one review, N signatures) that makes no progress for this long has ended: the signer's session is 10 minutes. */
export const SEQUENCE_STALL_MS = 11 * 60_000
/** The agent access is the signer's very last step, signed right after the ticket's steps: if it does not follow, it stopped. */
export const GRANT_STALL_MS = 45_000
const MAX_FAILED_POLLS = 5
/** How many earlier ended (cancelled, expired) attempts of the same action one press steps past to reach a fresh request. */
export const STALE_HOPS = 16

export interface HandoffRun {
  retry: number
  phase: HandoffPhase
  request: HandoffRequest | null
  state: HandoffState | null
  error: ApiError | null
  prepare: (action: HandoffAction, retryFloor?: number) => Promise<void>
  cancel: () => Promise<void>
  /** Back to the start (the person chose "Prepare again"). The previous request must already be settled or cancelled. */
  reset: (keepKey?: boolean) => void
  /** The person switched workspace while this request was open (or being prepared): it was cancelled, nothing was signed. */
  switched: boolean
}

const refetchKeys = ['ticket', 'tickets', 'board', 'today', 'workspaces', 'events', 'settings', 'members', 'settings-grants', 'identity']

export function useHandoff(ws: string | undefined, onSettled?: (phase: HandoffPhase, state: HandoffState | null, request: HandoffRequest | null) => void): HandoffRun {
  const qc = useQueryClient()
  const [phase, setPhaseState] = useState<HandoffPhase>('idle')
  const [request, setRequest] = useState<HandoffRequest | null>(null)
  const [state, setState] = useState<HandoffState | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  // The request is pinned to the workspace it was prepared in: poll, cancel and unmount use that one, whatever `ws` is now.
  const live = useRef({ phase: 'idle' as HandoffPhase, request: null as HandoffRequest | null, ws: undefined as string | undefined })
  const [pinned, setPinned] = useState<string | undefined>(undefined)
  // `retry` is the explicit "do it again" counter of this action; `lost` is a prepare whose answer may have been lost.
  const attempt = useRef<{ sig: string; retry: number; lost: boolean } | null>(null)
  const busy = useRef(false)
  const mounted = useRef(true)
  const settled = useRef(onSettled)
  settled.current = onSettled
  // The workspace the person is in now: a request is only for the workspace it was prepared in (design 5, switching).
  const wsNow = useRef(ws)
  wsNow.current = ws
  const [switched, setSwitched] = useState(false)

  const setPhase = useCallback((p: HandoffPhase) => {
    live.current.phase = p
    setPhaseState(p)
  }, [])
  const refetch = useCallback(() => {
    for (const k of refetchKeys) void qc.invalidateQueries({ queryKey: [k] })
  }, [qc])

  const prepare = useCallback(
    async (action: HandoffAction, retryFloor = 0) => {
      if (!ws || busy.current || live.current.phase === 'waiting') return // a double click is one prepare
      busy.current = true
      setError(null)
      setPhase('preparing')
      const sig = JSON.stringify(action)
      if (attempt.current?.sig !== sig) attempt.current = { sig, retry: 0, lost: false }
      // An explicit change of signing method advances the shared action intent.
      attempt.current.retry = Math.max(attempt.current.retry, retryFloor)
      const current = attempt.current
      try {
        let got = await api.prepareHandoff(ws, action, current.retry)
        // The host gave back a request this same action made earlier (the idem is derived from the action and `retry`),
        // and that one ended unsigned (cancelled by a wrong word or Cancel in the terminal, or expired): nothing was signed
        // and it can never be signed. Asking is the person's explicit new start, so step past EVERY such earlier attempt to
        // a fresh request instead of showing a dead end (an action cancelled twice must not need "Try again" twice). Not
        // when this click's own answer was lost (then the ended request IS its outcome); never past an open, signed or
        // inconsistent one; bounded, so a host that only answers "ended" stops at Cancelled.
        for (
          let hops = 0;
          hops < STALE_HOPS && !current.lost && (got.state === 'cancelled' || got.state === 'expired') && requestIsConsistent(got) && mounted.current && wsNow.current === ws;
          hops++
        ) {
          current.retry += 1
          got = await api.prepareHandoff(ws, action, current.retry)
        }
        if (!mounted.current) {
          void api.cancelHandoff(ws, got.id).catch(() => {}) // closed while preparing: nobody will see this code
          return
        }
        if (wsNow.current !== ws) {
          // The person switched workspace while the host was preparing: nobody will see this code.
          void api.cancelHandoff(ws, got.id).catch(() => {})
          setSwitched(true)
          setPhase('cancelled')
          return
        }
        if (!requestIsConsistent(got)) {
          // The host's own hashes do not recompute here: do not show it, and ask the host to drop it.
          void api.cancelHandoff(ws, got.id).catch(() => {})
          setPhase('invalid')
          return
        }
        live.current.request = got
        live.current.ws = ws
        setPinned(ws)
        setRequest(got)
        // The same action returns the same request in its current state (the host's idem record). A signed one is
        // "Already done" with an explicit "Do it again" - unless this very click lost its answer, then it IS the result.
        const lost = attempt.current?.lost ?? false
        if (attempt.current) attempt.current.lost = false
        const ended = (next: HandoffPhase) => {
          setPhase(next)
          refetch()
          settled.current?.(next, null, got)
        }
        if (got.state === 'submitted') {
          if (lost) ended('done')
          else {
            setPhase('already')
            refetch()
          }
        } else if (got.state === 'cancelled') ended('cancelled')
        else if (got.state === 'expired') ended('expired')
        else setPhase('waiting')
      } catch (e) {
        if (!mounted.current) return
        const err = e instanceof ApiError ? e : new ApiError(0, { code: 'host.unavailable', message: 'Could not reach the dashboard server.', retryable: true })
        setError(err)
        if (ALREADY_DONE.has(err.code) || err.code === 'intent.done') {
          refetch()
          setPhase('already')
        } else if (err.code === 'intent.failed') {
          setPhase('failed')
          refetch()
        } else if (CHANGED.has(err.code)) {
          refetch()
          setPhase('changed')
        } else if (err.code === 'human.request_expired' || err.code === 'intent.expired') {
          setPhase('expired')
        } else if (err.code === 'intent.blocked') {
          setPhase('blocked')
        } else if (err.code === INTENT_OPEN) {
          setPhase('attempt_open') // nothing is offered that prepares again until the host's attempt settles
        } else if (err.code === 'outcome.unknown') {
          if (attempt.current) attempt.current.lost = true
          setPhase('unknown') // "Try again" returns the same request if the host made one
        } else {
          if (err.retryable && attempt.current) attempt.current.lost = true
          setPhase('error')
        }
      } finally {
        busy.current = false
      }
    },
    [ws, setPhase, refetch],
  )

  const finish = useCallback(
    (next: HandoffPhase, got: HandoffState | null) => {
      setState(got)
      setPhase(next)
      refetch()
      settled.current?.(next, got, live.current.request)
    },
    [setPhase, refetch],
  )

  // Follow an open request: poll every 2 s until the host reports its terminal state; live stream updates check the
  // ticket. The poll only reads; it never prepares and never submits.
  useEffect(() => {
    if (phase !== 'waiting' || !request || !pinned) return
    const ws = pinned
    let failed = 0
    let stopped = false
    let inflight = false
    let progressed = { done: -1, at: Date.now() }
    const check = async () => {
      if (stopped || inflight) return
      inflight = true
      try {
        const got = await api.getHandoff(ws, request.id)
        failed = 0
        if (stopped) return
        if (got.id !== request.id || got.op !== request.op) throw new ApiError(0, { code: 'not_found', message: 'That is not the request that was prepared.', retryable: false }) // never follow another request
        // A terminal answer ends the watch before finish() refetches: that refetch is itself a query update.
        if (got.seq == null && got.state === 'submitted') {
          stopped = true
          setError(new ApiError(409, { code: 'intent.failed', message: `The earlier attempt was submitted but did not land${got.error?.code ? ` (${got.error.code})` : ''}. It was not signed into the log.`, retryable: false }))
          finish('failed', got)
        } else if (got.state === 'submitted' && got.progress && got.progress.done < got.progress.of) {
          // B11: the first signature of a sequence landed; the signer goes on in the person's terminal. Not done yet.
          setState(got)
          if (got.progress.done !== progressed.done) progressed = { done: got.progress.done, at: Date.now() }
          else {
            // Only the agent access is missing: the task is made and approved, and agents were NOT given access.
            const grantOnly = got.progress.last === 'grant' && got.progress.done === got.progress.of - 1
            if (Date.now() - progressed.at > (grantOnly ? GRANT_STALL_MS : SEQUENCE_STALL_MS)) {
              // F-712-4: the last signature may be landing right now: ask once more before saying it has not arrived.
              if (grantOnly) {
                const last = await api.getHandoff(ws, request.id).catch(() => null)
                if (stopped) return
                if (last && last.id === request.id && (last.progress?.done !== got.progress.done || last.state !== 'submitted')) {
                  progressed = { done: -1, at: Date.now() } // it moved (or finished): the next poll handles it from the top
                  return
                }
              }
              stopped = true
              const newTicket = (request.material.sequence?.intent as { kind?: unknown } | undefined)?.kind === 'new_ticket'
              const message = grantOnly && newTicket
                ? `${got.key ?? 'The ticket'} was created. Agent access is not confirmed yet: the last signature has not arrived (your terminal may still be waiting for you, or it says why it stopped). No agent was started; if access does not appear, start one from the ticket to give it again.`
                : grantOnly
                ? `${got.key ?? 'The task'} was created and approved; agent access is not confirmed yet: the last signature has not arrived (your terminal may still be waiting for you, or it says why it stopped). Agents cannot work until access is given from the agent access control.`
                : `Signing stopped after ${got.progress.done} of ${got.progress.of} signatures. ${got.key ?? 'The ticket'} was created and stays a draft with what was signed; ask again to continue.`
              setError(new ApiError(409, { code: grantOnly ? 'sequence.no_access' : 'intent.failed', message, retryable: false }))
              finish('failed', got)
            }
          }
        } else if (got.state === 'submitted' || got.state === 'cancelled' || got.state === 'expired') {
          stopped = true
          finish(got.state === 'submitted' ? 'done' : got.state, got)
        } else setState(got)
      } catch (e) {
        if (e instanceof ApiError && ['lock.busy', 'host.busy'].includes(e.code)) return
        failed += 1
        if ((e instanceof ApiError && e.code === 'not_found') || failed >= MAX_FAILED_POLLS) {
          stopped = true
          finish('unknown', null)
        }
      } finally {
        inflight = false
      }
    }
    const timer = setInterval(() => {
      void check()
    }, POLL_MS)
    // The live stream refreshes the ticket when the signer's event arrives: look at the request straight away.
    const unsubscribe = qc.getQueryCache().subscribe((event) => {
      if (event.type === 'updated' && event.action.type === 'success' && refetchKeys.includes(String(event.query.queryKey[0]))) void check()
    })
    return () => {
      stopped = true
      clearInterval(timer)
      unsubscribe()
    }
  }, [phase, request, pinned, qc, finish])

  const cancel = useCallback(async () => {
    const open = live.current.request
    const ws = live.current.ws
    if (!ws || !open || (live.current.phase !== 'waiting' && live.current.phase !== 'invalid')) return
    try {
      await api.cancelHandoff(ws, open.id)
    } catch {
      /* the host expires it anyway */
    }
    if (mounted.current) setPhase('cancelled')
  }, [setPhase])

  // Switching workspace with a request open cancels it at the host (design 5, 4.3): the dialog is for the old workspace.
  useEffect(() => {
    const { request: open, ws: where, phase: last } = live.current
    if (!open || !where || where === ws || (last !== 'waiting' && last !== 'invalid')) return
    void api.cancelHandoff(where, open.id).catch(() => {})
    setSwitched(true)
    setPhase('cancelled')
  }, [ws, setPhase])

  const reset = useCallback((keepKey = false) => {
    // Not keeping the attempt is an explicit "Do it again" / "Start again": the next prepare is another intent.
    if (!keepKey && attempt.current) attempt.current = { ...attempt.current, retry: attempt.current.retry + 1, lost: false }
    setSwitched(false)
    live.current.request = null
    setRequest(null)
    setState(null)
    setError(null)
    setPhase('idle')
  }, [setPhase])

  // Leaving the screen with a request still open cancels it (design 4.3). Deferred one tick so React StrictMode's
  // simulated unmount (which re-mounts at once) does not cancel a live request.
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      const { request: open, ws: where, phase: last } = live.current
      if (open && where && last === 'waiting') {
        setTimeout(() => {
          if (!mounted.current) void api.cancelHandoff(where, open.id).catch(() => {})
        }, 0)
      }
    }
  }, [])

  return { retry: attempt.current?.retry ?? 0, phase, request, state, error, prepare, cancel, reset, switched }
}
