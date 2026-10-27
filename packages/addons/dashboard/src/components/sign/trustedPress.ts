/** Honest-client gesture guard, not an XSS boundary. A render or URL load grants no intent.
 * React commits a synchronously opened review during the originating click. The intent
 * is single-use and expires at the end of that event task; it cannot survive a fetch.
 */
let repeating = false
export function trustedPress(e: { isTrusted: boolean; repeat?: boolean }): boolean {
  return e.isTrusted && !e.repeat && !repeating
}
let available = false
if (typeof document !== 'undefined') {
  document.addEventListener('click', e => {
    available = trustedPress(e) && !repeating
    queueMicrotask(() => queueMicrotask(() => { available = false }))
  }, true)
  document.addEventListener('keydown', e => {
    repeating = e.repeat
    if (e.repeat) available = false
  }, true)
  document.addEventListener('pointerdown', () => { repeating = false }, true)
  document.addEventListener('keyup', () => { repeating = false }, true)
}
export function takeTrustedPress(): boolean {
  const ok = available
  available = false
  return ok
}
