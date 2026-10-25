// The one-time code of an "Add this device" link (`/#enroll=<code>`). Memory and the address bar only, never storage.
// The code stays in the address bar while the page is open (a reload after turning on a screen lock still works) and
// leaves it when the link is used or given up.
let code: string | null = null
export const getEnrollCode = () => code
export function setEnrollCode(value: string | null) {
  code = value && /^[A-Za-z0-9_-]{16,128}$/.test(value) ? value : null
}
export function finishEnroll() {
  code = null
  history.replaceState(null, '', location.pathname + location.search)
}
