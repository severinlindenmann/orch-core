import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fakeRequest, state } from '@/test/handoffHarness'
import { renderApp } from '@/test/renderApp'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), handoffMode: () => true }))
// The signing dialog is its own feature (SignPopover tests); here it only reports how each signature ended.
vi.mock('@/components/sign/SignPopover', () => ({
  SignPopover: (p: { title: string; action: { action: string; key?: string }; onSettled?: (phase: string, st: unknown, req: unknown) => void; onClose: () => void }) => {
    const isNew = p.action.action === 'new' || p.action.action === 'new_sequence'
    const req = { ...fakeRequest({ key: 'DEMO-0044', id: 'hr_' + '0'.repeat(25) + '7' }), op: 'new' }
    return (
      <div role="dialog" aria-label={p.title}>
        <pre data-testid="sent-action">{JSON.stringify(p.action)}</pre>
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
  await r.user.click(within(sheet).getByRole('button', { name: 'Review ticket' }))
  return r
}
const gone = () => waitFor(() => expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull(), T)

describe('New ticket with signed hand-off', () => {
  it('the ticket and its text are ONE signature: closes without a discard prompt and opens the ticket', async () => {
    const r = await fill()
    await r.user.click(await screen.findByRole('button', { name: 'signed' }))
    await gone()
    expect(screen.queryByText('Discard unsaved changes?')).toBeNull()
    expect(screen.queryByText(/not saved yet/)).toBeNull() // no second signature is offered
    await waitFor(() => expect(r.router.state.location.pathname).toBe('/ticket/DEMO-0044'), T)
  })

  it('asks the host for the whole intent at once', async () => {
    await fill()
    const sent = await screen.findByTestId('sent-action')
    expect(JSON.parse(sent.textContent ?? '{}')).toEqual({ action: 'new_sequence', title: 'Export CSV', type: 'feature', sections: { requirements: 'Empty months export a header row' } })
  })

  it('carries acceptance criteria and people in the same intent, with the creator not named as owner again (N3)', async () => {
    const r = renderApp('/board')
    await waitFor(() => expect(screen.getByTestId('topbar-title')).not.toHaveTextContent(/^$/), T)
    await r.user.click(await screen.findByRole('button', { name: /New ticket/ }, T))
    const sheet = await screen.findByRole('dialog', { name: 'New ticket' }, T)
    await r.user.type(await within(sheet).findByLabelText('Title', {}, T), 'Export CSV')
    await r.user.type(within(sheet).getByLabelText(/^Requirements/), 'Empty months export a header row')
    await r.user.click(within(sheet).getByRole('button', { name: /More sections/ }))
    await r.user.type(within(sheet).getByLabelText('New acceptance criterion'), 'A header row is exported')
    await r.user.click(within(sheet).getByRole('button', { name: 'Add' }))
    await r.user.selectOptions(within(sheet).getByLabelText('Owner'), 'p_sev') // the signed-in person: already the owner
    await r.user.click(within(within(sheet).getByRole('group', { name: 'Assignees' })).getByRole('button', { name: 'Mara' }))
    await r.user.click(within(sheet).getByRole('button', { name: 'Review ticket' }))
    const sent = JSON.parse((await screen.findByTestId('sent-action')).textContent ?? '{}')
    expect(sent).toEqual({
      action: 'new_sequence',
      title: 'Export CSV',
      type: 'feature',
      sections: { requirements: 'Empty months export a header row' },
      acceptance: ['A header row is exported'],
      people: { assignees: ['p_mara'] },
    })
  })

  it('E-7: a criterion typed but not added with Add is still part of the ticket', async () => {
    const r = renderApp('/board')
    await waitFor(() => expect(screen.getByTestId('topbar-title')).not.toHaveTextContent(/^$/), T)
    await r.user.click(await screen.findByRole('button', { name: /New ticket/ }, T))
    const sheet = await screen.findByRole('dialog', { name: 'New ticket' }, T)
    await r.user.type(await within(sheet).findByLabelText('Title', {}, T), 'Export CSV')
    await r.user.type(within(sheet).getByLabelText(/^Requirements/), 'Empty months export a header row')
    await r.user.click(within(sheet).getByRole('button', { name: /More sections/ }))
    await r.user.type(within(sheet).getByLabelText('New acceptance criterion'), 'A header row is exported')
    await r.user.click(within(sheet).getByRole('button', { name: 'Review ticket' }))
    const sent = JSON.parse((await screen.findByTestId('sent-action')).textContent ?? '{}')
    expect(sent.acceptance).toEqual(['A header row is exported'])
  })

  it('cancelled before it was signed: nothing is created and the form keeps what was typed', async () => {
    const r = await fill()
    await r.user.click(await screen.findByRole('button', { name: 'cancelled' }))
    const sheet = (await screen.findAllByRole('dialog', { name: 'New ticket' }, T))[0]
    expect(within(sheet).getByLabelText('Title')).toHaveValue('Export CSV')
    expect(within(sheet).getByLabelText(/^Requirements/)).toHaveValue('Empty months export a header row')
  })
})
