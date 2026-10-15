// The canary corpus of contract section 12.6.7, against the page's own terminal (a real xterm.js in jsdom, the same
// `createTermView` the dock uses). jsdom has no renderer: xterm's parser, its query replies (`onData`) and its OSC
// handlers run as in a browser, which is what the property is about. Asserted: the clipboard is untouched, no
// window.open/confirm, no document.title change, nothing with tag 0 without a key or paste event, and every tag 1 is a
// CPR `ESC [ r ; c R` or a DA1 `ESC [ ? … c`.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { isCpr, isDa1 } from './termProtocol'
import { createTermView, termOptions, type TermView } from './termView'

const enc = new TextEncoder()
const b = (s: string) => enc.encode(s)
const ESC = '\x1b'
const ST = `${ESC}\\`
const rep = (s: string, n: number) => s.repeat(n)

/** A small deterministic PRNG (mulberry32): the fuzz run is the same every time. */
function prng(seed: number) {
  return () => {
    seed |= 0
    seed = (seed + 0x6d2b79f5) | 0
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

export const CORPUS: Record<string, string> = {
  osc52Write: `${ESC}]52;c;ZGVtby1ub3QtYS1yZWFsLXNlY3JldA==\x07${ESC}]52;p;ZXZpbA==${ST}`,
  osc52Read: `${ESC}]52;c;?\x07${ESC}]52;s;?${ST}`,
  osc8Js: `${ESC}]8;;javascript:alert(document.domain)${ST}click me${ESC}]8;;${ST}`,
  osc8Data: `${ESC}]8;id=x;data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==\x07open${ESC}]8;;\x07`,
  titles: `${ESC}]0;pwned-0\x07${ESC}]1;pwned-1\x07${ESC}]2;pwned-2${ST}${ESC}[22;0t${ESC}[23;0t`,
  daFlood: rep(`${ESC}[c${ESC}[0c${ESC}[>c${ESC}[>0c${ESC}[=c`, 200),
  dsrFlood: rep(`${ESC}[5n${ESC}[6n${ESC}[?6n${ESC}[?15n${ESC}[?25n${ESC}[?26n${ESC}[?53n`, 200),
  decrqmFlood: rep(`${ESC}[4$p${ESC}[20$p${ESC}[?1$p${ESC}[?25$p${ESC}[?2004$p${ESC}[?1049$p`, 200),
  misc: rep(`${ESC}[>q${ESC}[>0q${ESC}[14t${ESC}[16t${ESC}[18t${ESC}[19t${ESC}[20t${ESC}[21t${ESC}[11t${ESC}[13t`, 50),
  colours: rep(`${ESC}]4;1;?\x07${ESC}]10;?\x07${ESC}]11;?${ST}${ESC}]12;?\x07`, 100),
  xtgettcap: rep(`${ESC}P+q544e${ST}${ESC}P+q636f6c6f7273${ST}${ESC}P+q4d73${ST}`, 200),
  decrqss: rep(`${ESC}P$qm${ST}${ESC}P$qr${ST}${ESC}P$q"p${ST}${ESC}P$q q${ST}${ESC}P$qs${ST}`, 200),
  modes: `${ESC}[?1000h${ESC}[?1006h${ESC}[?1004h${ESC}[?2004h${ESC}[?1049h${ESC}[?1049l`,
}

let view: TermView
let host: HTMLDivElement
let sent: { tag: 0 | 1; data: string }[]
const spies: Record<string, ReturnType<typeof vi.fn>> = {}
const dec = new TextDecoder()

async function settle() {
  await new Promise<void>((resolve) => view.term.write('', () => resolve()))
}

async function feed(chunks: Uint8Array[]) {
  let off = 1000
  for (const chunk of chunks) {
    view.output(off, chunk)
    off += chunk.length
  }
  await settle()
}

beforeEach(() => {
  document.title = 'orch'
  for (const name of ['writeText', 'readText', 'write', 'read']) spies[name] = vi.fn(() => Promise.resolve())
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: spies.writeText, readText: spies.readText, write: spies.write, read: spies.read } })
  spies.open = vi.spyOn(window, 'open').mockImplementation(() => null) as never
  spies.confirm = vi.spyOn(window, 'confirm').mockImplementation(() => false) as never
  spies.exec = vi.fn(() => false)
  ;(document as unknown as { execCommand: unknown }).execCommand = spies.exec
  host = document.createElement('div')
  document.body.appendChild(host)
  view = createTermView(host, termOptions())
  sent = []
  view.setSink((tag, bytes) => sent.push({ tag, data: dec.decode(bytes) }))
  view.setWriter(true)
  view.ready({ start: 1000, offset: 1000, cols: 80, rows: 24, writer: true, state: 'running' })
})

afterEach(() => {
  view.dispose()
  host.remove()
  vi.restoreAllMocks()
})

function assertProperty() {
  for (const name of ['writeText', 'readText', 'write', 'read', 'open', 'confirm', 'exec']) expect(spies[name], name).not.toHaveBeenCalled()
  expect(document.title).toBe('orch')
  expect(sent.filter((s) => s.tag === 0)).toEqual([])
  const bad = sent.filter((s) => s.tag === 1 && !isCpr(s.data) && !isDa1(s.data))
  expect(bad.map((s) => JSON.stringify(s.data))).toEqual([])
}

