/**
 * One-shot request to open the "Save view" dialog, sent by the command palette. It is not a URL param on
 * purpose: saved views are exactly the search params, and a flag there would end up inside every view.
 */
let pending = false
const EVENT = 'orch:save-view'

export function requestSaveView() {
  pending = true
  window.dispatchEvent(new Event(EVENT))
}

/** Consumes a request made before the page mounted; returns whether there was one. */
export function takeSaveViewRequest(): boolean {
  const had = pending
  pending = false
  return had
}

export function onSaveViewRequest(fn: () => void): () => void {
  const h = () => {
    if (takeSaveViewRequest()) fn()
  }
  window.addEventListener(EVENT, h)
  return () => window.removeEventListener(EVENT, h)
}
