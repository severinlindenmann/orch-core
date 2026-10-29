// One click-to-sign request from "Sign" to the signed event (host contract section 0.8.6 item 7): prepare with the
// signer, read its View, show it, sign with the View's digest, or cancel. It prepares an action intent (the server derives the host's idem from
// the action, so a second tab or a retry reaches the same request) and never signs by itself. A refusal never cancels: only `cancel()` does.
import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import { CLICK_RISK, clickProblem, contractViewOk, unknownOutcome, type ClickAction, type ClickPrepared, type ClickProblem, type ClickSigned, type ClickViewAnswer } from '@/api/click'
import { ApiError } from '@/api/types'
import { type HandoffRequest, type HandoffState } from '@/api/handoff'
import { trackHold } from './holdToasts'
import { fmtClock } from '@/lib/time'

export type ClickPhase =
  | 'idle'
  | 'preparing'
  /** Prepared; reading the signer's View. */
  | 'viewing'
  /** The View is on screen: the person may sign (after the arming time) or cancel. */
  | 'ready'
  | 'signing'
  /** Unsigned in the signer; only the global Undo toast can cancel it. */
  | 'held'
  /** The outcome of a sign is unknown: wait for settlement, never retry. */
  | 'settling'
  | 'done'
  | 'cancelled'
  /** Something ended the attempt or needs the person: `problem` says what, and `problem.next` the one thing to do. */
  | 'failed'

export interface ClickRun {
  retry: number
  phase: ClickPhase
  prepared: ClickPrepared | null
  view: ClickViewAnswer | null
  /**
   * C3: the View is the first one read for a request this hook prepared (`start`, `again`: callers start only on a
   * trusted press) and is of contract 4. Only such a View may run the one-press arm; a re-read (`review`) never does.
   */
  fresh: boolean
  problem: ClickProblem | null
  signed: ClickSigned | null
  start: (action: ClickAction, retryFloor?: number, pressed?: boolean) => Promise<void>
  /** Read the View again (it changed). The same request. */
  review: () => Promise<void>
  /** `revealKey`: a shown-secret grant only (the page key its review was made for). */
  sign: (revealKey?: string, sealed?: Record<string, import('@/api/sealed').Envelope>) => Promise<void>
  cancel: () => Promise<void>
  /**
   * "Try again" / "Prepare again" / "Do it again": a request that is still open is cancelled first (no leaked slot) and
   * the retry counter advances (an explicit new attempt); with none prepared yet, the same intent is repeated so a lost
   * prepare answer returns the same request.
   */
  again: (action: ClickAction, opts?: { pressed?: boolean; repeatOf?: boolean }) => Promise<void>
  /** Shown in the review after an explicit "Create another": it makes another thing like one already signed (display only). */
  repeatNote: boolean
  /** Back to the start (the request must already be settled or cancelled). */
  reset: (keepKey?: boolean) => void
  /** The person switched workspace with a request open: it was cancelled. */
  switched: boolean
  /** The person left (switched workspace) while a signature's outcome was unknown: nothing was cancelled. */
  leftUnsettled: boolean
  /** While settling: what is known about the wait (set when it can no longer be followed). */
  settleNote: string | null
  /** The last refusal as it came (callers that branch on more than `problem`, e.g. the runner grant's `why`). */
  error: unknown
}

/** A cancel that failed: the request stays open until it expires, so it is never shown as cancelled. */
export const CANCEL_FAILED = 'Could not cancel. It will expire by itself.'

const refetchKeys = ['ticket', 'tickets', 'board', 'today', 'workspaces', 'events', 'settings', 'members', 'settings-grants', 'identity']

/** The `onSettled` arguments of the hand-off dialog, for a signed click request (callers read `key` of a signed `new`). */
export function settledAs(prepared: ClickPrepared, signed: ClickSigned): { state: HandoffState; request: HandoffRequest } {
  const state: HandoffState = { id: prepared.request_id, state: 'submitted', op: prepared.op, key: signed.key ?? null, expires_at: prepared.expires_at, event_ids: [], seq: signed.seq ?? null, ...(signed.deposit ? { deposit: signed.deposit } : {}) }
  const request = { id: prepared.request_id, code: null, expires_at: prepared.expires_at, state: 'submitted', op: prepared.op, key: signed.key ?? null } as unknown as HandoffRequest
  return { state, request }
}

/** `minLeadS`: the host's minimum scheduling lead when the page knows it (the schedules list), for a "too soon" refusal.
 *  `opts.refetchOnDone` (default true): read the pages behind the dialog again after a signature. */
