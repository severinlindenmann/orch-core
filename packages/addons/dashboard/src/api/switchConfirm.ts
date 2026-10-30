import { confirmSwitch, switchFailureCode } from './bootstrap'

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
  why.textContent = 'This is the first time this browser opens it. One click and you are in.'
  const go = document.createElement('button')
  go.type = 'button'
  go.className = 'boot-retry'
  go.textContent = 'Open workspace'
  go.addEventListener('click', () => {
    go.disabled = true
    go.textContent = 'Opening…'
    void confirmSwitch().then((to) => {
      if (to === undefined) showSwitchFailed(root)
      else location.replace(to ?? '/')
    }).catch(() => showSwitchFailed(root))
  })
  box.append(title, why, go)
  root.replaceChildren(box)
}

const FAILED_WHY: Record<string, string> = {
  'switch.refused': 'Open this workspace on its own computer. It shares only chosen items with the hub, or it runs at a customer\'s place.',
  'rate.limited': 'Too many tries. Wait a minute, then open it from the hub again.',
}
const FAILED_DEFAULT = 'This link was already used or has expired. Nothing was changed.'

/** Where the person came from, when that was another website (the hub): its origin only, never a path or a query. */
function hubOrigin(): string | null {
  try {
    const u = new URL(document.referrer)
    return (u.protocol === 'https:' || u.protocol === 'http:') && u.origin !== location.origin ? u.origin : null
  } catch { return null }
}

/** The link is not good (expired, already used, refused on purpose): an error page that says the real reason and always
 * offers a way on. Nothing is opened and nobody is signed in. */
export function showSwitchFailed(root: HTMLElement | null = document.getElementById('root'), code: string | null = switchFailureCode()) {
  if (!root) return
  const box = document.createElement('div')
  box.className = 'boot'
  box.setAttribute('role', 'alert')
  const title = document.createElement('b')
  title.textContent = 'This workspace did not open'
  const why = document.createElement('span')
  why.textContent = (code && FAILED_WHY[code]) || FAILED_DEFAULT
  box.append(title, why)
  const hub = hubOrigin()
  if (hub) {
    const back = document.createElement('button')
    back.type = 'button'
    back.className = 'boot-retry'
    back.textContent = 'Back to the hub'
    back.addEventListener('click', () => location.assign(hub + '/'))
    box.append(back)
  }
  const again = document.createElement('button')
  again.type = 'button'
  again.className = 'boot-retry'
  again.textContent = 'Sign in again'
  again.addEventListener('click', () => location.replace('/'))
  box.append(again)
  root.replaceChildren(box)
}
