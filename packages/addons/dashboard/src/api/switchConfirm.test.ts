import { afterEach, expect, it, vi } from 'vitest'

vi.mock('./bootstrap', () => ({ confirmSwitch: vi.fn(async () => undefined) }))
import { confirmSwitch } from './bootstrap'
import { showSwitchConfirm, showSwitchFailed } from './switchConfirm'

afterEach(() => { document.body.innerHTML = ''; vi.clearAllMocks() })

it('asks "Open <name>?" in plain words with one button, Open', () => {
  const root = document.createElement('div')
  showSwitchConfirm('Product', root)
  expect(root.querySelector('b')!.textContent).toBe('Open Product?')
  expect(root.textContent).toContain('This browser could not be verified here.')
  expect([...root.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['Open'])
  expect(root.querySelector('input')).toBeNull() // nothing to type
  expect(root.textContent).not.toContain('Continue from your hub')
})

it('without a name it still asks plainly', () => {
  const root = document.createElement('div')
  showSwitchConfirm(null, root)
  expect(root.querySelector('b')!.textContent).toBe('Open this workspace?')
})

it('Open with a link that is no longer good shows the error page', async () => {
  const root = document.createElement('div')
  showSwitchConfirm('Product', root)
  root.querySelector('button')!.click()
  await vi.waitFor(() => expect(root.textContent).toContain('This link expired'))
  expect(confirmSwitch).toHaveBeenCalledTimes(1)
})

it('the error page opens nothing by itself', () => {
  const root = document.createElement('div')
  showSwitchFailed(root)
  expect(root.textContent).toContain('This link expired')
  expect(confirmSwitch).not.toHaveBeenCalled()
})
