import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'

const T = { timeout: 4000 }
const ws = () => mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id
const setup = (store: typeof mockStore) => installAndGrant(store, store.workspaces.find((w) => w.prefix === 'DEMO')!.id, 'records')
const st = async () => (await api.getAddonState(ws(), 'records')) as unknown as { summary: string; rows: unknown[]; history: unknown[] }

describe('records page', () => {
  it('shows the pending summary, the per-ticket table, the buttons and the history', async () => {
    renderApp('/addon/records/records', { viewer: 'p_sev', setup })
    const s = await waitFor(async () => {
      const x = await st()
      expect(x.rows.length).toBeGreaterThan(0)
      return x
    }, T)
    expect(await screen.findByText(s.summary, {}, T)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Commit records' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Push' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Pull' })).toBeInTheDocument()
    expect(screen.getAllByRole('table').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/energy-records\.git/).length).toBeGreaterThan(0)
  })
  it('Commit records empties the pending list; push twice shows the error alert; Pull clears it', async () => {
    const { user } = renderApp('/addon/records/records', { viewer: 'p_sev', setup })
    await user.click(await screen.findByRole('button', { name: 'Commit records' }, T))
    expect(await screen.findByText('Everything is recorded', {}, T)).toBeInTheDocument()
    expect((await st()).rows).toEqual([])
    await user.click(screen.getByRole('button', { name: 'Push' }))
    await waitFor(() => expect(screen.getAllByText(/pushed/i).length).toBeGreaterThan(0), T)
    await user.click(screen.getByRole('button', { name: 'Push' }))
    const rejected = 'Remote rejected: non-fast-forward. Pull first.'
    await waitFor(() => expect(screen.getAllByText(rejected).length).toBeGreaterThan(0), T)
    expect(screen.getAllByRole('status').some((n) => n.textContent?.includes(rejected) && n.textContent.includes('Pull, then push again'))).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Pull' }))
    await waitFor(async () => expect(((await api.getAddonState(ws(), 'records')) as unknown as { pushAlert: { tone?: string } }).pushAlert.tone).not.toBe('error'), T)
    await waitFor(() => expect(screen.queryByText('Another clone pushed first. Pull, then push again.')).not.toBeInTheDocument(), T)
  })
  it('a viewer sees the page but its buttons are disabled', async () => {
    renderApp('/addon/records/records', { viewer: 'p_tom', setup })
    expect(await screen.findByRole('button', { name: 'Commit records' }, T)).toBeDisabled()
  })
})
