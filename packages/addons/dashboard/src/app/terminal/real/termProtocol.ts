// The browser side of the terminal WebSocket (contract section 12.6.6) and the page-side reply filter (12.6.7). Pure:
// no React, no xterm. The page decides retry by the close code alone; it never re-requests the write lease by itself.

import { remoteSocketProtocols } from '@/api/bootstrap'

export interface Ready {
  start: number
  offset: number
  cols: number
  rows: number
  writer: boolean
  state: 'running' | 'exited'
}

export interface TermEnd {
  reason: string
  code?: string
  message?: string
  retry_after_ms?: number
}

export interface TermHandlers {
  ready(r: Ready): void
  output(off: number, bytes: Uint8Array): void
  gap(from: number, to: number): void
  resize(cols: number, rows: number): void
  writer(writer: boolean): void
  inputLost(): void
  inputPaused(ms: number): void
  exit(code: number | null, signal: string | null): void
  /** The socket closed: `end` is the last `end` message, if one came (4401 has none). */
  closed(code: number, end: TermEnd | null): void
}

export type CloseAction =
  | { kind: 'done' }
  | { kind: 'ticket' }
  | { kind: 'after'; ms: number }
  | { kind: 'backoff' }
  | { kind: 'take' }

/** Section 12.6.6, the close-code table. Anything else (4500, unknown) does not retry. */
export function closePolicy(code: number, end: TermEnd | null): CloseAction {
  switch (code) {
    case 4401: return { kind: 'ticket' }
    case 4409: return { kind: 'take' }
    case 4429: return { kind: 'after', ms: Math.max(250, Math.min(60_000, end?.retry_after_ms ?? 1000)) }
    case 4503: return { kind: 'backoff' }
    default: return { kind: 'done' } // 1000, 4400, 4403, 4422, 4500 and anything unknown
  }
}

export const BACKOFF_MS = [1000, 2000, 5000, 10000, 30000]

