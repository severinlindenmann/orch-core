import { describe, expect, it, vi } from 'vitest'
import { closePolicy, connectTerm, endWords, isCpr, isDa1, ReplyFilter, termUrl, type TermHandlers } from './termProtocol'

describe('the close-code table (section 12.6.6): retry by the code alone', () => {
  it.each([
    [1000, 'done'], [4400, 'done'], [4401, 'ticket'], [4403, 'done'], [4409, 'take'], [4422, 'done'],
    [4429, 'after'], [4503, 'backoff'], [4500, 'done'], [1006, 'done'], [4999, 'done'],
  ])('%i → %s', (code, kind) => {
    expect(closePolicy(code, null).kind).toBe(kind)
  })

  it('4429 waits retry_after_ms (bounded)', () => {
    expect(closePolicy(4429, { reason: 'overflow', retry_after_ms: 700 })).toEqual({ kind: 'after', ms: 700 })
    expect(closePolicy(4429, { reason: 'overflow', retry_after_ms: 10 })).toEqual({ kind: 'after', ms: 250 })
    expect(closePolicy(4429, null)).toEqual({ kind: 'after', ms: 1000 })
  })

  it('core words for ends; the runner change says try again', () => {
    expect(endWords(4503, { reason: 'runner.changed' })).toBe('The runner changed; try again.')
    expect(endWords(4409, { reason: 'host.error', code: 'term.writer_busy' })).toBe('Another tab or device is controlling this terminal.')
    expect(endWords(1000, { reason: 'exited' })).toBe('The process exited.')
  })
})

describe('the reply allow-list', () => {
  it('CPR and DA1 only', () => {
    expect(isCpr('\x1b[12;40R')).toBe(true)
    expect(isDa1('\x1b[?1;2c')).toBe(true)
    for (const s of ['\x1b[>0;276;0c', '\x1b[=0c', '\x1b[0n', '\x1b[?12;1$y', '\x1bP1$r0m\x1b\\', '\x1b]11;rgb:0000/0000/0000\x07', '\x1b[?6;1R', 'a', '\x1b[1;1Rx'])
      expect(isCpr(s) || isDa1(s), JSON.stringify(s)).toBe(false)
  })

  it('tag 0 only for data xterm flagged as user input', () => {
    const f = new ReplyFilter()
    expect(f.classify('ls', { writer: true, replaying: false, user: false })).toBeNull()
    expect(f.classify('ls', { writer: true, replaying: false, user: true })).toEqual({ tag: 0, data: 'ls' })
    expect(f.classify('ls', { writer: false, replaying: false, user: true })).toBeNull()
    expect(f.classify('ls', { writer: true, replaying: true, user: true })).toBeNull()
    expect(f.classify('\x1b[3;4R', { writer: true, replaying: false, user: false })).toEqual({ tag: 1, data: '\x1b[3;4R' })
  })
})

class FakeSocket {
  static OPEN = 1
  readyState = 0
  binaryType = ''
  sent: unknown[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: unknown }) => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  constructor(public url: string) {}
  send(d: unknown) { this.sent.push(d) }
  close() {}
  open() { this.readyState = 1; this.onopen?.() }
}

function handlers() {
  const h = { ready: vi.fn(), output: vi.fn(), gap: vi.fn(), resize: vi.fn(), writer: vi.fn(), inputLost: vi.fn(), inputPaused: vi.fn(), exit: vi.fn(), closed: vi.fn() }
  return h satisfies TermHandlers
}

describe('the socket', () => {
  it('this origin only, the ticket first, offsets and tags', () => {
    expect(termUrl({ protocol: 'http:', host: '127.0.0.1:5050' })).toBe('ws://127.0.0.1:5050/term')
    vi.stubGlobal('WebSocket', FakeSocket)
    let sock!: FakeSocket
    const h = handlers()
    const c = connectTerm('TICKET', h, (u) => (sock = new FakeSocket(u)) as unknown as WebSocket)
    sock.open()
    expect(sock.binaryType).toBe('arraybuffer')
    expect(sock.sent).toEqual([JSON.stringify({ ticket: 'TICKET' })])
    sock.onmessage!({ data: JSON.stringify({ t: 'ready', start: 0, offset: 5, cols: 80, rows: 24, writer: true, state: 'running' }) })
    expect(h.ready).toHaveBeenCalledWith({ start: 0, offset: 5, cols: 80, rows: 24, writer: true, state: 'running' })
    const buf = new ArrayBuffer(11)
    new DataView(buf).setBigUint64(0, 4294967301n)
    new Uint8Array(buf).set([104, 105, 33], 8)
    sock.onmessage!({ data: buf })
    expect(h.output.mock.calls[0][0]).toBe(4294967301)
    expect(Array.from(h.output.mock.calls[0][1] as Uint8Array)).toEqual([104, 105, 33])
    sock.onmessage!({ data: '{"t":"writer","writer":"yes"}' }) // malformed: ignored
    sock.onmessage!({ data: 'not json' })
    expect(h.writer).not.toHaveBeenCalled()
    c.sendInput(1, new Uint8Array([27, 91, 49, 59, 49, 82]))
    expect(Array.from(sock.sent[1] as Uint8Array)).toEqual([1, 27, 91, 49, 59, 49, 82])
    c.resize(100, 30)
    expect(sock.sent[2]).toBe('{"t":"resize","cols":100,"rows":30}')
    sock.onmessage!({ data: JSON.stringify({ t: 'end', reason: 'host.error', code: 'attach.limit', retry_after_ms: 900, message: 'x' }) })
    sock.onclose!({ code: 4429 })
    expect(h.closed).toHaveBeenCalledWith(4429, { reason: 'host.error', code: 'attach.limit', message: 'x', retry_after_ms: 900 })
    vi.unstubAllGlobals()
  })
})

describe('a large paste (review L1)', () => {
  it('is split into messages of at most 64 KiB, each with its tag', () => {
    vi.stubGlobal('WebSocket', FakeSocket)
    let sock!: FakeSocket
    const c = connectTerm('T', handlers(), (u) => (sock = new FakeSocket(u)) as unknown as WebSocket)
    sock.open()
    const big = Uint8Array.from({ length: 150_000 }, (_, i) => i % 251)
    c.sendInput(0, big)
    const frames = sock.sent.slice(1) as Uint8Array[]
    expect(frames.length).toBe(3)
    expect(frames.every((f) => f.length <= 65536 && f[0] === 0)).toBe(true)
    const joined = frames.flatMap((f) => Array.from(f.subarray(1)))
    expect(joined).toEqual(Array.from(big))
    vi.unstubAllGlobals()
  })
})
