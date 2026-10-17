import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fakeRequest, state } from '@/test/handoffHarness'
import { renderApp } from '@/test/renderApp'
import { unsavedCopy } from './index'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), handoffMode: () => true }))
// The signing dialog is its own feature (HandoffDialog tests); here it only reports how each signature ended.
vi.mock('@/components/sign/HandoffDialog', () => ({
  HandoffDialog: (p: { title: string; action: { action: string; key?: string }; onSettled?: (phase: string, st: unknown, req: unknown) => void; onClose: () => void }) => {
    const isNew = p.action.action === 'new'
    const req = { ...fakeRequest({ key: 'DEMO-0044', id: 'hr_' + '0'.repeat(25) + '7' }), op: 'new' }
    return (
      <div role="dialog" aria-label={p.title}>
        <button onClick={() => p.onSettled?.('done', isNew ? state(req, { state: 'submitted', key: 'DEMO-0044' }) : null, isNew ? req : null)}>signed</button>
        <button onClick={p.onClose}>cancelled</button>
      </div>
    )
  },
}))

const T = { timeout: 5000 }
afterEach(() => vi.restoreAllMocks())

async function fill() {
  const r = renderApp('/board')
  await waitFor(() => expect(screen.getByTestId('topbar-title')).not.toHaveTextContent(/^$/), T)
  await r.user.click(await screen.findByRole('button', { name: /New ticket/ }, T))
  const sheet = await screen.findByRole('dialog', { name: 'New ticket' }, T)
  await r.user.type(await within(sheet).findByLabelText('Title', {}, T), 'Export CSV')
  await r.user.type(within(sheet).getByLabelText(/^Requirements/), 'Empty months export a header row')
  await r.user.click(within(sheet).getByRole('button', { name: 'Create' }))
  return r
}
const gone = () => waitFor(() => expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull(), T)

describe('New ticket with signed hand-off', () => {
  it('create and the follow-up signature both signed: closes without a discard prompt and opens the ticket', async () => {
    const r = await fill()
    await r.user.click(await screen.findByRole('button', { name: 'signed' }))
    await r.user.click(await screen.findByRole('button', { name: 'signed' })) // the written text
    await gone()
    expect(screen.queryByText('Discard unsaved changes?')).toBeNull()
    await waitFor(() => expect(r.router.state.location.pathname).toBe('/ticket/DEMO-0044'), T)
  })

  it('follow-up cancelled: says the ticket was created and its text is not saved yet, offers Try again, never "not saved as a ticket"', async () => {
    const r = await fill()
    await r.user.click(await screen.findByRole('button', { name: 'signed' }))
    await r.user.click(await screen.findByRole('button', { name: 'cancelled' }))
    expect(await screen.findByTestId('followup-unsaved')).toHaveTextContent('DEMO-0044 was created; its written text is not saved yet.')
    expect(screen.queryByText(/not been saved as a ticket/)).toBeNull()
    expect(screen.queryByText('Discard unsaved changes?')).toBeNull()
    await r.user.click(screen.getByRole('button', { name: 'Try again' }))
    await r.user.click(await screen.findByRole('button', { name: 'signed' }))
    await gone()
    await waitFor(() => expect(r.router.state.location.pathname).toBe('/ticket/DEMO-0044'), T)
  })
})

describe('unsavedCopy', () => {
  it('names the written text, or the details', () => {
    expect(unsavedCopy('RA-0001', [{ action: 'section', key: '', section: 'requirements', text: 'x' }])).toBe('RA-0001 was created; its written text is not saved yet.')
    expect(unsavedCopy('RA-0001', [{ action: 'details', key: '', priority: 'high' }])).toBe('RA-0001 was created; its details are not saved yet.')
  })
})
