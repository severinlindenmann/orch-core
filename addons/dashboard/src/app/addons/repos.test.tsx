import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'
const T = { timeout: 6000 }
afterEach(() => vi.unstubAllGlobals())
const PATH = '/addon/repos/repos'

describe('Repos page', () => {
  it('shows the tree and row details, full copyable remote and working repo filter', async () => {
    const { user } = renderApp(PATH, { viewer: 'p_sev' })
    expect(await screen.findByRole('heading', { level: 1, name: /Repos/ }, T)).toHaveTextContent('Preview')
    for (const name of ['Structure', 'Checks', 'Activity log']) expect(screen.getByRole('tab', { name })).toBeInTheDocument()
    expect(screen.getByText('~/work/acme')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Details for web-portal' }))
    expect(screen.getByRole('button', { name: 'Copy Remote URL' })).toBeInTheDocument()
    expect(screen.getByText('https://git.example.test/acme/web-portal.git')).toBeInTheDocument()
    expect(screen.getByText('gh · orch-agent-acme on github.com · OS user orch-agent')).toBeInTheDocument()
    await user.click(screen.getByRole('link', { name: 'Tickets linking web-portal' }))
    expect(await screen.findByText('Repo: web-portal ×', {}, T)).toBeInTheDocument()
    expect(await screen.findByText('DEMO-0046', {}, T)).toBeInTheDocument()
  })
  it('opens the requested repo row from a permanent URL', async () => {
    renderApp('/w/DEMO/addon/repos/repos?tab.repos=structure&row=web-portal', { viewer: 'p_sev' })
    expect(await screen.findByRole('button', { name: 'Details for web-portal' }, T)).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('https://git.example.test/acme/web-portal.git')).toBeInTheDocument()
  })
  it('checks now and displays actor and timestamp in the activity log', async () => {
    const { user } = renderApp(PATH, { viewer: 'p_sev' })
    await user.click(await screen.findByRole('tab', { name: 'Checks' }, T))
    expect(screen.getByText('meter-ingest: 3 behind origin/main')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Check now' }))
    await user.click(screen.getByRole('tab', { name: 'Activity log' }))
    expect(await screen.findByText('Checked all repo folders.', {}, T)).toBeInTheDocument()
    expect(screen.getByText(/Severin · repos.checked/)).toBeInTheDocument()
  })
  it('Glance shows a calm readiness line and a missing repo needs attention', async () => {
    renderApp('/', { viewer: 'p_sev' })
    const glance = await screen.findByRole('region', { name: 'Glance' }, T)
    expect(await within(glance).findByText('5 of 7', {}, T)).toBeInTheDocument()
    expect(within(glance).getByText('1 missing · 1 behind')).toBeInTheDocument()
    expect(await screen.findByText('billing-api is not cloned yet', {}, T)).toBeInTheDocument()
  })
  it('ticket panel shows linked repo states and branch with a row link', async () => {
    vi.stubGlobal('innerWidth', 1600)
    const { user } = renderApp('/ticket/DEMO-0046', { viewer: 'p_sev' })
    const panel = await openTicketPanel(user, /Repos/)
    expect(await within(panel).findByText('web-portal', {}, T)).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'Open web-portal in Repos' })
    expect(link.getAttribute('href')).toContain('row=web-portal')
    await user.click(link)
    expect(await screen.findByRole('button', { name: 'Details for web-portal' }, T)).toHaveAttribute('aria-expanded', 'true')
  })
  it('a maintainer sees no declaration controls; the owner-only rule is said in plain words', async () => {
    renderApp(PATH, { viewer: 'p_mara' })
    expect(await screen.findByText('Only owners change the declared repos (the workspace settings).', {}, T)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Declare a repo/ })).toBeNull()
  })
  it('settings shows the read-only workspace root and interval/fetch controls', async () => {
    renderApp('/settings/addon/repos', { viewer: 'p_sev' })
    expect(await screen.findByText('~/work/acme', {}, T)).toBeInTheDocument()
    expect(await screen.findByRole('combobox', { name: 'Auto-check interval' }, T)).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Fetch on check' })).toBeInTheDocument()
  })
})
