// One real terminal (M7a): xterm per section 12.6.7 (termView), the WebSocket of section 12.6.6 (termProtocol) and the
// lease of section 12.6.5. Read-only by default; "Take control" asks for the lease with take:true; a reconnect, a
// remount or a focus change never asks for it by itself. Retry is decided by the close code alone.

import { useEffect, useRef, useState } from 'react'
import { ApiError } from '@/api/types'
import { runsApi, type Run } from '@/api/runs'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { BACKOFF_MS, closePolicy, endWords, termTransport, type connectTerm, type TermConnection, type TermEnd } from './termProtocol'
import { createTermView, termOptions, type TermView } from './termView'

const token = (el: HTMLElement, name: string) => getComputedStyle(el).getPropertyValue(name).trim() || undefined

/** One attach request: the mode asked for, and whether it takes the lease (only after the person's own click). */
export interface AttachRequest {
  mode: 'read' | 'write'
  take: boolean
  /** Bumped for every new attachment of this mount. */
  n: number
}

export type LeaseState = 'reading' | 'writing' | 'busy' | 'lost'

export function RealTerminal({ ws, run, initial, connect = termTransport.connect }: {
  ws: string
  run: Run
  /** The first attach of this mount: read, except right after the person opened this shell (write, never take). */
  initial: 'read' | 'write'
  /** Tests pass a fake socket. */
  connect?: typeof connectTerm
}) {
  const host = useRef<HTMLDivElement>(null)
  const view = useRef<TermView | null>(null)
  const conn = useRef<TermConnection | null>(null)
  const [req, setReq] = useState<AttachRequest>({ mode: initial, take: false, n: 0 })
  const [lease, setLease] = useState<LeaseState>('reading')
  const [status, setStatus] = useState<{ words: string; detail?: string; final: boolean } | null>(null)
  const [paused, setPaused] = useState(false)
  const [lost, setLost] = useState(false)
  const [exit, setExit] = useState<{ code: number | null; signal: string | null } | null>(null)
  const failures = useRef(0)
  const runRef = useRef(run)
  runRef.current = run

  // xterm: one per mount of this run.
  useEffect(() => {
    const el = host.current
    if (!el) return
    const v = createTermView(el, termOptions({
      theme: { background: token(el, '--bg'), foreground: token(el, '--text'), cursor: token(el, '--brand'), green: token(el, '--success'), red: token(el, '--danger'), cyan: token(el, '--brand'), yellow: token(el, '--warning'), blue: token(el, '--info'), brightBlack: token(el, '--text-faint') },
    }))
    v.term.textarea?.setAttribute('aria-label', 'Terminal input')
    view.current = v
    const ro = new ResizeObserver(() => {
      const size = view.current?.fit()
      if (size) conn.current?.resize(size.cols, size.rows)
    })
    ro.observe(el)
    return () => {
      ro.disconnect()
      view.current = null
      v.dispose()
    }
  }, [run.run])

  // The attachment: a browser ticket, then the socket. Every attach request of this mount gets its own.
  useEffect(() => {
    let cancelled = false
    let retry: ReturnType<typeof setTimeout> | undefined
    let pausedTimer: ReturnType<typeof setTimeout> | undefined
    const again = (ms: number) => { retry = setTimeout(() => { if (!cancelled) setReq((r) => ({ mode: 'read', take: false, n: r.n + 1 })) }, ms) }
    const start = async () => {
      let ticket: string
      try {
        ticket = (await runsApi.ticket(ws, run.run, req.mode, req.take)).ticket
      } catch (e) {
        if (cancelled) return
        const retryable = e instanceof ApiError && e.status >= 500
        setStatus({ words: retryable ? 'The terminal is not reachable. Trying again…' : 'This terminal cannot be attached.', final: !retryable })
        if (retryable) again(BACKOFF_MS[Math.min(failures.current++, BACKOFF_MS.length - 1)])
        return
      }
      if (cancelled) return
      const c = connect(ticket, {
        ready(r) {
          failures.current = 0
          setStatus(null)
          setExit(null)
          const v = view.current
          v?.setWriter(r.writer)
          v?.ready(r)
          setLease(r.writer ? 'writing' : runRef.current.writer ? 'busy' : 'reading')
          if (r.writer) {
            const size = v?.fit()
            if (size) c.resize(size.cols, size.rows)
          }
        },
        output: (off, bytes) => view.current?.output(off, bytes),
        gap: (from, to) => view.current?.gap(from, to),
        resize: (cols, rows) => view.current?.resize(cols, rows),
        writer(w) {
          view.current?.setWriter(w)
          setLease((was) => (w ? 'writing' : was === 'writing' ? 'lost' : 'reading'))
        },
        inputLost: () => setLost(true),
        inputPaused(ms) {
          setPaused(true)
          clearTimeout(pausedTimer)
          pausedTimer = setTimeout(() => setPaused(false), ms)
        },
        exit: (code, signal) => setExit({ code, signal }),
        closed(code: number, end: TermEnd | null) {
          if (cancelled) return
          view.current?.setWriter(false)
          conn.current = null
          const policy = closePolicy(code, end)
          const detail = end?.message
          if (policy.kind === 'take') {
            setLease('busy')
            setStatus(null)
          } else if (policy.kind === 'ticket') {
            setStatus({ words: endWords(code, end), final: false })
            again(250)
          } else if (policy.kind === 'after') {
            setStatus({ words: endWords(code, end), detail, final: false })
            again(policy.ms)
          } else if (policy.kind === 'backoff') {
            setStatus({ words: endWords(code, end), detail, final: false })
            again(BACKOFF_MS[Math.min(failures.current++, BACKOFF_MS.length - 1)])
          } else {
            setLease('reading')
            setStatus({ words: endWords(code, end), detail, final: true })
          }
        },
      })
      conn.current = c
      view.current?.setSink((tag, bytes) => c.sendInput(tag, bytes))
    }
    void start()
    return () => {
      cancelled = true
      clearTimeout(retry)
      clearTimeout(pausedTimer)
      view.current?.setSink(null)
      conn.current?.close()
      conn.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws, run.run, req.n])

  const take = () => setReq((r) => ({ mode: 'write', take: true, n: r.n + 1 }))
  const writing = lease === 'writing'
  // Someone else holds the lease while this tab reads (from the run's live state).
  const busy = lease === 'busy' || (lease === 'reading' && run.writer && run.state === 'running')
  const running = run.state === 'running' && !(status?.final)
  return (
    <div className="flex h-full min-h-0 flex-col" data-real-terminal={run.run}>
      {running && !writing && (
        <div role="status" className="flex shrink-0 items-center gap-2 border-b border-border bg-surface px-2 py-1 text-xs text-text-muted" data-lease={lease === 'lost' ? 'lost' : busy ? 'busy' : 'reading'}>
          <span className="min-w-0 truncate">
            {lease === 'lost' ? 'Another tab or device took control of this terminal.' : busy ? 'Another tab or device is controlling this terminal.' : 'Read only.'}
          </span>
          <Button variant="link" size="xs" className="shrink-0" onClick={take}>{lease === 'lost' ? 'Take back' : 'Take control'}</Button>
        </div>
      )}
      {status && (
        <div role="status" className="shrink-0 border-b border-border bg-surface px-2 py-1 text-xs text-text-muted" data-term-status>
          {status.words}{status.detail && <> <span className="text-text-faint">(<Inline>{status.detail}</Inline>)</span></>}
        </div>
      )}
      {exit && (
        <p role="status" className="shrink-0 border-b border-border bg-surface px-2 py-1 text-xs text-text-muted">
          Process exited{exit.code !== null ? ` with code ${exit.code}` : ''}{exit.signal ? ` (signal ${exit.signal.replace(/[^A-Za-z0-9]/g, '')})` : ''}.
        </p>
      )}
      {paused && <p role="status" className="shrink-0 px-2 py-0.5 text-xs text-warning">Input paused</p>}
      {lost && <p role="alert" className="shrink-0 px-2 py-0.5 text-xs text-warning">Some input was not delivered. Check the terminal before typing again.</p>}
      {/* A read-only viewer renders at the writer's size and scrolls; the writer's terminal fits this box. */}
      <div ref={host} role="group" aria-label="Terminal" aria-readonly={writing ? undefined : true} data-terminal-session={run.run}
        className={`min-h-0 flex-1 bg-bg p-2 ${writing ? 'overflow-hidden' : 'overflow-auto'}`} />
    </div>
  )
}
