import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

describe('Agents page', () => {
  it('shows sessions with their subagents and leases', async () => {
    renderApp('/agents')
    const tree = await screen.findByRole('tree', { name: 'Sessions' })
    expect(within(tree).getByText(/s_77c2\.1/)).toBeInTheDocument()
    expect(within(tree).getByText(/DEMO-0043\/T3/)).toBeInTheDocument()
  })
  it('summarises the workspace in the header', async () => {
    renderApp('/agents')
    expect(await screen.findByRole('heading', { level: 1, name: 'Agents' })).toBeInTheDocument()
    expect(await screen.findByText('3 sessions working · 1 waiting on you · grant until 18:00')).toBeInTheDocument()
  })
  it('revokes a grant after signing, and its sessions stop', async () => {
    const { user } = renderApp('/agents')
    const row = await screen.findByRole('row', { name: /gr_01J9Z8/ })
    await user.click(within(row).getByRole('button', { name: 'Revoke' }))
    await user.click(await screen.findByRole('button', { name: /Sign with Touch ID/ }))
    expect(await within(screen.getByRole('row', { name: /gr_01J9Z8/ })).findByText('revoked')).toBeInTheDocument()
    expect(within(screen.getByRole('tree', { name: 'Sessions' })).getAllByText('stopped').length).toBeGreaterThan(0)
  })
  it('stops the main session when its grant is revoked', async () => {
    const { user } = renderApp('/agents')
    const tree = await screen.findByRole('tree', { name: 'Sessions' })
    expect(within(tree).getByRole('treeitem', { name: /s_77c2(?!\.).*working/ })).toBeInTheDocument()
    await user.click(within(await screen.findByRole('row', { name: /gr_01J9Z8/ })).getByRole('button', { name: 'Revoke' }))
    await user.click(await screen.findByRole('button', { name: /Sign with Touch ID/ }))
    await within(screen.getByRole('row', { name: /gr_01J9Z8/ })).findByText('revoked')
    const after = within(screen.getByRole('tree', { name: 'Sessions' }))
    expect(after.getByRole('treeitem', { name: /s_77c2(?!\.).*stopped/ })).toBeInTheDocument()
    expect(after.getByRole('treeitem', { name: /s_77c2\.1.*stopped/ })).toBeInTheDocument()
    expect(after.queryByRole('treeitem', { name: /Claude Code.*working/ })).toBeNull()
  })
  it('lists refusals and marks the stop rule', async () => {
    const { user } = renderApp('/agents')
    await user.click(await screen.findByRole('checkbox', { name: 'Refusals only' }))
    expect(await screen.findByText('human_only')).toBeInTheDocument()
    expect(screen.getByText('stopped after 3 refusals')).toBeInTheDocument()
  })
  it('the viewer cannot revoke or issue', async () => {
    renderApp('/agents', { viewer: 'p_tom' })
    await screen.findByRole('tree', { name: 'Sessions' })
    expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Issue grant…' })).toBeNull()
  })
  it('issues a grant after signing', async () => {
    const { user } = renderApp('/agents')
    await user.click(await screen.findByRole('button', { name: 'Issue grant…' }))
    await user.click(await screen.findByRole('button', { name: /Sign with Touch ID/ }))
    await waitFor(() => expect(screen.getAllByRole('row', { name: /gr_/ })).toHaveLength(5))
  })
})
