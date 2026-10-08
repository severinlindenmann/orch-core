import { useRef, useState, type ReactNode } from 'react'
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
export function FrameNode({ node, fallback }: { node: NodeOf<'frame'>; fallback: ReactNode }) {
  const srcDoc = `<meta http-equiv="Content-Security-Policy" content="${CSP}">${node.html}`
  // A new document is a new frame: its load count starts again.
  return <SandboxFrame key={srcDoc} node={node} srcDoc={srcDoc} fallback={fallback} />
}

/** The first load is the srcdoc itself; any later load means the frame navigated somewhere else. */
function SandboxFrame({ node, srcDoc, fallback }: { node: NodeOf<'frame'>; srcDoc: string; fallback: ReactNode }) {
  const loads = useRef(0)
  const [navigated, setNavigated] = useState(false)
  if (navigated) return <>{fallback}</>
  return (
    <iframe
      title={node.title}
      sandbox="allow-scripts"
      srcDoc={srcDoc}
      referrerPolicy="no-referrer"
      loading="lazy"
      style={{ height: node.height }}
      className="w-full rounded-md border border-border bg-bg"
      onLoad={() => {
        loads.current += 1
        if (loads.current > 1) setNavigated(true)
      }}
    />
  )
}