describe('the canary corpus (section 12.6.7)', () => {
  it('xterm is configured as the contract says', () => {
    const o = view.term.options
    expect(o.allowProposedApi).toBe(false)
    expect(o.windowOptions).toEqual({})
    expect(o.scrollback).toBe(5000)
    expect(o.convertEol).toBe(false)
    expect(typeof o.linkHandler?.activate).toBe('function')
  })

  // Floods that xterm does answer: the test sees those replies arrive at onData (and be swallowed by the filter).
  const ANSWERED = new Set(['daFlood', 'dsrFlood', 'decrqmFlood', 'decrqss'])
  for (const [name, text] of Object.entries(CORPUS)) {
    it(`${name}: nothing leaves but CPR/DA1, nothing is touched`, async () => {
      const raw: string[] = []
      const tap = view.term.onData((d) => raw.push(d))
      await feed([b(text)])
      tap.dispose()
      if (ANSWERED.has(name)) expect(raw.filter((d) => !isCpr(d) && !isDa1(d)).length).toBeGreaterThan(0)
      assertProperty()
    })
  }

  it('the pipeline is live: a CPR and a DA1 query are answered with tag 1', async () => {
    await feed([b(`${ESC}[6n${ESC}[c`)])
    expect(sent.some((s) => s.tag === 1 && isCpr(s.data))).toBe(true)
    expect(sent.some((s) => s.tag === 1 && isDa1(s.data))).toBe(true)
    assertProperty()
  })

  it('1 MB of random bytes', async () => {
    const rnd = prng(42)
    const chunks: Uint8Array[] = []
    for (let i = 0; i < 16; i++) chunks.push(Uint8Array.from({ length: 65536 }, () => Math.floor(rnd() * 256)))
    await feed(chunks)
    assertProperty()
  }, 120_000)

  it('split UTF-8 across chunks', async () => {
    const bytes = b('€ 𝄞 ü ' + `${ESC}]0;tïtle\x07` + ' ok')
    const chunks = Array.from(bytes, (x) => Uint8Array.of(x)) // one byte per chunk
    await feed(chunks)
    expect(view.term.buffer.active.getLine(0)?.translateToString(true)).toContain('€ 𝄞 ü')
    assertProperty()
  })

  it('a fuzz run: corpus fragments, random bytes and random splits', async () => {
    const rnd = prng(7)
    const parts = Object.values(CORPUS)
    for (let round = 0; round < 40; round++) {
      let all = new Uint8Array(0)
      for (let i = 0; i < 8; i++) {
        const piece = rnd() < 0.5 ? b(parts[Math.floor(rnd() * parts.length)].slice(0, 1 + Math.floor(rnd() * 400))) : Uint8Array.from({ length: Math.floor(rnd() * 300) }, () => Math.floor(rnd() * 256))
        const next = new Uint8Array(all.length + piece.length)
        next.set(all)
        next.set(piece, all.length)
        all = next
      }
      const chunks: Uint8Array[] = []
      for (let at = 0; at < all.length;) {
        const n = 1 + Math.floor(rnd() * 97)
        chunks.push(all.slice(at, at + n))
        at += n
      }
      await feed(chunks)
    }
    assertProperty()
  }, 120_000)
})

// A browser runs a microtask checkpoint after every event listener callback of a trusted event (HTML "clean up after
// running script"), so a microtask queued by one listener runs before the next listener of the same event. jsdom's
// dispatchEvent does not: these tests emulate the browser by running queued microtasks at once while an event is being
// dispatched (review H1: a filter that closes its window with a microtask dropped every real keystroke and paste).
function browserDispatch(target: EventTarget, event: Event) {
  const real = globalThis.queueMicrotask
  globalThis.queueMicrotask = (fn: () => void) => fn()
  try { target.dispatchEvent(event) } finally { globalThis.queueMicrotask = real }
}

describe('real xterm input, dispatched as a browser does (review H1)', () => {
  it('typing on the textarea sends tag 0 (keydown, Enter)', () => {
    const ta = view.term.textarea!
    browserDispatch(ta, new KeyboardEvent('keydown', { key: 'l', code: 'KeyL', keyCode: 76, bubbles: true, cancelable: true }))
    browserDispatch(ta, new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true, cancelable: true }))
    expect(sent.filter((s) => s.tag === 0).map((s) => s.data).join('')).toBe('l\r')
  })

  it('a paste on the textarea sends tag 0', () => {
    const paste = new Event('paste', { bubbles: true, cancelable: true }) as Event & { clipboardData: unknown }
    paste.clipboardData = { getData: (t: string) => (t === 'text/plain' ? 'echo pasted' : '') }
    browserDispatch(view.term.textarea!, paste)
    expect(sent.filter((s) => s.tag === 0).map((s) => s.data).join('')).toContain('echo pasted')
  })

  it('replies parsed in the same tick as a keystroke never get tag 0', () => {
    const ta = view.term.textarea!
    browserDispatch(ta, new KeyboardEvent('keydown', { key: 'x', code: 'KeyX', keyCode: 88, bubbles: true, cancelable: true }))
    // After user input xterm parses the next write synchronously (WriteBuffer.handleUserInput): same tick.
    view.output(1000, b(`${ESC}[>c${ESC}[5n${ESC}P$qm${ST}${ESC}[c${ESC}[6n`))
    expect(sent.filter((s) => s.tag === 0).map((s) => s.data)).toEqual(['x'])
    expect(sent.filter((s) => s.tag === 1).every((s) => isCpr(s.data) || isDa1(s.data))).toBe(true)
  })
})

