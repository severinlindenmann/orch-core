// One click-to-sign request from "Sign" to the signed event (host contract section 0.8.6 item 7): prepare with the
// signer, read its View, show it, sign with the View's digest, or cancel. It prepares once per click (one idem, reused
// on a retry) and never signs by itself. A refusal never cancels: only `cancel()` does.
import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import { getToken } from '@/api/bootstrap'
import { CLICK_RISK, clickProblem, type ClickPrepared, type ClickProblem, type ClickSigned, type ClickViewAnswer } from '@/api/click'
import { ApiError } from '@/api/types'
import { newIdem, type HandoffAction, type HandoffRequest, type HandoffState } from '@/api/handoff'

export type ClickPhase =
  | 'idle'
  | 'preparing'
  /** Prepared; reading the signer's View. */
  | 'viewing'
  /** The View is on screen: the person may sign (after the arming time) or cancel. */
  | 'ready'
  | 'signing'
  /** The outcome of a sign is unknown: the host settles it by itself. Nothing is offered but waiting (no cancel, no second sign). */
  | 'settling'
  | 'done'
  | 'cancelled'
  /** Something ended the attempt or needs the person: `problem` says what, and `problem.next` the one thing to do. */
  | 'failed'

export interface ClickRun {
  phase: ClickPhase
  prepared: ClickPrepared | null
  view: ClickViewAnswer | null
  problem: ClickProblem | null
  signed: ClickSigned | null
  start: (action: HandoffAction) => Promise<void>
  /** Read the View again (it changed). The same request. */
  review: () => Promise<void>
  sign: (word?: string) => Promise<void>
  cancel: () => Promise<void>
  /**
   * "Try again" / "Prepare again": a request that is still open is cancelled first (no leaked slot); with none open and
   * the same action, the same key is reused so a lost prepare answer returns the same request.
   */
  again: (action: HandoffAction) => Promise<void>
  /** Back to the start (the request must already be settled or cancelled). */
  reset: (keepKey?: boolean) => void
  /** The person switched workspace with a request open: it was cancelled. */
  switched: boolean
}

const refetchKeys = ['ticket', 'tickets', 'board', 'today', 'workspaces', 'events', 'settings', 'members', 'settings-grants', 'identity']

/** The `onSettled` arguments of the hand-off dialog, for a signed click request (callers read `key` of a signed `new`). */
export function settledAs(prepared: ClickPrepared, signed: ClickSigned): { state: HandoffState; request: HandoffRequest } {
  const state: HandoffState = { id: prepared.request_id, state: 'submitted', op: prepared.op, key: signed.key ?? null, expires_at: prepared.expires_at, event_ids: [], seq: signed.seq ?? null }
  const request = { id: prepared.request_id, code: null, expires_at: prepared.expires_at, state: 'submitted', op: prepared.op, key: signed.key ?? null } as unknown as HandoffRequest
  return { state, request }
}

