// "Open the dock on this session": an action that opened a terminal session (e.g. Worktrees' "Open terminal here")
// answers with its id, and core's dock opens on it with the keyboard in it. Light (entry chunk): a window event only.

const EVENT = 'orch:dock-open'

/** Ask the dock to open and select `session` (the terminals addon's session id). */
export function openDockOn(session: string): void {
  window.dispatchEvent(new CustomEvent<string>(EVENT, { detail: session }))
}

/** DockArea's side: called with each requested session id. */
export function onDockRequest(fn: (session: string) => void): () => void {
  const h = (e: Event) => {
    const id = (e as CustomEvent<unknown>).detail
    if (typeof id === 'string' && id) fn(id)
  }
  window.addEventListener(EVENT, h)
  return () => window.removeEventListener(EVENT, h)
}
