import { confirmSwitch } from './bootstrap'

/**
 * A hub switch link was opened here and this browser could not prove its key for this dashboard (a fresh profile, a
 * browser that never signed in on this computer, or a link meant for someone else). Nobody is signed in by a link
 * alone, and the ticket is still unused: this plain page asks once, "Open <name>?", with one button. DOM only, like the
 * boot failure page: no framework runs and the CSP allows no inline script.
 */
export function showSwitchConfirm(name: string | null, root: HTMLElement | null = document.getElementById('root')) {
  if (!root) return
  const box = document.createElement('div')
  box.className = 'boot'
  box.setAttribute('role', 'dialog')
  const title = document.createElement('b')
  title.textContent = `Open ${name ?? 'this workspace'}?`
  const why = document.createElement('span')
  why.textContent = 'This browser could not be verified here.'
  const go = document.createElement('button')
  go.type = 'button'
  go.className = 'boot-retry'
  go.textContent = 'Open'
  go.addEventListener('click', () => {
    go.disabled = true
    void confirmSwitch().then((to) => {
      if (to === undefined) showSwitchFailed(root)
      else location.replace(to ?? '/')
    }).catch(() => showSwitchFailed(root))
  })
  box.append(title, why, go)
  root.replaceChildren(box)
}

/** The link is not good (expired, already used, or not for this dashboard's person): an error page, nothing opened. */
export function showSwitchFailed(root: HTMLElement | null = document.getElementById('root')) {
  if (!root) return
  const box = document.createElement('div')
  box.className = 'boot'
  box.setAttribute('role', 'alert')
  const title = document.createElement('b')
  title.textContent = 'This link expired'
  const why = document.createElement('span')
  why.textContent = 'Go back to the hub and click again.'
  const home = document.createElement('button')
  home.type = 'button'
  home.className = 'boot-retry'
  home.textContent = 'Open the dashboard'
  home.addEventListener('click', () => location.replace('/'))
  box.append(title, why, home)
  root.replaceChildren(box)
}
