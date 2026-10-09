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
const MIN_FIT = 48

/** The first load is the srcdoc itself; any later load means the frame navigated somewhere else. */
function SandboxFrame({ node, srcDoc, fallback, fitContent }: { node: NodeOf<'frame'>; srcDoc: string; fallback: ReactNode; fitContent: boolean }) {
  const loads = useRef(0)
  const ref = useRef<HTMLIFrameElement>(null)
  const [navigated, setNavigated] = useState(false)
  const [fit, setFit] = useState<number | null>(null)
  // `fitContent`: the document is core's frame document, whose size reporter posts its content height. Only messages
  // from this frame's own window count, and only a number: the frame shrinks to it, never past `node.height`.
  useEffect(() => {
    if (!fitContent) return
    const onMessage = (e: MessageEvent) => {
      if (!ref.current || e.source !== ref.current.contentWindow) return
      const h = (e.data as { orch?: unknown; height?: unknown } | null)?.height
      if ((e.data as { orch?: unknown } | null)?.orch !== 'size' || typeof h !== 'number' || !Number.isFinite(h)) return
      setFit(Math.max(MIN_FIT, Math.min(node.height, Math.ceil(h))))
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
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
      style={{ height: fit ?? node.height }}
      className="w-full rounded-md border border-border bg-bg"
      onLoad={() => {
        loads.current += 1
        if (loads.current > 1) setNavigated(true)
      }}
    />
  )
}
