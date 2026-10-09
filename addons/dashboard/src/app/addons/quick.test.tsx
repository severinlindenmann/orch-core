import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'

const T = { timeout: 4000 }
const ws = () => mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id
const setup = (store: typeof mockStore) => installAndGrant(store, store.workspaces.find((w) => w.prefix === 'DEMO')!.id, 'quick')
const items = async () => (await api.getAddonState(ws(), 'quick')).items as { id: string; status: string; ticket?: string }[]
// The page's list rows (a toast such as "Q-001 claimed." is also an li, so skip the toaster).
const rowOf = async (key: string) => {
  return waitFor(() => {
    const hit = screen.getAllByText(new RegExp(`^${key}\\b`)).find((h) => !h.closest('[data-sonner-toaster]'))
    if (!hit) throw new Error(`no row ${key}`)
    return hit.closest('li')!
  }, T)
}

describe('quick tasks page', () => {
  it('lists the tasks with status chips and the outgrew limits', async () => {
    renderApp('/addon/quick/quick', { viewer: 'p_sev', setup })
    const o = await rowOf('Q-004')
    expect(within(o).getByText('outgrew')).toBeInTheDocument()
    expect(within(o).getByText(/4 commits, 7 files/)).toBeInTheDocument()
    expect(within(await rowOf('Q-003')).getByText('claimed')).toBeInTheDocument()
    expect(within(await rowOf('Q-005')).getByText('done')).toBeInTheDocument()
  })
  it('the add form appends Q-007', async () => {
    const { user } = renderApp('/addon/quick/quick', { viewer: 'p_sev', setup })
    await rowOf('Q-001') // the form's schema arrives with the state
    await user.type(await screen.findByLabelText(/^Quick task \(one line\)/, {}, T), 'Fix the typo in CONTRIBUTING')
    await user.click(screen.getByRole('button', { name: 'Add quick task' }))
    await rowOf('Q-007')
    expect((await items()).at(-1)!.status).toBe('open')
  })
  it('Claim moves an open task to claimed', async () => {
    const { user } = renderApp('/addon/quick/quick', { viewer: 'p_sev', setup })
    await user.click(within(await rowOf('Q-001')).getByRole('button', { name: 'Claim' }))
    await waitFor(async () => expect((await items()).find((q) => q.id === 'Q-001')!.status).toBe('claimed'), T)
  })
  it('Close with proof opens a one-line form and closes the task', async () => {
    const { user } = renderApp('/addon/quick/quick', { viewer: 'p_sev', setup })
    await user.click(within(await rowOf('Q-003')).getByRole('button', { name: 'Close with proof' }))
    await user.type(await screen.findByLabelText(/^Proof/, {}, T), 'removed, 9ac1f20')
    await user.click(screen.getByRole('button', { name: 'Close task' }))
    await waitFor(async () => expect((await items()).find((q) => q.id === 'Q-003')!.status).toBe('done'), T)
  })
  it('Make a ticket converts the task', async () => {
    const { user } = renderApp('/addon/quick/quick', { viewer: 'p_sev', setup })
    await user.click(within(await rowOf('Q-002')).getByRole('button', { name: 'Make a ticket' }))
    await waitFor(async () => expect((await items()).find((q) => q.id === 'Q-002')!.status).toBe('converted'), T)
  })
  it('viewer sees disabled actions', async () => {
    renderApp('/addon/quick/quick', { viewer: 'p_tom', setup })
    expect(within(await rowOf('Q-001')).getByRole('button', { name: 'Claim' })).toBeDisabled()
  })
})

describe('quick tasks decision on Today', () => {
  it('shows the outgrew decision and removes it once decided', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', setup })
    const card = (await screen.findAllByText('Q-004 outgrew its limit: make it a ticket, or allow 3 more files?', {}, T))[0].closest('[data-testid^="card-addon:"]') as HTMLElement
    await user.click(within(card).getByRole('button', { name: 'Decide' }))
    await user.click(within(card).getByRole('button', { name: 'Allow 3 more files' }))
    await user.click(await screen.findByRole('button', { name: 'Sign with Touch ID' }, T))
    await waitFor(() => expect(screen.queryByText('Q-004 outgrew its limit: make it a ticket, or allow 3 more files?')).not.toBeInTheDocument(), T)
  })
})
