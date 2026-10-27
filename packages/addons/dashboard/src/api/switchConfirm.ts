import { confirmSwitch, declineSwitch } from './bootstrap'

/**
 * A hub switch link was opened here. Nobody is signed in by a link alone (a stranger could send you one): this plain
 * page asks first. DOM only, like the boot failure page: no framework runs and the CSP allows no inline script.
 */
export function showSwitchConfirm(root: HTMLElement | null = document.getElementById('root')) {
  if (!root) return
  const box = document.createElement('div')
  box.className = 'boot'
  box.setAttribute('role', 'dialog')
  const title = document.createElement('b')
  title.textContent = 'Continue from your hub?'
  const why = document.createElement('span')
  why.textContent = 'You followed a link from your company hub. Only continue if you just clicked it there.'
  const go = document.createElement('button')
  go.type = 'button'
  go.className = 'boot-retry'
  go.textContent = 'Continue'
  const cancel = document.createElement('button')
  cancel.type = 'button'
  cancel.className = 'boot-retry'
  cancel.textContent = 'Cancel'
  go.addEventListener('click', () => {
    go.disabled = cancel.disabled = true
    void confirmSwitch().then((to) => {
      if (to === undefined) {
        title.textContent = 'This link expired'
        why.textContent = 'Go back to the hub and click again.'
        box.replaceChildren(title, why, cancel)
        cancel.disabled = false
        cancel.textContent = 'Open the dashboard'
      } else location.replace(to ?? '/')
    }).catch(() => { title.textContent = 'This link could not be used'; cancel.disabled = false })
  })
  cancel.addEventListener('click', () => { declineSwitch(); location.replace('/') })
  box.append(title, why, go, cancel)
  root.replaceChildren(box)
}