export function useClickSign(ws: string | undefined, onSettled?: (phase: 'done', state: HandoffState | null, request: HandoffRequest | null) => void, minLeadS?: number, opts: { refetchOnDone?: boolean } = {}): ClickRun {
  const qc = useQueryClient()
  const refetching = useRef(opts.refetchOnDone !== false)
  refetching.current = opts.refetchOnDone !== false
  const leadNow = useRef(minLeadS)
  leadNow.current = minLeadS
  const [phase, setPhaseState] = useState<ClickPhase>('idle')
  const [prepared, setPrepared] = useState<ClickPrepared | null>(null)
  const [view, setView] = useState<ClickViewAnswer | null>(null)
  const [problem, setProblem] = useState<ClickProblem | null>(null)
  const [signed, setSigned] = useState<ClickSigned | null>(null)
  const [switched, setSwitched] = useState(false)
  const [leftUnsettled, setLeftUnsettled] = useState(false)
  const [settleNote, setSettleNote] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [fresh, setFresh] = useState(false)
  const [repeatNote, setRepeatNote] = useState(false)
  const live = useRef({ phase: 'idle' as ClickPhase, prepared: null as ClickPrepared | null, ws: undefined as string | undefined })
  const attempt = useRef<{ sig: string; retry: number; lost: boolean } | null>(null)
  const sequenceStart = useRef(false)
  // The digest of the review the person last pressed Sign for, and the one-shot arming gate of `again`: a new review may
  // arm itself only when a press was asked for AND its digest is byte-identical to the pressed one.
  const pressedDigest = useRef<string | null>(null)
  const armGate = useRef<{ digest: string | null } | null>(null)
  const busy = useRef(false)
  const mounted = useRef(true)
  const settled = useRef(onSettled)
  settled.current = onSettled
  const wsNow = useRef(ws)
  wsNow.current = ws

  const setPhase = useCallback((p: ClickPhase) => {
    live.current.phase = p
    setPhaseState(p)
  }, [])
  const refetch = useCallback((success = false) => {
    // `refetchOnDone: false`: the step is only the first half of something (a hub join waits for the hub to confirm), and
    // the pages behind the dialog are refused until then. Only the success path skips; a refusal, cancel, expiry or
    // unknown outcome reads as before. The caller reads the pages once, when the whole thing is done.
    if (success && !refetching.current) return
    for (const k of refetchKeys) void qc.invalidateQueries({ queryKey: [k] })
  }, [qc])
  const fail = useCallback(
    (e: unknown, keep: ClickPhase = 'failed') => {
      const p = clickProblem(e, leadNow.current)
      if (sequenceStart.current && e instanceof ApiError && e.reason === 'resumable') p.next = 'retry'
      setError(e)
      setProblem(p)
      // A refusal to sign that can simply be tried again leaves the View where it is; the request stays open.
      setPhase(keep === 'ready' && p.next === 'retry' ? 'ready' : 'failed')
    },
    [setPhase],
  )

  const read = useCallback(
    async (where: string, id: string, first = false, gate: { digest: string | null } | null = null) => {
      setPhase('viewing')
      const got = await api.viewClick(where, id)
      if (!mounted.current || live.current.phase === 'cancelled' || live.current.prepared?.request_id !== id) return
      // Fail closed: the owner-mandated risk line must be exactly what the card says, or nothing is shown.
      if (got.view.risk !== CLICK_RISK) throw new ApiError(409, { code: 'human.click_refused', message: 'The review lacks the risk line.', retryable: false, reason: 'check_failed' })
      // C3: only a contract-4 View (headline and risk lines) of a request prepared here may run the one-press arm.
      setFresh(first && contractViewOk(got.view) && (!gate || (gate.digest !== null && gate.digest === got.view_digest)))
      setView(got)
      setProblem(null)
      setPhase('ready')
    },
    [setPhase],
  )

  const start = useCallback(
    /** `pressed`: a real press started this (default). An auto-prepare without one (run-9 J04: the start review
     * opens on the host's View) passes `false`, so the one-press arm (`fresh`) is never set without a press. */
    async (action: ClickAction, retryFloor = 0, pressed = true) => {
      sequenceStart.current = action.action === 'new_sequence' && !!action.launch
      const gate = armGate.current
      armGate.current = null
      if (!ws || busy.current || live.current.phase !== 'idle') return // a double click is one prepare
      busy.current = true
      setProblem(null)
      setPhase('preparing')
      const sig = JSON.stringify(action)
      if (attempt.current?.sig !== sig) attempt.current = { sig, retry: 0, lost: false }
      // An explicit change of signing method advances the shared action intent.
      attempt.current.retry = Math.max(attempt.current.retry, retryFloor)
      attempt.current.lost = false
      const current = attempt.current
      try {
        const got = await api.prepareClick(ws, action, current.retry)
        if (!mounted.current || wsNow.current !== ws) {
          void api.cancelClick(ws, got.request_id).catch(() => {}) // nobody will see this request
          if (mounted.current) {
            setSwitched(true)
            setPhase('cancelled')
          }
          return
        }
        live.current.prepared = got
        live.current.ws = ws
        setPrepared(got)
        await read(ws, got.request_id, pressed, gate)
      } catch (e) {
        if (!mounted.current) return
        // The answer may have been lost: only then does "Try again" repeat the same intent. A definite answer (the
        // action is done, its attempt ended) moves on only by an explicit click, which advances the counter.
        if (attempt.current) attempt.current.lost = !(e instanceof ApiError) || e.retryable
        fail(e)
      } finally {
        busy.current = false
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ws, setPhase, read, fail],
  )

  const review = useCallback(async () => {
    const { prepared: open, ws: where } = live.current
    if (!open || !where || busy.current) return
    busy.current = true
    try {
      await read(where, open.request_id)
    } catch (e) {
      if (mounted.current) fail(e)
    } finally {
      busy.current = false
    }
  }, [read, fail])

  const sign = useCallback(
    async (revealKey?: string, sealed?: Record<string, import('@/api/sealed').Envelope>) => {
      const { prepared: open, ws: where } = live.current
      if (!open || !where || !view || busy.current || live.current.phase !== 'ready') return
      busy.current = true
      pressedDigest.current = view.view_digest
      setPhase('signing')
      try {
        const delay = view.view.hold_allowed ? 10 : undefined // only a held action carries the delay; every other call keeps its old shape
        const got = await (delay ? api.signClick(where, open.request_id, view.view_digest, revealKey, sealed, delay) : sealed ? api.signClick(where, open.request_id, view.view_digest, revealKey, sealed) : api.signClick(where, open.request_id, view.view_digest, revealKey))
        if (got.state === 'held' && got.deadline_ms) {
          trackHold(where, open.request_id, got.deadline_ms / 1000, () => {
            refetch(true)
            const as = settledAs(open, { state: 'submitted', duplicate: false })
            settled.current?.('done', as.state, as.request)
          })
          if (mounted.current) setPhase('held')
          return
        }
        if (!mounted.current) return
        setSigned(got)
        setPhase('done')
        refetch(true)
        const as = settledAs(open, got)
        settled.current?.('done', as.state, as.request)
      } catch (e) {
        if (mounted.current) {
          fail(e, 'ready') // refusals never cancel: the request stays open
          // An unknown outcome (the signer's, or a lost/failed HTTP answer) is waited out, never retried or replaced:
          // the signature may already be at the host.
          if (clickProblem(e).next === 'check' || unknownOutcome(e)) setPhase('settling')
        }
        refetch()
      } finally {
        busy.current = false
      }
    },
    [view, setPhase, refetch, fail],
  )

  // An unknown outcome: ask the server where the request stands (it reads the host through its own link) until it settles.
  useEffect(() => {
    if (phase !== 'settling' || !prepared || !live.current.ws) return
    const where = live.current.ws
    let stopped = false
    setSettleNote(null)
    const expires = Date.parse(prepared.expires_at)
    const deadline = (Number.isNaN(expires) ? Date.now() + 600_000 : expires) + 30_000
    const stillOpen = () => {
      const when = Number.isNaN(expires) ? '' : ` It expires at ${fmtClock(new Date(expires).toISOString())}.`
      setSettleNote(`Still not settled.${when} Check the ticket's activity before doing anything else; nothing is offered until it settles.`)
    }
    const check = async (last = false) => {
      try {
        const got = await api.stateClick(where, prepared.request_id)
        if (stopped) return
        if (got.state === 'submitted') {
          stopped = true
          setSigned({ state: 'submitted', duplicate: false })
          setPhase('done')
          refetch(true)
          const as = settledAs(prepared, { state: 'submitted', duplicate: false })
          settled.current?.('done', as.state, as.request)
        } else if (got.state === 'cancelled' || got.state === 'expired') {
          stopped = true
          setProblem({ title: got.state === 'expired' ? 'Expired' : 'Cancelled', body: 'The host says it was not signed. Start again to prepare a new request.', next: 'prepare' })
          setPhase('failed')
          refetch()
        } else if (last) stillOpen()
      } catch (e) {
        if (stopped) return
        // Not even the server can say: never present that as "nothing was signed".
        if (e instanceof ApiError && e.code === 'intent.failed') {
          stopped = true
          fail(e)
          refetch()
        } else if (e instanceof ApiError && e.code === 'not_found') {
          stopped = true
          setSettleNote("Could not confirm what happened. Check the ticket's activity before doing anything else.")
          refetch()
        } else if (last) stillOpen()
      }
    }
    const timer = setInterval(() => {
      if (Date.now() > deadline) {
        stopped = true
        clearInterval(timer)
        void api.stateClick(where, prepared.request_id).then(
          (got) => (got.state === 'submitted' || got.state === 'cancelled' || got.state === 'expired' ? undefined : stillOpen()),
          () => stillOpen(),
        )
        return
      }
      void check()
    }, 2000)
    void check()
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [phase, prepared, setPhase, refetch, fail])

  const cancel = useCallback(async () => {
    const { prepared: open, ws: where, phase: last } = live.current
    if (!open || !where || last === 'done' || last === 'cancelled' || last === 'idle' || last === 'settling' || last === 'held') return // never cancel while a signature may be in flight
    setPhase('cancelled') // Tombstone immediately, before the cancellation round trip.
    try {
      await api.cancelClick(where, open.request_id)
    } catch {
      // The request is still open (the signer and the host expire it): never say "cancelled" for it.
      if (mounted.current) {
        setProblem({ title: CANCEL_FAILED, body: '', next: 'unavailable' }) // one plain line, no Try again

        setPhase('failed')
      }
      return
    }
    if (mounted.current) {
      setPhase('cancelled')
    }
  }, [setPhase])

  // Switching workspace with a request open cancels it (the panel is for the old workspace).
  useEffect(() => {
    const { prepared: open, ws: where, phase: last } = live.current
    if (!open || !where || where === ws || last === 'done' || last === 'cancelled' || last === 'idle') return
    if (last === 'settling' || last === 'held') {
      // Its outcome is unknown: nothing is cancelled and nothing is claimed. The server keeps it unsettled.
      setLeftUnsettled(true)
      return
    }
    void api.cancelClick(where, open.request_id).catch(() => {})
    setSwitched(true)
    setPhase('cancelled')
  }, [ws, setPhase])

  const reset = useCallback((keepKey = false) => {
    // Not keeping the attempt is an explicit "Do it again" / "Start again": the next prepare is another intent.
    if (!keepKey && attempt.current) attempt.current = { ...attempt.current, retry: attempt.current.retry + 1 }
    live.current.prepared = null
    pressedDigest.current = null
    armGate.current = null
    setSwitched(false)
    setLeftUnsettled(false)
    setSettleNote(null)
    setPrepared(null)
    setView(null)
    setFresh(false)
    setRepeatNote(false)
    setProblem(null)
    setError(null)
    setSigned(null)
    setPhase('idle')
  }, [setPhase])

  const again = useCallback(
    async (action: ClickAction, opts: { pressed?: boolean; repeatOf?: boolean } = {}) => {
      const pressedBefore = pressedDigest.current
      const open = live.current.prepared
      if (open && error instanceof ApiError && error.reason === 'resumable') {
        setProblem(null)
        await review()
        return
      }
      if (open && live.current.phase !== 'done' && live.current.phase !== 'settling') {
        await cancel() // the old request must not keep its quota slot
        reset()
      } else if (!open) {
        reset(attempt.current?.lost ?? false) // nothing was prepared: the same intent after a lost answer, else an explicit new one
      } else {
        reset()
      }
      if (opts.repeatOf) setRepeatNote(true)
      armGate.current = { digest: (opts.pressed ?? true) ? pressedBefore : null } // the one place that decides arming for every caller
      await start(action, 0, opts.pressed ?? true)
    },
    [cancel, reset, start, error, review],
  )

  // Leaving the screen with a request open cancels it. Deferred one tick so React StrictMode's simulated unmount does not.
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      const { prepared: open, ws: where, phase: last } = live.current
      if (open && where && (last === 'ready' || last === 'viewing' || last === 'failed')) {
        setTimeout(() => {
          if (!mounted.current) void api.cancelClick(where, open.request_id).catch(() => {})
        }, 0)
      }
    }
  }, [])

  return { retry: attempt.current?.retry ?? 0, phase, prepared, view, fresh, problem, signed, repeatNote, start, review, sign, cancel, again, reset, switched, leftUnsettled, settleNote, error }
}
