import type { NodeOf } from './nodes'

// Prepended to every addon document. No network, no nested frames; inline script/style and data: images only.
const CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:"

/**
 * Sandboxed iframe for addon-supplied HTML. `sandbox="allow-scripts"` only: never allow-same-origin (the
 * document gets an opaque origin and cannot reach core's DOM, storage or cookies), nor top-navigation,
 * popups or forms.
 */
export function FrameNode({ node }: { node: NodeOf<'frame'> }) {
  const srcDoc = `<meta http-equiv="Content-Security-Policy" content="${CSP}">${node.html}`
  return (
    <iframe
      title={node.title}
      sandbox="allow-scripts"
      srcDoc={srcDoc}
      referrerPolicy="no-referrer"
      loading="lazy"
      style={{ height: node.height }}
      className="w-full rounded-md border border-border bg-bg"
    />
  )
}
