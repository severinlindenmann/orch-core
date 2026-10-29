import { render, screen, fireEvent } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { SignShell, SignFooter, STICKY_FOOTER } from './SignShell'

afterEach(() => vi.unstubAllGlobals())

it.each(['dialog', 'sheet', 'popover'] as const)('%s keeps actions outside the scrolling review when details expand', mode => {
  vi.stubGlobal('matchMedia', () => ({ matches: mode !== 'sheet', addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  render(<SignShell anchor={mode === 'popover' ? document.body : null} open title="Allow access" showClose onDismiss={vi.fn()} onOpenAutoFocus={e => e.preventDefault()}>
    <p>Access lasts one day.</p>
    <details><summary>What you sign</summary><p>{'Signed text. '.repeat(500)}</p></details>
    <section><SignFooter><div className={STICKY_FOOTER}><button>Cancel</button><button>Sign</button></div></SignFooter></section>
  </SignShell>)
  expect(screen.getAllByRole('heading', { name: 'Allow access' })).toHaveLength(1)
  const cancel = screen.getByRole('button', { name: 'Cancel' })
  const slot = cancel.closest('[data-sign-footer-slot]')!
  expect(slot).not.toBeNull()
  expect(cancel.closest('[data-sign-scroll]')).toBeNull()
  expect(slot.parentElement).toHaveClass('flex', 'flex-col', 'overflow-hidden')
  expect(slot.parentElement?.querySelector('[data-sign-scroll]')).toHaveClass('min-h-0', 'overflow-y-auto')
  expect(screen.getByText('What you sign').closest('details')).not.toHaveAttribute('open')
  fireEvent.click(screen.getByText('What you sign'))
  expect(cancel).toBeVisible()
  expect(screen.getByRole('button', { name: 'Sign' })).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Close' })).toBeNull()
})
