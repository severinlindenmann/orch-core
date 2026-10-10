import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }

afterEach(() => vi.restoreAllMocks())

async function artifactsTab(key = 'DEMO-0041') {
  const r = renderApp(`/ticket/${key}`)
  await screen.findByRole('heading', { level: 1 }, T)
  await r.user.click(screen.getByRole('tab', { name: /Artifacts/ }))
  await screen.findByRole('list', { name: 'Artifacts' }, T)
  return r
}

// Owner request 2026-10-10 (G3): the ticket's artifact cards follow the Artifacts page model.
describe('Ticket artifacts: cards', () => {
  it('a card has one Preview button; its name is plain text; AC and task are links that look like links', async () => {
    const { user } = await artifactsTab()
    const preview = screen.getByRole('button', { name: 'Preview reconcile-pass.log' })
    const card = preview.closest('li')!
    expect(within(card).getByText('reconcile-pass.log').closest('a, button')).toBeNull()
    const ac = within(card).getByRole('button', { name: 'Proves AC1' })
    expect(ac.className).toMatch(/underline/)
    await user.click(preview)
    expect(await screen.findByRole('dialog', {}, T)).toBeInTheDocument()
  })
  it('no generated placeholder art on the cards, only the type', async () => {
    await artifactsTab()
    const card = screen.getByRole('button', { name: 'Preview reconcile-pass.log' }).closest('li')!
    expect(within(card).queryByRole('img', { name: /Generated placeholder/ })).toBeNull()
    expect(card.querySelector('svg[viewBox="0 0 240 150"]')).toBeNull()
  })
})

describe('Ticket artifacts: viewer', () => {
  it('Copy all says when it could not copy', async () => {
    const { user } = await artifactsTab()
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    await user.click(screen.getByRole('button', { name: 'Preview reconcile-pass.log' }))
    const sheet = await screen.findByRole('dialog', {}, T)
    await user.click(within(sheet).getByRole('button', { name: 'Copy all' }))
    expect(await within(sheet).findByText(/Could not copy/, {}, T)).toBeInTheDocument()
  })
  it('Copy all without a clipboard says so too', async () => {
    const { user } = await artifactsTab()
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
    await user.click(screen.getByRole('button', { name: 'Preview reconcile-pass.log' }))
    const sheet = await screen.findByRole('dialog', {}, T)
    await user.click(within(sheet).getByRole('button', { name: 'Copy all' }))
    expect(await within(sheet).findByText(/Could not copy/, {}, T)).toBeInTheDocument()
  })
  it('View source switches to "Show preview" and back', async () => {
    const { user } = await artifactsTab()
    await user.click(screen.getByRole('button', { name: 'Preview reconciliation-demo.html' }))
    const sheet = await screen.findByRole('dialog', {}, T)
    await waitFor(() => expect(sheet.querySelector('iframe')).toHaveAttribute('sandbox', 'allow-scripts'), T)
    await user.click(within(sheet).getByRole('button', { name: 'View source' }))
    expect(sheet.querySelector('iframe')).toBeNull()
    await user.click(within(sheet).getByRole('button', { name: 'Show preview' }))
    await waitFor(() => expect(sheet.querySelector('iframe')).toHaveAttribute('sandbox', 'allow-scripts'), T)
  })
  it('read-only CSV rows do not light up on hover', async () => {
    const { user } = await artifactsTab('DEMO-0043')
    await user.click(screen.getByRole('button', { name: 'Preview tariff-source-diff.csv' }))
    const sheet = await screen.findByRole('dialog', {}, T)
    const rows = within(sheet).getAllByRole('row')
    for (const r of rows) expect(r.className).toMatch(/hover:bg-transparent/)
  })
})
