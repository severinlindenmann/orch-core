// One terminal hand-off from click to outcome (design 4.1, 4.3). It prepares a request once per click, follows it by
// polling `GET /handoff/<id>` (bounded) and by the live stream (a query update triggers an early check), and never
// prepares again on its own. The server derives the host's idem from the action itself, so a lost answer, a second tab
// or a new login all reach the SAME request; only the person's explicit "Do it again" / "Start again" advances `retry`.
import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import { ALREADY_DONE, CHANGED, INTENT_OPEN, expiryMs, requestIsConsistent, type HandoffAction, type HandoffRequest, type HandoffState } from '@/api/handoff'
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
  /** The host holds an attempt of this action that this dashboard cannot reach (the terminal, or from before a restart): nothing is prepared until it settles. */
  | 'attempt_open'
  /** The host's answer does not recompute to its own fingerprint: never shown as valid. */
  | 'invalid'
  | 'error'

/** What a failed submit's catalog code means to the person; an unknown code is shown as it is. */
export function failedSubmit(code: string): string {
  if (code === 'gate.stale' || code === 'question.stale' || code === 'members.stale') return 'The ticket changed before it was signed, so the host refused the signature. Prepare again to sign what is there now.'
  if (code === 'human.request_expired') return 'The request ran out of time before the signature reached the host.'
  return `The host refused the signature (${code}). Nothing was signed.`
}

export const POLL_MS = 2000
/** The poll gives up this long after the request would have expired on the host (clock skew, a slow host). */
const GRACE_MS = 20000
const MAX_FAILED_POLLS = 5

export interface HandoffRun {
  phase: HandoffPhase
  request: HandoffRequest | null
  state: HandoffState | null
  error: ApiError | null
  /** `floor`: the terminal fallback after a click attempt starts above that attempt's counter (the host binds an idem to its path). */
  prepare: (action: HandoffAction, floor?: number) => Promise<void>
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
    async (action: HandoffAction, floor = 0) => {
      if (!ws || busy.current || live.current.phase === 'waiting') return // a double click is one prepare
      busy.current = true
      setError(null)
      setPhase('preparing')
      const sig = JSON.stringify(action)
      if (attempt.current?.sig !== sig) attempt.current = { sig, retry: 0, lost: false }
      if (attempt.current.retry < floor) attempt.current = { ...attempt.current, retry: floor }
      const { retry } = attempt.current
      try {
        const got = await api.prepareHandoff(ws, action, retry)
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
        if (ALREADY_DONE.has(err.code)) {
          refetch()
          setPhase('already')
        } else if (CHANGED.has(err.code)) {
          refetch()
          setPhase('changed')
        } else if (err.code === 'human.request_expired') {
          setPhase('expired')
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

  // Follow an open request: poll every 2 s (bounded by its expiry), and check at once when the live stream updates the
  // ticket. The poll only reads; it never prepares and never submits.
  useEffect(() => {
    if (phase !== 'waiting' || !request || !pinned) return
    const ws = pinned
    const deadline = (expiryMs(request.expires_at) ?? Date.now() + 600_000) + GRACE_MS
    let failed = 0
    let stopped = false
    let inflight = false
    const check = async () => {
      if (stopped || inflight) return
      inflight = true
      try {
        const got = await api.getHandoff(ws, request.id)
        failed = 0
        if (stopped) return
        if (got.id !== request.id || got.op !== request.op) throw new ApiError(0, { code: 'not_found', message: 'That is not the request that was prepared.', retryable: false }) // never follow another request
        // A terminal answer ends the watch before finish() refetches: that refetch is itself a query update.
        if (got.error?.code && got.state === 'submitted') {
          stopped = true
          setError(new ApiError(409, { code: got.error.code, message: failedSubmit(got.error.code), retryable: false }))
          finish('error', got)
        } else if (got.state === 'submitted' || got.state === 'cancelled' || got.state === 'expired') {
          stopped = true
          finish(got.state === 'submitted' ? 'done' : got.state, got)
        } else setState(got)
      } catch (e) {
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
      if (Date.now() > deadline) {
        stopped = true
        clearInterval(timer)
        finish('expired', null)
        return
      }
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

  return { phase, request, state, error, prepare, cancel, reset, switched }
}
