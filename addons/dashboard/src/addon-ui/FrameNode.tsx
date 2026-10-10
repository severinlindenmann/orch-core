import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { NodeOf } from './nodes'

// Prepended to every addon document. It blocks fetches, subresources and nested frames (inline script/style and
// data: images only). It does NOT govern navigation: the document can still navigate its own frame to a remote
// URL, which is why SandboxFrame drops the frame after its first load.
const CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:"

/**
 * Sandboxed iframe for addon-supplied HTML. `sandbox="allow-scripts"` only: never allow-same-origin (the
 * document gets an opaque origin and cannot reach core's DOM, storage or cookies), nor top-navigation,
 * popups or forms. `fallback` replaces the frame if it navigates away from the document core gave it.
 */
export function FrameNode({ node, fallback, fitContent = false }: { node: NodeOf<'frame'>; fallback: ReactNode; fitContent?: boolean }) {
  const srcDoc = `<meta http-equiv="Content-Security-Policy" content="${CSP}">${node.html}`
  // A new document is a new frame: its load count starts again.
  return <SandboxFrame key={srcDoc} node={node} srcDoc={srcDoc} fallback={fallback} fitContent={fitContent} />
}

/** The smallest height a fitted frame takes, whatever it reports. */
export const MIN_FIT = 48
/** A shrink reported this soon after the frame's own shrink is taken as the document answering that shrink. */
export const LOOP_MS = 500
/** Reports this long without a new one mean the document's height is stable: a held shrink is tried once (settleFit). */
export const QUIET_MS = 500
/** Changes of direction (grow, shrink, grow, ...) within LOOP_MS of each other that count as an oscillation. */
const FLIPS = 3

/** How a fitted frame follows its document's reported heights (see `stepFit`). */
export interface FitState {
  /** The height applied; null until the first report (the frame then has `node.height`). */
  fit: number | null
  /** When the last shrink was applied, and by how much. */
  shrankAt: number | null
  shrankBy: number
  /** Shrinks in a row that answered the frame's own shrink, and the height before the first shrink of that run. */
  streak: number
  before: number
  /** The last change: its direction and time, and how often the direction flipped in a row. */
  dir: -1 | 0 | 1
  changedAt: number
  flips: number
  /** The frame takes no shrinks for now (it still grows); `held` is the last shrink it did not take. */
  frozen: boolean
  held: number | null
  /** When the last report came. */
  reportAt: number
  /** The held shrink is being tried after a quiet spell; a shrink that answers it confirms a loop. */
  probing: boolean
  /** A loop or an oscillation was confirmed: frozen for good (for this document). */
  confirmed: boolean
}
export const FIT_START: FitState = { fit: null, shrankAt: null, shrankBy: 0, streak: 0, before: 0, dir: 0, changedAt: 0, flips: 0, frozen: false, held: null, reportAt: 0, probing: false, confirmed: false }

/**
 * One reported height → the frame's next state. `reported` is the height the frame needs, its own border included
 * (the caller adds it), so a document sized from its viewport is a fixed point. Bounded to [MIN_FIT, max]; changes
 * under 2 px are ignored. Backstops, for documents that still answer the frame's size (agent HTML in 90vh, a
 * scrollbar that comes and goes):
 * - a shrink loop: a shrink within LOOP_MS of the frame's own shrink and at least half as big counts as an answer to
 *   it; the second answer in a row puts the frame back to its height before that run and holds further shrinks. After
 *   QUIET_MS without reports the held shrink is tried once (settleFit): a document that only settled in steps ends at
 *   its final height, a loop answers again and stays frozen at the larger height for good;
 * - an oscillation: FLIPS changes of direction in a row, each within LOOP_MS, settle on the larger height for good.
 * Pure (tested).
 */
export function stepFit(s0: FitState, reported: number, max: number, now: number): FitState {
  const s = { ...s0, reportAt: now }
  const next = Math.max(MIN_FIT, Math.min(max, Math.ceil(reported)))
  const cur = s.fit ?? max
  if (s.fit !== null && Math.abs(cur - next) < 2) return { ...s, held: null }
  const dir = next > cur ? 1 : -1
  const recent = now - s.changedAt <= LOOP_MS
  const flips = s.dir !== 0 && dir !== s.dir && recent ? s.flips + 1 : dir === s.dir && recent ? s.flips : 0
  if (flips >= FLIPS) return { ...s, fit: Math.max(cur, next), frozen: true, confirmed: true, held: null, probing: false }
  const moved = { dir, changedAt: now, flips } as const
  if (dir > 0) return { ...s, ...moved, fit: next, streak: 0 }
  if (s.frozen) return { ...s, held: next }
  const answers = s.shrankAt !== null && now - s.shrankAt <= LOOP_MS && cur - next >= s.shrankBy / 2
  if (s.probing && s.shrankAt !== null && now - s.shrankAt <= LOOP_MS) return { ...s, fit: s.before, frozen: true, confirmed: true, held: null, probing: false }
  if (!answers) return { ...s, ...moved, fit: next, shrankAt: now, shrankBy: cur - next, streak: 0, before: cur, probing: false }
  if (s.streak + 1 >= 2) return { ...s, fit: s.before, frozen: true, held: next }
  return { ...s, ...moved, fit: next, shrankAt: now, shrankBy: cur - next, streak: s.streak + 1 }
}

