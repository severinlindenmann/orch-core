import type { Terminal } from '@xterm/xterm'

/** Dispose synchronously, including the pinned xterm 5.x viewport's unfinished refresh. */
export function disposeTerminal(term: Terminal) {
  // xterm's Viewport does not cancel its refresh frame or guard syncScrollArea after disposal.
  // Its constructor timer and reset frame also retain no cancellable handle. Neutralize their
  // entry point before destroying the renderer, instead of deferring disposal across two frames.
  // Keep this private-API workaround covered by tests using real xterm when upgrading it.
  const viewport = (term as unknown as { _core?: { viewport?: {
    syncScrollArea: () => void
    _refreshAnimationFrame: number | null
  } } })._core?.viewport
  if (viewport) {
    viewport.syncScrollArea = () => {}
    if (viewport._refreshAnimationFrame != null) {
      cancelAnimationFrame(viewport._refreshAnimationFrame)
      viewport._refreshAnimationFrame = null
    }
  }
  term.dispose()
}