/** CPR (`ESC [ r ; c R`) and DA1 (`ESC [ ? … c`): the only terminal-generated replies the page forwards (tag 1). */
export const isCpr = (s: string) => /^\x1b\[\d{1,5};\d{1,5}R$/.test(s)
export const isDa1 = (s: string) => /^\x1b\[\?[0-9;]{0,64}c$/.test(s)

/**
 * Tags `onData`: 0 only for the person's own key, paste or IME input (`user`: xterm itself flagged the data as user
 * input, see termView), 1 only for CPR or DA1, and nothing for everything else (DA2/DA3, DECRQM, DSR, DECRQSS,
 * XTGETTCAP, OSC colour replies…). A read attachment, a lost lease and a replay send nothing.
 */
export class ReplyFilter {
  classify(data: string, state: { writer: boolean; replaying: boolean; user: boolean }): { tag: 0 | 1; data: string } | null {
    if (!state.writer || state.replaying) return null
    if (state.user) return { tag: 0, data }
    if (isCpr(data) || isDa1(data)) return { tag: 1, data }
    return null
  }
}

/**
 * Pointer and wheel events: xterm flags their reports (mouse tracking, wheel-to-arrows in the alternate buffer) as user
 * input too, but §12.6.7 allows tag 0 only for key and paste input, so data flagged while one of these is being
 * dispatched is swallowed.
 */
export const POINTER_EVENTS = ['mousedown', 'mouseup', 'mousemove', 'wheel', 'click', 'auxclick', 'dblclick', 'contextmenu', 'pointerdown', 'pointerup', 'pointermove'] as const

export interface TermConnection {
  sendInput(tag: 0 | 1, bytes: Uint8Array): void
  resize(cols: number, rows: number): void
  close(): void
}

/** The terminal socket's address: this page's own origin, path `/term` (the server allows nothing else). */
export const termUrl = (loc: Pick<Location, 'protocol' | 'host'> = window.location) => `${loc.protocol === 'https:' ? 'wss' : 'ws'}://${loc.host}/term`

/** Payload bytes per input message: 64 KiB minus the tag byte. */
export const MAX_INPUT = 65535

const int = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0

/** Open the socket, present the ticket, and route the server's messages. Malformed messages are ignored. */
export function connectTerm(ticket: string, h: TermHandlers, make: (url: string) => WebSocket = (u) => new WebSocket(u, remoteSocketProtocols())): TermConnection {
  const sock = make(termUrl())
  sock.binaryType = 'arraybuffer'
  let end: TermEnd | null = null
  let closed = false
  sock.onopen = () => sock.send(JSON.stringify({ ticket }))
  sock.onmessage = (e: MessageEvent) => {
    if (closed) return
    if (e.data instanceof ArrayBuffer) {
      if (e.data.byteLength < 8) return
      const off = Number(new DataView(e.data).getBigUint64(0, false))
      h.output(off, new Uint8Array(e.data, 8))
      return
    }
    if (typeof e.data !== 'string') return
    let m: Record<string, unknown>
    try { m = JSON.parse(e.data) as Record<string, unknown> } catch { return }
    if (!m || typeof m !== 'object') return
    switch (m.t) {
      case 'ready':
        if (int(m.start) && int(m.offset) && int(m.cols) && int(m.rows) && typeof m.writer === 'boolean')
          h.ready({ start: m.start, offset: m.offset, cols: m.cols, rows: m.rows, writer: m.writer, state: m.state === 'exited' ? 'exited' : 'running' })
        break
      case 'gap': if (int(m.from) && int(m.to)) h.gap(m.from, m.to); break
      case 'resize': if (int(m.cols) && int(m.rows)) h.resize(m.cols, m.rows); break
      case 'writer': if (typeof m.writer === 'boolean') h.writer(m.writer); break
      case 'input_lost': h.inputLost(); break
      case 'input_paused': if (int(m.retry_ms)) h.inputPaused(m.retry_ms); break
      case 'exit': h.exit(typeof m.code === 'number' ? m.code : null, typeof m.signal === 'string' ? m.signal : null); break
      case 'end':
        if (typeof m.reason === 'string')
          end = {
            reason: m.reason,
            ...(typeof m.code === 'string' ? { code: m.code } : {}),
            ...(typeof m.message === 'string' ? { message: m.message } : {}),
            ...(int(m.retry_after_ms) ? { retry_after_ms: m.retry_after_ms } : {}),
          }
        break
    }
  }
  sock.onclose = (e: CloseEvent) => {
    if (closed) return
    closed = true
    h.closed(e.code, end)
  }
  sock.onerror = () => {}
  const open = () => sock.readyState === WebSocket.OPEN && !closed
  return {
    sendInput(tag, bytes) {
      if (!open() || bytes.length === 0) return
      // One message is at most 64 KiB with its tag (the server's max_size; review L1): a big paste goes in pieces.
      for (let at = 0; at < bytes.length; at += MAX_INPUT) {
        const part = bytes.subarray(at, at + MAX_INPUT)
        const frame = new Uint8Array(part.length + 1)
        frame[0] = tag
        frame.set(part, 1)
        sock.send(frame)
      }
    },
    resize(cols, rows) {
      if (open()) sock.send(JSON.stringify({ t: 'resize', cols, rows }))
    },
    close() {
      closed = true
      try { sock.close(1000) } catch { /* already closed */ }
    },
  }
}

/** The person-facing words for an end (core words; the server's `message` is untrusted and shown only as a detail). */
export function endWords(code: number, end: TermEnd | null): string {
  const key = end?.reason === 'host.error' ? end.code ?? end.reason : end?.reason ?? ''
  switch (key) {
    case 'exited': return 'The process exited.'
    case 'run.stopped': return 'The run was stopped.'
    case 'cancelled': return 'This terminal was closed.'
    case 'writer_busy': case 'term.writer_busy': return 'Another tab or device is controlling this terminal.'
    case 'runner.changed': return 'The runner changed; try again.'
    case 'runner.lost': case 'runner.restarting': case 'runner.unavailable': return 'The runner is not reachable. Trying again…'
    case 'host.restarting': case 'host.unavailable': return 'The host is not reachable. Trying again…'
    case 'term.not_attachable': return 'This terminal can no longer be attached.'
    case 'not_visible': case 'person.removed': case 'op.not_allowed': case 'not_found': return 'You can no longer see this terminal.'
    case 'runner.stale': return 'The agent runner is from an older orch version. Restart it in your terminal: orch host enable.'
    case 'runner.disabled': return 'Terminals are switched off for this workspace.'
    case 'ticket_invalid': return 'The terminal refused the attachment.'
    case 'overflow': return 'The terminal fell behind. Reconnecting…'
    case 'protocol': case 'invalid.input': return 'The terminal connection broke.'
    case 'attach.limit': case 'run.limit': case 'rate.limited': case 'quota.host': return 'Too many terminals are open. Trying again shortly…'
  }
  if (code === 4401) return 'Reconnecting…'
  return 'The terminal connection ended.'
}

/** The socket factory the dock uses (the dev-only layout preview replaces it with a fixture; nothing else does). */
export const termTransport: { connect: typeof connectTerm } = { connect: connectTerm }