/** After QUIET_MS without reports, a held shrink (not a confirmed loop) is tried once; see stepFit. */
export function settleFit(s: FitState, max: number, now: number): FitState {
  if (!s.frozen || s.confirmed || s.held === null || now - s.reportAt < QUIET_MS) return s
  const cur = s.fit ?? max
  return { ...s, fit: s.held, held: null, frozen: false, probing: true, shrankAt: now, shrankBy: cur - s.held, streak: 0, before: cur }
}

/**
 * The last height each fitted document settled at (G4), per frame document, for this page load. A frame opens at its
 * remembered height instead of `node.height`, so on a second visit nothing below it moves when its size report
 * arrives. Bounded; the oldest entries go first.
 */
const fitMemory = new Map<string, number>()
const FIT_MEMORY_MAX = 200
/** A short key for a document (FNV-1a over its text, plus its length). */
export function fitKey(srcDoc: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < srcDoc.length; i++) h = Math.imul(h ^ srcDoc.charCodeAt(i), 0x01000193)
  return `${srcDoc.length}:${(h >>> 0).toString(36)}`
}
export function rememberedFit(srcDoc: string): number | undefined {
  return fitMemory.get(fitKey(srcDoc))
}
function rememberFit(srcDoc: string, height: number) {
  const k = fitKey(srcDoc)
  fitMemory.delete(k)
  fitMemory.set(k, height)
  if (fitMemory.size > FIT_MEMORY_MAX) fitMemory.delete(fitMemory.keys().next().value!)
}
/** Tests only. */
export function forgetFits() {
  fitMemory.clear()
}

/** The first load is the srcdoc itself; any later load means the frame navigated somewhere else. */
function SandboxFrame({ node, srcDoc, fallback, fitContent }: { node: NodeOf<'frame'>; srcDoc: string; fallback: ReactNode; fitContent: boolean }) {
  const loads = useRef(0)
  const ref = useRef<HTMLIFrameElement>(null)
  const [navigated, setNavigated] = useState(false)
  // A document seen before starts at the height it settled at then (null: `node.height` until its first report).
  const [fit, setFit] = useState<FitState>(() => {
    const known = fitContent ? rememberedFit(srcDoc) : undefined
    return known === undefined ? FIT_START : { ...FIT_START, fit: Math.min(node.height, known) }
  })
  useEffect(() => {
    // Only a height the frame settled at: not a probe that may be rolled back, nor while a shrink is held.
    if (fitContent && fit.fit !== null && !fit.probing && fit.held === null) rememberFit(srcDoc, fit.fit)
  }, [fitContent, srcDoc, fit.fit, fit.probing, fit.held])
  // `fitContent`: the document is core's frame document, whose size reporter posts its content height. Only messages
  // from this frame's own window count, and only a number: the frame shrinks to it, never past `node.height`, and
  // never in a loop (stepFit).
  useEffect(() => {
    if (!fitContent) return
    // At most one height per animation frame (the last one wins): a chatty frame cannot make the page re-layout on
    // every message.
    let pending: number | null = null
    let raf = 0
    let quiet = 0
    const apply = () => {
      raf = 0
      const next = pending
      pending = null
      if (next === null) return
      // The frame's own border (top + bottom): the document reports its content, the frame's height includes the border.
      const el = ref.current
      const border = el ? el.offsetHeight - el.clientHeight : 0
      setFit((cur) => stepFit(cur, next + Math.max(0, border), node.height, performance.now()))
      clearTimeout(quiet)
      quiet = window.setTimeout(() => setFit((cur) => settleFit(cur, node.height, performance.now())), QUIET_MS + 20)
    }
    const onMessage = (e: MessageEvent) => {
      if (!ref.current || e.source !== ref.current.contentWindow) return
      const h = (e.data as { orch?: unknown; height?: unknown } | null)?.height
      if ((e.data as { orch?: unknown } | null)?.orch !== 'size' || typeof h !== 'number' || !Number.isFinite(h)) return
      pending = h
      if (!raf) raf = requestAnimationFrame(apply)
    }
    window.addEventListener('message', onMessage)
    return () => {
      window.removeEventListener('message', onMessage)
      if (raf) cancelAnimationFrame(raf)
      clearTimeout(quiet)
    }
  }, [fitContent, node.height])
  if (navigated) return <>{fallback}</>
  return (
    <iframe
      ref={ref}
      title={node.title}
      sandbox="allow-scripts"
      srcDoc={srcDoc}
      referrerPolicy="no-referrer"
      loading="lazy"
      style={{ height: fit.fit ?? node.height }}
      className="w-full rounded-md border border-border bg-bg"
      onLoad={() => {
        loads.current += 1
        if (loads.current > 1) setNavigated(true)
      }}
    />
  )
}
