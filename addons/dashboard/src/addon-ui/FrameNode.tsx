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
const LOOP_MS = 500

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
  /** A feedback loop was seen: the frame keeps its height and takes no more shrinks (it still grows). */
  frozen: boolean
}
export const FIT_START: FitState = { fit: null, shrankAt: null, shrankBy: 0, streak: 0, before: 0, frozen: false }

/**
 * One reported content height → the frame's next state. Bounded to [MIN_FIT, max]; changes under 2 px are ignored.
 * A document that sizes itself from its own viewport (100vh, height:100%) reports a smaller height after every shrink
 * of the frame, which shrinks it again, round after round: a shrink that comes within LOOP_MS of the frame's own
 * shrink and is at least half as big counts as an answer to it, and the second such answer in a row stops it. The
 * frame goes back to its height before that run and takes no more shrinks, so it never ends up tiny. Pure (tested).
 */
export function stepFit(s: FitState, reported: number, max: number, now: number): FitState {
  const next = Math.max(MIN_FIT, Math.min(max, Math.ceil(reported)))
  const cur = s.fit ?? max
  if (s.fit !== null && Math.abs(cur - next) < 2) return s
  if (next >= cur) return { ...s, fit: next, streak: 0 }
  if (s.frozen) return s
  const answers = s.shrankAt !== null && now - s.shrankAt <= LOOP_MS && cur - next >= s.shrankBy / 2
  if (!answers) return { ...s, fit: next, shrankAt: now, shrankBy: cur - next, streak: 0, before: cur }
  if (s.streak + 1 >= 2) return { ...s, fit: s.before, frozen: true }
  return { ...s, fit: next, shrankAt: now, shrankBy: cur - next, streak: s.streak + 1 }
}

/** The first load is the srcdoc itself; any later load means the frame navigated somewhere else. */
function SandboxFrame({ node, srcDoc, fallback, fitContent }: { node: NodeOf<'frame'>; srcDoc: string; fallback: ReactNode; fitContent: boolean }) {
  const loads = useRef(0)
  const ref = useRef<HTMLIFrameElement>(null)
  const [navigated, setNavigated] = useState(false)
  const [fit, setFit] = useState<FitState>(FIT_START)
  // `fitContent`: the document is core's frame document, whose size reporter posts its content height. Only messages
  // from this frame's own window count, and only a number: the frame shrinks to it, never past `node.height`, and
  // never in a loop (stepFit).
  useEffect(() => {
    if (!fitContent) return
    // At most one height per animation frame (the last one wins): a chatty frame cannot make the page re-layout on
    // every message.
    let pending: number | null = null
    let raf = 0
    const apply = () => {
      raf = 0
      const next = pending
      pending = null
      if (next === null) return
      setFit((cur) => stepFit(cur, next, node.height, performance.now()))
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