export function useClickSign(ws: string | undefined, onSettled?: (phase: 'done', state: HandoffState | null, request: HandoffRequest | null) => void): ClickRun {
  const qc = useQueryClient()
  const [phase, setPhaseState] = useState<ClickPhase>('idle')
  const [prepared, setPrepared] = useState<ClickPrepared | null>(null)
  const [view, setView] = useState<ClickViewAnswer | null>(null)
  const [problem, setProblem] = useState<ClickProblem | null>(null)
  const [signed, setSigned] = useState<ClickSigned | null>(null)
  const [switched, setSwitched] = useState(false)
  const live = useRef({ phase: 'idle' as ClickPhase, prepared: null as ClickPrepared | null, ws: undefined as string | undefined })
  const attempt = useRef<{ sig: string; idem: string; token: string | null } | null>(null)
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
  const refetch = useCallback(() => {
    for (const k of refetchKeys) void qc.invalidateQueries({ queryKey: [k] })
  }, [qc])
  const fail = useCallback(
    (e: unknown, keep: ClickPhase = 'failed') => {
      const p = clickProblem(e)
      setProblem(p)
      // A refusal to sign that can simply be tried again leaves the View where it is; the request stays open.
      setPhase(keep === 'ready' && p.next === 'retry' ? 'ready' : 'failed')
    },
    [setPhase],
  )

  const read = useCallback(
    async (where: string, id: string) => {
      setPhase('viewing')
      const got = await api.viewClick(where, id)
      if (!mounted.current) return
      // Fail closed: the owner-mandated risk line must be exactly what the card says, or nothing is shown.
      if (got.view.risk !== CLICK_RISK) throw new ApiError(409, { code: 'human.click_refused', message: 'The review lacks the risk line.', retryable: false, reason: 'check_failed' })
      setView(got)
      setProblem(null)
      setPhase('ready')
    },
    [setPhase],
  )

  const start = useCallback(
    async (action: HandoffAction) => {
      if (!ws || busy.current || live.current.phase !== 'idle') return // a double click is one prepare
      busy.current = true
      setProblem(null)
      setPhase('preparing')
      const sig = JSON.stringify(action)
      const token = getToken()
      if (attempt.current && attempt.current.token !== token) attempt.current = null
      const idem = attempt.current?.sig === sig ? attempt.current.idem : newIdem()
      attempt.current = { sig, idem, token }
      try {
        const got = await api.prepareClick(ws, action, idem)
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
        await read(ws, got.request_id)
      } catch (e) {
        if (!mounted.current) return
        // A retry of the same click keeps its key only while the failure may be a lost answer.
        if (!(e instanceof ApiError && e.retryable)) attempt.current = null
        fail(e)
      } finally {
        busy.current = false
      }
    },
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
    async (word?: string) => {
      const { prepared: open, ws: where } = live.current
      if (!open || !where || !view || busy.current || live.current.phase !== 'ready') return
      busy.current = true
      setPhase('signing')
      try {
        const got = await api.signClick(where, open.request_id, view.view_digest, word)
        if (!mounted.current) return
        setSigned(got)
        setPhase('done')
        refetch()
        const as = settledAs(open, got)
        settled.current?.('done', as.state, as.request)
      } catch (e) {
        if (mounted.current) {
          fail(e, 'ready') // refusals never cancel: the request stays open
          // An unknown outcome is waited out, never retried: the signature may already be at the host.
          if (clickProblem(e).next === 'check') setPhase('settling')
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
    const deadline = (Date.parse(prepared.expires_at) || Date.now() + 600_000) + 30_000
    const check = async () => {
      try {
        const got = await api.stateClick(where, prepared.request_id)
        if (stopped) return
        if (got.state === 'submitted') {
          stopped = true
          setSigned({ state: 'submitted', duplicate: false })
          setPhase('done')
          refetch()
          const as = settledAs(prepared, { state: 'submitted', duplicate: false })
          settled.current?.('done', as.state, as.request)
        } else if (got.state === 'cancelled' || got.state === 'expired') {
          stopped = true
          setProblem({ title: got.state === 'expired' ? 'Expired' : 'Cancelled', body: 'The host says it was not signed. You can sign in the terminal instead, or prepare it again.', next: 'terminal' })
          setPhase('failed')
          refetch()
        }
      } catch (e) {
        // The server no longer knows it (the request settled and was forgotten): the host's log is the truth.
        if (e instanceof ApiError && e.code === 'not_found' && !stopped) {
          stopped = true
          setProblem({ title: 'Settled', body: 'The page shows the current state of the ticket. Check it before signing again.', next: 'terminal' })
          setPhase('failed')
          refetch()
        }
      }
    }
    const timer = setInterval(() => {
      if (Date.now() > deadline) {
        stopped = true
        clearInterval(timer)
        return
      }
      void check()
    }, 2000)
    void check()
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [phase, prepared, setPhase, refetch])

  const cancel = useCallback(async () => {
    const { prepared: open, ws: where, phase: last } = live.current
    if (!open || !where || last === 'done' || last === 'cancelled' || last === 'idle' || last === 'settling') return // never cancel while a signature may be in flight
    try {
      await api.cancelClick(where, open.request_id)
    } catch {
      /* the signer and the host expire it anyway */
    }
    if (mounted.current) {
      setPhase('cancelled')
    }
  }, [setPhase])

  // Switching workspace with a request open cancels it (the panel is for the old workspace).
  useEffect(() => {
    const { prepared: open, ws: where, phase: last } = live.current
    if (!open || !where || where === ws || last === 'done' || last === 'cancelled' || last === 'idle') return
    void api.cancelClick(where, open.request_id).catch(() => {})
    setSwitched(true)
    setPhase('cancelled')
  }, [ws, setPhase])

  const reset = useCallback((keepKey = false) => {
    if (!keepKey) attempt.current = null // a new request is a new click: a new key
    live.current.prepared = null
    setSwitched(false)
    setPrepared(null)
    setView(null)
    setProblem(null)
    setSigned(null)
    setPhase('idle')
  }, [setPhase])

  const again = useCallback(
    async (action: HandoffAction) => {
      const open = live.current.prepared
      if (open && live.current.phase !== 'done' && live.current.phase !== 'settling') {
        await cancel() // the old request must not keep its quota slot
        reset()
      } else if (!open) {
        reset(true) // nothing was prepared yet: the same key, so a lost answer returns the same request
      } else {
        reset()
      }
      await start(action)
    },
    [cancel, reset, start],
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

  return { phase, prepared, view, problem, signed, start, review, sign, cancel, again, reset, switched }
}
