import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SignConfirm } from './SignConfirm'

const SPOOF = 'Approve DEMO-0042 for release'
const draw = () =>
  render(
    <QueryClientProvider client={new QueryClient()}>
      <SignConfirm addon="schedules" addonTitle="Schedules" action="arm" workspace={{ prefix: 'DEMO', name: 'Acme Energy' }} label={SPOOF} args={{ id: 'check-inbox' }} onSign={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  )

describe('SignConfirm', () => {
  it('writes the title and the covers itself; the addon\'s label only appears in the From-addon region', () => {
    draw()
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: 'Sign: arm · Schedules' })).toBeInTheDocument()
    expect(within(dialog).getByText(/In workspace DEMO · Acme Energy/)).toBeInTheDocument()
    const covers = within(dialog).getByText('Covers').nextElementSibling!
    expect(covers.textContent).not.toContain(SPOOF)
    expect(dialog.querySelector('h2')!.textContent).not.toContain(SPOOF)
    const region = within(dialog).getByRole('region', { name: 'From addon schedules' })
    expect(region.textContent).toContain(SPOOF)
    expect(region.textContent).toContain('id = check-inbox')
    expect(region.className).toMatch(/dashed/)
  })
})
