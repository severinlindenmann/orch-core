// One real terminal (M7a): xterm per section 12.6.7 (termView), the WebSocket of section 12.6.6 (termProtocol) and the
// lease of section 12.6.5. Read-only by default; "Take control" asks for the lease with take:true; a reconnect, a
// remount or a focus change never asks for it by itself. The one exception is the first attach of the tab that just
// started this run (`initial: 'write'`, B12A N5): nobody else can hold a run that was created a moment ago, so that tab
// asks for the lease outright instead of showing "Read only". Retry is decided by the close code alone.

import { useEffect, useImperativeHandle, useRef, useState, type Ref } from 'react'
import { ApiError } from '@/api/types'
import { markNoReplay, runsApi, type Run } from '@/api/runs'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { BACKOFF_MS, closePolicy, endWords, termTransport, type connectTerm, type TermConnection, type TermEnd } from './termProtocol'
import { createTermView, termOptions, type TermView } from './termView'
import { TermTools } from './TermTools'
import { termTheme, TERM_PADDING } from './termTheme'

/** One attach request: the mode asked for, and whether it takes the lease (only after the person's own click). */
export interface AttachRequest {
  mode: 'read' | 'write'
  take: boolean
  /** Bumped for every new attachment of this mount. */
  n: number
}

export type LeaseState = 'reading' | 'writing' | 'busy' | 'lost'

/** What a chat composer can do with the terminal: write a message into the TUI, or ask for the write lease. */
export interface RealTerminalHandle {
  /** Bracketed paste plus Enter, through the lease this tab holds. False when it does not hold it (nothing is sent). */
  sendInput(text: string): boolean
  take(): void
}

export function RealTerminal({ ws, run, initial, connect = termTransport.connect, handle, onLease }: {
  ws: string
  run: Run
  /** The first attach of this mount: read, except right after this tab started the shell (write, and it takes the lease). */
  initial: 'read' | 'write'
  /** Tests pass a fake socket. */
  connect?: typeof connectTerm
  /** Focus mode's chat view writes through this terminal (design phase 2, decision 7). */
  handle?: Ref<RealTerminalHandle>
  onLease?: (lease: LeaseState) => void
}) {
  const host = useRef<HTMLDivElement>(null)
  const view = useRef<TermView | null>(null)
  const [toolsView, setToolsView] = useState<TermView | null>(null)
  const [finding, setFinding] = useState(false)
  const conn = useRef<TermConnection | null>(null)
  const [req, setReq] = useState<AttachRequest>({ mode: initial, take: initial === 'write', n: 0 })
  const [lease, setLease] = useState<LeaseState>('reading')
  // An ended run is not attached by itself: every page load would use up one of the few attach tickets a minute (and
  // log a refusal when they run out) for output nobody asked to see. "Show output" attaches it, once.
  const [replay, setReplay] = useState(() => run.state !== 'exited' && run.state !== 'lost')
  const [status, setStatus] = useState<{ words: string; detail?: string; final: boolean } | null>(null)
  const [paused, setPaused] = useState(false)
  const [lost, setLost] = useState(false)
  const [exit, setExit] = useState<{ code: number | null; signal: string | null } | null>(null)
  const failures = useRef(0)
  const focused = useRef(false)
  const runRef = useRef(run)
  runRef.current = run
  const leaseRef = useRef<LeaseState>('reading')
  leaseRef.current = lease
  useEffect(() => { onLease?.(lease) }, [lease, onLease])
  useImperativeHandle(handle, () => ({
    sendInput(text) {
      const c = conn.current
      if (!c || leaseRef.current !== 'writing') return false
      c.sendInput(0, new TextEncoder().encode(`\x1b[200~${text}\x1b[201~\r`))
      return true
    },
    take: () => setReq((r) => ({ mode: 'write', take: true, n: r.n + 1 })),
  }), [])

  // xterm: one per mount of this run.
  useEffect(() => {
    const el = host.current
    if (!el) return
    const v = createTermView(el, termOptions({
      theme: termTheme(el),
    }))
    v.term.textarea?.setAttribute('aria-label', 'Terminal input')
    view.current = v
    setToolsView(v)
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
      if (!replay) return
      let ticket: string
      try {
        ticket = (await runsApi.ticket(ws, run.run, req.mode, req.take)).ticket
      } catch (e) {
        if (cancelled) return
        // An ended run is never retried: asking again cannot bring it back (and every try is a logged 503).
        const ended = runRef.current.state === 'exited' || runRef.current.state === 'lost'
        const retryable = e instanceof ApiError && e.status >= 500 && !ended
        if (ended) markNoReplay(runRef.current.run)
        setStatus({ words: ended ? 'This terminal has ended and its output can no longer be shown.' : retryable ? 'The terminal is not reachable. Trying again…' : 'This terminal cannot be attached.', final: !retryable })
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
          // K7: the tab that just opened this terminal types into it at once (once per mount; a reconnect never steals focus).
          if (r.writer && initial === 'write' && !focused.current) { focused.current = true; v?.term.focus() }
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
          } else if (runRef.current.state === 'exited' || runRef.current.state === 'lost') {
            // The run is over: reconnecting cannot help, so stop asking.
            setLease('reading')
            setStatus({ words: endWords(code, end), detail, final: true })
            if (code === 4422 || end?.code === 'term.not_attachable') markNoReplay(runRef.current.run)
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
  }, [ws, run.run, req.n, replay])

  const take = () => setReq((r) => ({ mode: 'write', take: true, n: r.n + 1 }))
  const writing = lease === 'writing'
  // Someone else holds the lease while this tab reads (from the run's live state).
  const busy = lease === 'busy' || (lease === 'reading' && run.writer && run.state === 'running')
  const running = run.state === 'running' && !(status?.final)
  return (
    <div className="flex h-full min-h-0 flex-col" data-real-terminal={run.run} onKeyDownCapture={(e) => {
      if (e.metaKey && e.key.toLowerCase() === 'f' && view.current) {
        e.preventDefault()
        e.stopPropagation()
        setFinding(true)
      }
    }}>
      {running && !writing && (
        <div role="status" className="flex shrink-0 items-center gap-2 border-b border-border bg-surface px-2 py-1 text-xs text-text-muted" data-lease={lease === 'lost' ? 'lost' : busy ? 'busy' : 'reading'}>
          <span className="min-w-0 truncate">
            {lease === 'lost' ? 'Another tab or device took control of this terminal.' : busy ? 'Another tab or device is controlling this terminal.' : 'Read only.'}
          </span>
          <Button variant="link" size="xs" className="shrink-0" onClick={take}>{lease === 'lost' ? 'Take back' : 'Take control'}</Button>
        </div>
      )}
      {!replay && (
        <div role="status" className="flex shrink-0 items-center gap-2 border-b border-border bg-surface px-2 py-1 text-xs text-text-muted" data-term-status data-ended-idle>
          <span className="min-w-0 truncate">This terminal has ended.</span>
          <Button variant="link" size="xs" className="shrink-0" onClick={() => setReplay(true)}>Show output</Button>
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
      <TermTools view={toolsView} run={run} finding={finding} onFinding={setFinding} />
      {/* A read-only viewer renders at the writer's size and scrolls; the writer's terminal fits this box. */}
      <div ref={host} role="group" aria-label="Terminal" aria-readonly={writing ? undefined : true} data-terminal-session={run.run}
        style={{ padding: TERM_PADDING }}
        className={`min-h-0 flex-1 bg-bg ${writing ? 'overflow-hidden' : 'overflow-auto'}`} />
    </div>
  )
}
