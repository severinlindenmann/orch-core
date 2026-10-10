import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
    expect(screen.getByRole('button', { name: 'Record changes' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Push to remote' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Pull' })).toBeInTheDocument()
    expect(screen.getAllByRole('table').length).toBeGreaterThan(0)
    expect(screen.getByText('Saves the 12 pending events as one record commit.')).toBeInTheDocument()
    expect(screen.getByText('Sends 1 record commit to the shared remote.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Record changes' }).className).toMatch(/bg-primary/) // the next step
    expect(s.summary).toBe('12 events to record · 1 commit waiting to push')
    // The remote and the last push are under Details, closed.
    expect(screen.queryByText(/energy-records\.git/)).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Details' }))
    expect((await screen.findAllByText(/energy-records\.git/)).length).toBeGreaterThan(0)
  })
  it('Record empties the pending list; nothing to push disables Push with its reason', async () => {
    const { user } = renderApp('/addon/records/records', { viewer: 'p_sev', setup })
    await user.click(await screen.findByRole('button', { name: 'Record changes' }, T))
    expect(await screen.findByText('Everything is recorded · 2 commits waiting to push', {}, T)).toBeInTheDocument()
    expect((await st()).rows).toEqual([])
    expect(screen.getByRole('button', { name: 'Record changes' })).toBeDisabled()
    expect(screen.getByText('Nothing pending to record.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Push to remote' }))
    await waitFor(() => expect(screen.getAllByText(/pushed/i).length).toBeGreaterThan(0), T)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Push to remote' })).toBeDisabled(), T)
    expect(screen.getByText('Nothing to push: the remote has every record commit.')).toBeInTheDocument()
  })
  it('a rejected push says "pull first" once, with Pull primary; Pull clears it', async () => {
    // Recorded and pushed, then another clone pushed, and someone worked on a ticket since.
    const { user } = renderApp('/addon/records/records', {
      viewer: 'p_sev',
      setup: (store) => {
        setup(store)
        const w = store.workspaces.find((x) => x.prefix === 'DEMO')!.id
        store.runAddon(w, 'records', 'commit', {})
        store.runAddon(w, 'records', 'push', {})
        store.append('DEMO-0041', { type: 'labels.changed', add: ['x'] })
      },
    })
    await user.click(await screen.findByRole('button', { name: 'Record changes' }, T))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Push to remote' })).toBeEnabled(), T)
    await user.click(screen.getByRole('button', { name: 'Push to remote' }))
    expect(await screen.findByText('Push rejected · pull first', {}, T)).toBeInTheDocument()
    expect(screen.getAllByText(/Remote rejected: non-fast-forward/)).toHaveLength(1)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Pull' }).className).toMatch(/bg-primary/), T)
    await user.click(screen.getByRole('button', { name: 'Pull' }))
    await waitFor(() => expect(screen.queryByText('Push rejected · pull first')).not.toBeInTheDocument(), T)
    expect(await screen.findByText('Everything is recorded · 1 commit waiting to push', {}, T)).toBeInTheDocument()
  })
  it('has Pending and History tabs: pending first, history behind', async () => {
    const { user } = renderApp('/addon/records/records', { viewer: 'p_sev', setup })
    expect(await screen.findByRole('tab', { name: /^Pending\s*\d+/ }, T)).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('columnheader', { name: 'Events' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Record changes' }))
    await waitFor(() => expect(screen.getByRole('tab', { name: /^Pending\s*0/ })).toBeInTheDocument(), T)
    await user.click(screen.getByRole('tab', { name: /^History\s*\d+/ }))
    expect((await screen.findAllByText(/ ago by /, {}, T)).length).toBeGreaterThan(0)
    expect(screen.queryByRole('columnheader', { name: 'Events' })).not.toBeInTheDocument()
  })
  it('a viewer sees the page but its buttons are disabled', async () => {
    renderApp('/addon/records/records', { viewer: 'p_tom', setup })
    expect(await screen.findByRole('button', { name: 'Record changes' }, T)).toBeDisabled()
  })
})
