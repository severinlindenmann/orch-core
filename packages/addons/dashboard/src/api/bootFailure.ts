/**
 * The boot screen ("orch / Loading…", index.html) is replaced by React once the app's code has loaded. When that code
 * cannot load at all (a stale page after the app was updated: its file names are gone; the server stopped; a blocked
 * request) nothing else would ever replace it, and the person would watch "Loading…" for good. This puts a plain
 * message and a button in its place. DOM only: no framework is running, and the page's CSP allows no inline script.
 */
export function showBootFailure(root: HTMLElement | null = document.getElementById('root')) {
  if (!root) return
  const box = document.createElement('div')
  box.className = 'boot'
  box.setAttribute('role', 'alert')
  const title = document.createElement('b')
  title.textContent = 'The dashboard could not start'
  const why = document.createElement('span')
  why.textContent = 'The page may be out of date, or the dashboard server stopped. If it keeps happening, run orch dashboard open in your terminal.'
  const button = document.createElement('button')
  button.type = 'button'
  button.className = 'boot-retry'
  button.textContent = 'Try again'
  button.addEventListener('click', () => location.reload())
  box.append(title, why, button)
  root.replaceChildren(box)
}
