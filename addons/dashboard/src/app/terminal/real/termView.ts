// xterm.js for a real terminal, configured as contract section 12.6.7 requires (testable without React): no proposed
// API, no window reports, the explicit no-op link handler, Fit and Search only, 5000 lines of scrollback, no EOL
// conversion, no title consumer. Output goes in by offset; replies come out only through the page-side filter.

import { FitAddon } from '@xterm/addon-fit'
import { SearchAddon } from '@xterm/addon-search'
import { Terminal, type ITerminalInitOnlyOptions, type ITerminalOptions } from '@xterm/xterm'
import { POINTER_EVENTS, ReplyFilter, type Ready } from './termProtocol'

/** xterm's default OSC 8 behaviour would confirm() and window.open(): links do nothing here (no links in phase 2). */
export const NO_LINKS = { activate: () => {}, hover: () => {}, leave: () => {} }

/** Written by the page, never composed from host strings: dimmed, on its own line. */
export const GAP_MARKER = '\x1b[0m\x1b[2m[some output was skipped]\x1b[0m\r\n'
export const RING_MARKER = '\x1b[0m\x1b[2m[earlier output is no longer kept]\x1b[0m\r\n'

export function termOptions(extra: { fontSize?: number; theme?: ITerminalOptions['theme'] } = {}): ITerminalOptions & ITerminalInitOnlyOptions {
  return {
    allowProposedApi: false,
    windowOptions: {},
    linkHandler: NO_LINKS,
    scrollback: 5000,
    convertEol: false,
    disableStdin: true,
    cursorBlink: false,
    fontFamily: 'Geist Mono, ui-monospace, monospace',
    fontSize: extra.fontSize ?? 13,
    ...(extra.theme ? { theme: extra.theme } : {}),
  }
}

export type Sink = (tag: 0 | 1, bytes: Uint8Array) => void

export interface TermView {
  term: Terminal
  search: SearchAddon
  /** A new attachment: reset, size, replay mode. */
  ready(r: Ready): void
  output(off: number, bytes: Uint8Array): void
  gap(from: number, to: number): void
  /** The writer's size (a read-only viewer renders at it and scrolls; it never fits). */
  resize(cols: number, rows: number): void
  /** Do we hold the write lease? Without it stdin is disabled and nothing is sent. */
  setWriter(writer: boolean): void
  /** Fit to the container (writer only); the new size, or null when unchanged or not measurable. */
  fit(): { cols: number; rows: number } | null
  setSink(sink: Sink | null): void
  readonly replaying: boolean
  dispose(): void
}

export function createTermView(el: HTMLElement, options: ITerminalOptions & ITerminalInitOnlyOptions = termOptions()): TermView {
  const term = new Terminal(options)
  const fitAddon = new FitAddon()
  const search = new SearchAddon()
  term.loadAddon(fitAddon)
  term.loadAddon(search)
  term.open(el)
  const filter = new ReplyFilter()
  const encoder = new TextEncoder()
  let writer = false
  let replaying = false
  let readyOffset = 0
  let sink: Sink | null = null
  let disposed = false
  // Tag 0 needs the person's own input. xterm knows which data is that: its key, paste, IME and input handlers call
  // `coreService.triggerDataEvent(data, true)`, which fires `onUserInput` synchronously right before `onData`; the
  // parser's replies call it without the flag. So the flag is taken from xterm's own dispatch (review H1: an
  // event-timing window closed by a microtask dropped every real keystroke, because a browser runs microtasks between
  // listeners). The core service is not public API: xterm is pinned by the lockfile, and a version without it fails
  // here (no terminal at all) instead of guessing; the canary corpus test guards the property.
  const core = (term as unknown as { _core?: { coreService?: { onUserInput?: (fn: () => void) => { dispose(): void } } } })._core
  const onUserInput = core?.coreService?.onUserInput
  if (typeof onUserInput !== 'function') {
    term.dispose()
    throw new Error('xterm has no user-input signal: the terminal is not drawn')
  }
  let user = false
  // Pointer/wheel reports are flagged as user input too: data flagged while one is dispatched is not tag 0. The flag
  // is cleared by a task (not a microtask): widening this window only ever swallows more.
  let pointer = false
  let pointerTimer: ReturnType<typeof setTimeout> | undefined
  const markPointer = () => {
    pointer = true
    clearTimeout(pointerTimer)
    pointerTimer = setTimeout(() => { pointer = false }, 0)
  }
  const doc = el.ownerDocument
  for (const name of POINTER_EVENTS) doc.addEventListener(name, markPointer, true)
  const userInput = onUserInput.call(core!.coreService, () => { user = !pointer })
  const data = term.onData((d) => {
    const fromUser = user
    user = false
    const out = filter.classify(d, { writer, replaying, user: fromUser })
    if (out && sink) sink(out.tag, encoder.encode(out.data))
  })
  // Binary reports (X10 mouse) are terminal-generated and never a CPR/DA1: dropped.
  const binary = term.onBinary(() => {})
  /** Reset in write order: queued output before this point is parsed first, then the screen is cleared. */
  const resetThen = (marker: string, done?: () => void) => {
    term.write('', () => { if (!disposed) term.reset() })
    term.write(marker, done)
  }
  return {
    term,
    search,
    get replaying() { return replaying },
    ready(r) {
      readyOffset = r.offset
      replaying = r.offset > r.start
      if (r.cols !== term.cols || r.rows !== term.rows) term.resize(r.cols, r.rows)
      // A replay starts with a clean screen; a ring that no longer starts at 0 says so first.
      resetThen(r.start > 0 && replaying ? RING_MARKER : '')
    },
    output(off, bytes) {
      // Below ready.offset is replay; the write callback of the chunk that reaches it ends the replay (written in
      // order, so every replay chunk has been parsed by then).
      const reaches = off + bytes.length >= readyOffset
      term.write(bytes, () => { if (reaches) replaying = false })
    },
    gap(_from, to) {
      const ends = to >= readyOffset
      resetThen(GAP_MARKER, () => { if (ends) replaying = false })
    },
    resize(cols, rows) {
      if (!writer && (cols !== term.cols || rows !== term.rows)) term.resize(cols, rows)
    },
    setWriter(next) {
      writer = next
      term.options.disableStdin = !next
      term.options.cursorBlink = next
    },
    fit() {
      if (!writer) return null
      const dims = fitAddon.proposeDimensions()
      if (!dims || !Number.isFinite(dims.cols) || !Number.isFinite(dims.rows)) return null
      const cols = Math.min(500, Math.max(20, dims.cols))
      const rows = Math.min(200, Math.max(5, dims.rows))
      if (cols === term.cols && rows === term.rows) return null
      term.resize(cols, rows)
      return { cols, rows }
    },
    setSink(next) { sink = next },
    dispose() {
      disposed = true
      sink = null
      for (const name of POINTER_EVENTS) doc.removeEventListener(name, markPointer, true)
      clearTimeout(pointerTimer)
      userInput.dispose()
      data.dispose()
      binary.dispose()
      // xterm schedules frame work after open; disposing before it ran throws in StrictMode remounts.
      const dying = term
      dying.write('', () => setTimeout(() => requestAnimationFrame(() => requestAnimationFrame(() => dying.dispose())), 0))
    },
  }
}
