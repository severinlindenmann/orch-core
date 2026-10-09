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
    expect(within(dialog).getByRole('heading', { name: 'Sign: Arm · Schedules' })).toBeInTheDocument()
    expect(within(dialog).getByText(/In workspace Acme Energy \(DEMO\)/)).toBeInTheDocument()
    const covers = within(dialog).getByText('Covers').nextElementSibling!
    expect(covers.textContent).not.toContain(SPOOF)
    expect(dialog.querySelector('h2')!.textContent).not.toContain(SPOOF)
    const region = within(dialog).getByRole('region', { name: 'From addon schedules' })
    expect(region.textContent).toContain(SPOOF)
    expect(region.textContent).toContain('Id: check-inbox')
    expect(region.className).toMatch(/dashed/)
  })

  it('always shows every sent arg; the addon-written subject is extra, labelled context in the From-addon region', () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SignConfirm addon="publish" addonTitle="Publish" action="revoke" workspace={{ prefix: 'DEMO', name: 'Acme Energy' }} args={{ share: 'B' }} subject="Revoke share A" onSign={() => {}} onClose={() => {}} />
      </QueryClientProvider>,
    )
    const dialog = screen.getByRole('dialog')
    const region = within(dialog).getByRole('region', { name: 'From addon publish' })
    expect(region.textContent).toContain('Share: B')
    expect(region.textContent).toMatch(/About:\s*Revoke share A/)
    expect(within(dialog).getByText('Covers').nextElementSibling!.textContent).not.toContain('Revoke share A')
  })

  it('names the item an id points at by its row ("Schedule: Smoke on testing") and keeps the signed id under it, in words', () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SignConfirm addon="schedules" addonTitle="Schedules" action="run_now" workspace={{ prefix: 'DEMO', name: 'Acme Energy' }} args={{ id: 'smoke-on-testing' }} subject="Smoke on testing" onSign={() => {}} onClose={() => {}} />
      </QueryClientProvider>,
    )
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: 'Sign: Run now · Schedules' })).toBeInTheDocument()
    const region = within(dialog).getByRole('region', { name: 'From addon schedules' })
    expect(region.textContent).toMatch(/Schedule:\s*Smoke on testing/)
    expect(region.textContent).toContain('Id: smoke-on-testing')
    expect(region.textContent).not.toContain(' = ')
    expect(region.textContent).toContain('From the addon Schedules')
  })

  it('names its verb, says how you confirm, starts on Cancel and has no repeated line', () => {
    draw()
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('button', { name: 'Sign and run' })).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /signs with Touch ID/i })).toBeNull()
    expect(dialog).toHaveTextContent('You confirm with Touch ID or your key.')
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus()
    expect(within(dialog).queryByText('Signed as you, with your own key')).toBeNull()
  })
})
