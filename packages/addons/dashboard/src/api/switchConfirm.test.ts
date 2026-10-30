import { afterEach, expect, it, vi } from 'vitest'

vi.mock('./bootstrap', () => ({ confirmSwitch: vi.fn(async () => undefined), switchFailureCode: vi.fn(() => null) }))
import { confirmSwitch } from './bootstrap'
import { showSwitchConfirm, showSwitchFailed } from './switchConfirm'

afterEach(() => { document.body.innerHTML = ''; vi.clearAllMocks() })

it('asks "Open <name>?" in plain words with one button, Open', () => {
  const root = document.createElement('div')
  showSwitchConfirm('Product', root)
  expect(root.querySelector('b')!.textContent).toBe('Open Product?')
  expect(root.textContent).toContain('This is the first time this browser opens it. One click and you are in.')
  expect([...root.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['Open workspace'])
  expect(root.querySelector('input')).toBeNull() // nothing to type
  expect(root.textContent).not.toContain('Continue from your hub')
  expect(root.textContent).not.toContain('verified')
})

it('without a name it still asks plainly', () => {
  const root = document.createElement('div')
  showSwitchConfirm(null, root)
  expect(root.querySelector('b')!.textContent).toBe('Open this workspace?')
})

it('after the click the button reads Opening… and stays disabled, nothing continues on its own', async () => {
  vi.mocked(confirmSwitch).mockReturnValueOnce(new Promise(() => {}))
  const root = document.createElement('div')
  showSwitchConfirm('Product', root)
  expect(confirmSwitch).not.toHaveBeenCalled()
  const b = root.querySelector('button')!
  b.click()
  expect(b.textContent).toBe('Opening…')
  expect(b.disabled).toBe(true)
  expect(confirmSwitch).toHaveBeenCalledTimes(1)
})

it('Open with a link that is no longer good shows the error page', async () => {
  const root = document.createElement('div')
  showSwitchConfirm('Product', root)
  root.querySelector('button')!.click()
  await vi.waitFor(() => expect(root.textContent).toContain('already used or has expired'))
  expect(confirmSwitch).toHaveBeenCalledTimes(1)
})

it('the error page opens nothing by itself', () => {
  const root = document.createElement('div')
  showSwitchFailed(root)
  expect(root.textContent).toContain('already used or has expired')
  expect(confirmSwitch).not.toHaveBeenCalled()
})

it('run-15 J02: a switch the workspace refuses by design says to open it on its own computer, not that the link broke', () => {
  const root = document.createElement('div')
  showSwitchFailed(root, 'switch.refused')
  expect(root.textContent).toContain('Open this workspace on its own computer')
  expect(root.textContent).toContain('shares only chosen items with the hub')
  expect(root.textContent).not.toContain('already used')
})

it('P01: every code gives a plain line and a way on: Sign in again always, Back to the hub only when the hub sent us', () => {
  for (const code of ['switch.refused', 'session.required', 'origin.bad', 'rate.limited', null]) {
    const root = document.createElement('div')
    showSwitchFailed(root, code)
    expect(root.querySelector('b')!.textContent).toBe('This workspace did not open')
    expect([...root.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['Sign in again']) // jsdom: no referrer
    expect(root.textContent).not.toMatch(/orch dashboard|terminal/i)
  }
  Object.defineProperty(document, 'referrer', { value: 'https://hub.example.org/path?x=1', configurable: true })
  const root = document.createElement('div')
  showSwitchFailed(root, 'session.required')
  expect([...root.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['Back to the hub', 'Sign in again'])
  Object.defineProperty(document, 'referrer', { value: '', configurable: true })
})
