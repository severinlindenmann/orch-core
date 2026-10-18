import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { PhoneHint, PhoneRequest } from '@/api/phone'
import { ApiError } from '@/api/types'

export function usePhone(ws: string | undefined) {
  const qc = useQueryClient()
  const [hint, setHint] = useState<PhoneHint | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [needsPair, setNeedsPair] = useState(false)
  const pinned = useRef(ws)
  const lock = useRef(false)
  const revision = useRef(0)
  const idem = useRef(crypto.randomUUID())
  const requestIdentity = useRef<string | null>(null)
  const terminalDeadline = useRef<number | null>(null)
  const liveHint = useRef<PhoneHint | null>(null)
  liveHint.current = hint
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      if (liveHint.current?.state === 'open' && pinned.current) void api.phoneCancel(pinned.current, liveHint.current.id).catch(() => {})
    }
  }, [])
  const report = (e: unknown) => setError(e instanceof ApiError && e.code === 'gate.stale'
    ? 'This changed while you looked. Reopen it.'
    : e instanceof ApiError && e.code === 'op.not_allowed' ? 'This workspace does not allow this on the phone.'
      : e instanceof Error ? e.message : 'Could not reach the host. Try again.')
  const start = async (request: PhoneRequest) => {
    if (!ws || lock.current || hint) return
    const identity = JSON.stringify([ws, request.ref, request.gate, request.gate_gen])
    if (requestIdentity.current !== identity) {
      requestIdentity.current = identity
      idem.current = crypto.randomUUID()
    }
    lock.current = true; setBusy(true); setError(null); pinned.current = ws
    try {
      const result = await api.phoneRequest(ws, { ...request, idem: idem.current })
      if (!mounted.current) {
        await api.phoneCancel(ws, result.id)
        return
      }
      setHint(result)
      setNeedsPair(!result.phones)
    } catch (e) { if (mounted.current) report(e) }
    finally { lock.current = false; if (mounted.current) setBusy(false) }
  }
  const cancel = async (): Promise<boolean> => {
    if (!hint || !pinned.current) return true
    if (lock.current) return false
    lock.current = true; revision.current += 1; setBusy(true)
    try {
      await api.phoneCancel(pinned.current, hint.id)
      const result = await api.phoneStatus(pinned.current, hint.id)
      const current = result.hints[0]
      setHint(current)
      return current.state !== 'done' && current.state !== 'open'
    } catch (e) { report(e); return false }
    finally { lock.current = false; setBusy(false) }
  }
  useEffect(() => {
    if (!hint || !pinned.current || hint.state === 'done') return
    if (hint.state !== 'open') terminalDeadline.current ??= Date.now() + 60_000
    let active = true, polling = false
    const poll = async () => {
      if (!active || polling || lock.current || (terminalDeadline.current !== null && Date.now() >= terminalDeadline.current)) return
      polling = true
      const version = revision.current
      try {
        const result = await api.phoneStatus(pinned.current!, hint.id)
        if (active && version === revision.current && result.hints[0]) {
          if (result.hints[0].state === 'done') stop()
          setHint(result.hints[0]); setError(null)
        }
      } catch (e) {
        if (active && version === revision.current) {
          if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
            setError(e.status === 404 ? 'This phone request is no longer available.' : 'You no longer have access to this phone request.')
            stop()
          } else report(e)
        }
      }
      finally { polling = false }
    }
    const timer = setInterval(() => void poll(), 2000)
    const unsubscribe = qc.getQueryCache().subscribe(event => {
      if (event.type === 'updated' && event.query.queryKey[0] === 'ticket') void poll()
    })
    const timeout = terminalDeadline.current === null ? undefined
      : setTimeout(() => stop(), Math.max(0, terminalDeadline.current - Date.now()))
    function stop() { active = false; clearInterval(timer); clearTimeout(timeout); unsubscribe() }
    return stop
  }, [hint?.id, hint?.state, qc]) // Allow up to one minute for an in-flight decision to land.
  useEffect(() => {
    if (hint?.state === 'done') {
      for (const key of ['ticket', 'tickets', 'today', 'board', 'events']) void qc.invalidateQueries({ queryKey: [key] })
    }
  }, [hint?.state, qc])
  return { hint, error, busy, needsPair, start, cancel, switched: (!!hint || busy) && ws !== pinned.current, reset: () => { revision.current += 1; terminalDeadline.current = null; setHint(null); setError(null); setNeedsPair(false); idem.current = crypto.randomUUID() } }
}