describe('pointer reports are not tag 0', () => {
  it('data xterm flags as user input inside a mouse or wheel dispatch is swallowed', () => {
    const el = view.term.element!
    // Stand-ins for xterm's own mouse-tracking and wheel handlers: they send a report while the event is dispatched.
    const onMouse = () => view.term.input('\x1b[<0;5;5M', true)
    const onWheel = () => view.term.input('\x1b[A', true)
    el.addEventListener('mousedown', onMouse)
    el.addEventListener('wheel', onWheel)
    browserDispatch(el, new MouseEvent('mousedown', { bubbles: true }))
    browserDispatch(el, new WheelEvent('wheel', { bubbles: true, deltaY: 3 }))
    el.removeEventListener('mousedown', onMouse)
    el.removeEventListener('wheel', onWheel)
    expect(sent).toEqual([])
  })

  it('a key typed right after the mouse moved is not dropped (delta D-L1)', () => {
    browserDispatch(document, new MouseEvent('mousemove', { bubbles: true }))
    browserDispatch(view.term.textarea!, new KeyboardEvent('keydown', { key: 'k', code: 'KeyK', keyCode: 75, bubbles: true, cancelable: true }))
    expect(sent).toEqual([{ tag: 0, data: 'k' }])
  })
})

describe('xterm pin (delta D-L2)', () => {
  it('is exactly 5.5.0: tag 0 relies on its private user-input flag', async () => {
    // A bump needs the audit of every `triggerDataEvent(…, true)` caller in the new version (DECISIONS-LOG, M7a H1):
    // only key, paste, IME, input, mouse and wheel handlers may flag data as user input, never the parser.
    const pkg = (await import('@xterm/xterm/package.json')) as { version: string }
    expect(pkg.version).toBe('5.5.0')
  })
})

describe('the reply filter around the person', () => {
  it('a key event sends tag 0; the same bytes from the parser do not', async () => {
    const ta = view.term.textarea!
    ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', code: 'KeyA', keyCode: 65, bubbles: true }))
    ta.dispatchEvent(new KeyboardEvent('keypress', { key: 'a', charCode: 97, keyCode: 97, bubbles: true }))
    expect(sent.filter((s) => s.tag === 0).map((s) => s.data).join('')).toContain('a')
    await Promise.resolve()
    const before = sent.length
    await feed([b(`${ESC}[>c${ESC}[5n`)]) // DA2 and DSR: replies outside a gesture are swallowed
    expect(sent.slice(before)).toEqual([])
  })

  it('a paste sends tag 0', () => {
    const ta = view.term.textarea!
    const paste = new Event('paste', { bubbles: true, cancelable: true }) as Event & { clipboardData: unknown }
    paste.clipboardData = { getData: () => 'echo pasted' }
    ta.dispatchEvent(paste)
    expect(sent.filter((s) => s.tag === 0).map((s) => s.data).join('')).toContain('echo pasted')
  })

  it('a read attachment and a lost lease send nothing, even for a key', async () => {
    view.setWriter(false)
    expect(view.term.options.disableStdin).toBe(true)
    view.term.textarea!.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', keyCode: 65, bubbles: true }))
    await feed([b(`${ESC}[6n${ESC}[c`)])
    expect(sent).toEqual([])
  })

  it('during replay nothing is sent until the last replay chunk was written', async () => {
    view.ready({ start: 0, offset: 20, cols: 80, rows: 24, writer: true, state: 'running' })
    expect(view.replaying).toBe(true)
    view.output(0, b(`${ESC}[6n`)) // replay: the old query must not be answered again
    await settle()
    expect(sent).toEqual([])
    view.output(4, b('0123456789abcdef')) // 4 + 16 = 20: the last replay chunk
    await settle()
    expect(view.replaying).toBe(false)
    view.output(20, b(`${ESC}[6n`)) // live
    await settle()
    expect(sent.filter((s) => s.tag === 1).length).toBe(1)
  })

  it('a gap resets the screen and writes the page marker', async () => {
    await feed([b('before the gap')])
    view.gap(1014, 5000)
    view.output(5000, b('after'))
    await settle()
    const lines = Array.from({ length: 3 }, (_, i) => view.term.buffer.active.getLine(i)?.translateToString(true))
    expect(lines[0]).toBe('[some output was skipped]')
    expect(lines.join('\n')).not.toContain('before the gap')
    expect(lines[1]).toContain('after')
  })
})
