import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'

const T = { timeout: 4000 }
const ws = () => mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id
const setup = (store: typeof mockStore) => installAndGrant(store, store.workspaces.find((w) => w.prefix === 'DEMO')!.id, 'worktrees')
const wts = async () => (await api.getAddonState(ws(), 'worktrees')).worktrees as { ticket: string; path: string; dirty: number }[]

describe('worktrees page', () => {
  it('lists worktrees per repo with path, branch, files and ahead/behind', async () => {
    renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const row = (await screen.findByText('wt/DEMO-0043-energy-dbt', {}, T)).closest('li')!
    expect(within(row).getByText(/feat\/load-tariff-tables-as-dbt-seeds/)).toBeInTheDocument()
    expect(within(row).getByText(/3 changed files/)).toBeInTheDocument()
    expect(within(row).getByText(/ahead 2/)).toBeInTheDocument()
  })
  it('Remove on the dirty worktree is refused with the message', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const row = (await screen.findByText('wt/DEMO-0043-energy-dbt', {}, T)).closest('li')!
    await user.click(within(row).getByRole('button', { name: 'Remove' }))
    await screen.findByText('3 changed files. Commit or stash first.', {}, T)
    expect((await wts()).some((w) => w.path === 'wt/DEMO-0043-energy-dbt')).toBe(true)
  })
  it('Remove on a clean worktree removes it from the page', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const row = (await screen.findByText('wt/DEMO-0041-energy-dbt', {}, T)).closest('li')!
    await user.click(within(row).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(screen.queryByText('wt/DEMO-0041-energy-dbt')).not.toBeInTheDocument(), T)
  })
  it('the add form creates a worktree', async () => {
    const { user } = renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const sel = await screen.findByLabelText(/^Ticket\b/, {}, T)
    await waitFor(() => expect(within(sel).getByRole('option', { name: 'DEMO-0044' })).toBeInTheDocument(), T)
    await user.selectOptions(sel, 'DEMO-0044')
    await user.selectOptions(screen.getByLabelText(/^Repository/), 'acme-energy/billing-api')
    await user.click(screen.getByRole('button', { name: 'Add worktree' }))
    await screen.findByText('wt/DEMO-0044-billing-api', {}, T)
  })
  it('Open terminal here shows only while terminals is active', async () => {
    renderApp('/addon/worktrees/worktrees', { viewer: 'p_sev', setup })
    const row = (await screen.findByText('wt/DEMO-0043-energy-dbt', {}, T)).closest('li')!
    expect(within(row).getByRole('button', { name: 'Open terminal here' })).toBeInTheDocument()
  })
  it('no Open terminal here when terminals is off', async () => {
    renderApp('/addon/worktrees/worktrees', {
      viewer: 'p_sev',
      setup: (st) => {
        setup(st)
        st.addonOp(ws(), 'terminals', { op: 'disable' }, { kind: 'person', id: 'p_sev' })
      },
    })
    const row = (await screen.findByText('wt/DEMO-0043-energy-dbt', {}, T)).closest('li')!
    expect(within(row).queryByRole('button', { name: 'Open terminal here' })).not.toBeInTheDocument()
  })
  it('viewer sees disabled actions', async () => {
    renderApp('/addon/worktrees/worktrees', { viewer: 'p_tom', setup })
    const row = (await screen.findByText('wt/DEMO-0043-energy-dbt', {}, T)).closest('li')!
    expect(within(row).getByRole('button', { name: 'Remove' })).toBeDisabled()
  })
})

describe('worktrees ticket panel', () => {
  it('shows this ticket worktrees and adds one for this ticket', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev', setup })
    const panel = await screen.findByRole('complementary', { name: 'Ticket details' }, T)
    expect(await within(panel).findByText('wt/DEMO-0043-energy-dbt', {}, T)).toBeInTheDocument()
    await user.selectOptions(within(panel).getByLabelText(/^Repository/), 'acme-energy/ingest')
    await user.click(within(panel).getByRole('button', { name: 'Add worktree for this ticket' }))
    await within(panel).findByText('wt/DEMO-0043-ingest', {}, T)
  })
})
