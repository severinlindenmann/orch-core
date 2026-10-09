import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import type { MockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
const wsOf = (s: MockStore) => s.workspaces.find((w) => w.prefix === 'DEMO')!.id
const on = (s: MockStore) => installAndGrant(s, wsOf(s), 'factory')

describe('AI Factory page', () => {
  it('shows the epic, its limits, progress and the children, with a Preview chip next to the title and the nav entry', async () => {
    const { user } = renderApp('/addon/factory/factory', { viewer: 'p_sev', setup: on })
    const h1 = await screen.findByRole('heading', { level: 1, name: /AI Factory/ }, T)
    expect(within(h1).getByText('Preview')).toBeInTheDocument()
    expect(await screen.findByText(/Monthly billing v2/, {}, T)).toBeInTheDocument()
    expect(screen.getByText(/25 children or 72 hours/)).toBeInTheDocument()
    expect(screen.getAllByRole('progressbar').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByRole('columnheader', { name: 'Approval' })).toBeInTheDocument()
    expect(screen.getAllByText('auto-approved by agent', { selector: 'td' }).length).toBeGreaterThan(0)
    await user.click(screen.getByRole('button', { name: 'Expand sidebar' }))
    const nav = await screen.findByRole('link', { name: 'AI Factory' })
    expect(within(nav).getByText('Preview')).toBeInTheDocument()
  })
  it('the addon manager row carries the Preview chip too', async () => {
    renderApp('/settings/addons', { viewer: 'p_sev', setup: on })
    const row = await screen.findByRole('row', { name: /AI Factory 0\.1\.0/ }, T)
    expect(within(row).getByText('Preview')).toBeInTheDocument()
  })
  it('Pause factory is signed in core\'s dialog; the paused state is calm (info), and Resume brings it back', async () => {
    const { user } = renderApp('/addon/factory/factory', { viewer: 'p_sev', setup: on })
    await user.click(await screen.findByRole('button', { name: 'Pause factory' }, T))
    const dialog = await screen.findByRole('dialog', { name: /Sign: pause · AI Factory/ }, T)
    expect(mockStore.addonStateView(wsOf(mockStore), 'factory')!.mode).toBe('running')
    await user.click(within(dialog).getByRole('button', { name: /Sign with Touch ID/ }))
    const alert = (await screen.findByText(/^Paused by/, {}, T)).closest('[role="status"]')!
    expect(alert.className).toMatch(/info/)
    expect(alert.className).not.toMatch(/warning|danger/)
    await waitFor(() => expect(mockStore.addonStateView(wsOf(mockStore), 'factory')!.mode).toBe('paused'), T)
    await user.click(await screen.findByRole('button', { name: 'Resume' }, T))
    await user.click(within(await screen.findByRole('dialog', { name: /Sign: resume · AI Factory/ }, T)).getByRole('button', { name: /Sign with Touch ID/ }))
    await waitFor(() => expect(mockStore.addonStateView(wsOf(mockStore), 'factory')!.mode).toBe('running'), T)
  })
  it('cancelling the signing dialog changes nothing', async () => {
    const { user } = renderApp('/addon/factory/factory', { viewer: 'p_sev', setup: on })
    await user.click(await screen.findByRole('button', { name: 'Pause factory' }, T))
    const dialog = await screen.findByRole('dialog', { name: /Sign: pause · AI Factory/ }, T)
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), T)
    expect(mockStore.addonStateView(wsOf(mockStore), 'factory')!.mode).toBe('running')
    expect(mockStore.eventsOf('DEMO-0050').some((e) => e.type === 'factory.paused')).toBe(false)
  })
  it('a viewer sees Pause and Watch live disabled', async () => {
    renderApp('/addon/factory/factory', { viewer: 'p_tom', setup: on })
    const pause = await screen.findByRole('button', { name: 'Pause factory' }, T)
    await waitFor(() => expect(pause).toBeDisabled(), T)
    expect(screen.getByRole('button', { name: 'Watch live' })).toBeDisabled()
  })
})

describe('permits on Today', () => {
  it('Grant once removes the card and logs factory.permit_granted on the epic', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', setup: on })
    const card = (await screen.findAllByTestId(/^card-addon:factory\.permit:/, {}, T))[0]
    const id = card.getAttribute('data-testid')!.replace('card-addon:', '')
    expect(within(card).getByRole('button', { name: 'Grant for this epic' })).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Refuse' })).toBeInTheDocument()
    await user.click(within(card).getByRole('button', { name: 'Grant once' }))
    await user.click(await screen.findByRole('button', { name: 'Sign with Touch ID' }, T))
    await waitFor(() => expect(screen.queryByTestId(`card-addon:${id}`)).not.toBeInTheDocument(), T)
    expect(mockStore.eventsOf('DEMO-0050').some((e) => e.type === 'factory.permit_granted' && e.scope === 'once')).toBe(true)
  })
})
