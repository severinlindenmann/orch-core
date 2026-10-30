import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { cleanProfile, EMPTY_PROFILE } from '@/api/profile'
import { resetMockProfile, setHubScenario } from '@/mocks/hub'
import { renderApp } from '@/test/renderApp'

beforeEach(() => { resetMockProfile(); setHubScenario(null) })
afterEach(() => { vi.restoreAllMocks(); setHubScenario(null) })

describe('Settings > Profile (HUB-WS-09)', () => {
  it('without a hub says the profile is shown once a hub is connected, and editing still saves', async () => {
    const { user } = renderApp('/settings/profile')
    expect(await screen.findByText(/It is shown once a hub is connected/)).toBeInTheDocument()
    const put = vi.spyOn(api, 'putHubProfile')
    await user.type(await screen.findByTestId('profile-description'), 'Invoices and payments')
    await user.type(screen.getByTestId('profile-tags'), 'billing, , finance')
    await user.click(screen.getByTestId('profile-listed'))
    await user.click(screen.getByTestId('profile-accepts-question'))
    await user.click(screen.getByTestId('profile-save'))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    expect(put.mock.calls[0][1]).toEqual({ ...EMPTY_PROFILE, listed: true, description: 'Invoices and payments', tags: ['billing', 'finance'], accepts: ['question'] })
    expect(await screen.findByTestId('profile-saved')).toHaveTextContent('shows it once a hub is connected')
  })

  it('says it was sent when a hub is connected, and keeps the rarer fields behind Details', async () => {
    setHubScenario('selective')
    const { user } = renderApp('/settings/profile')
    expect(await screen.findByText(/agents use it to find the right workspace/)).toBeInTheDocument()
    expect(screen.queryByTestId('profile-contact')).toBeNull()
    await user.click(screen.getByTestId('profile-more'))
    await user.type(screen.getByTestId('profile-contact'), 'Dana')
    await user.click(screen.getByTestId('profile-save'))
    expect(await screen.findByTestId('profile-saved')).toHaveTextContent('Saved and sent to the hub.')
  })

  it('shows the plain refusal of the host and asks for no typed word', async () => {
    vi.spyOn(api, 'putHubProfile').mockRejectedValue(new ApiError(403, { code: 'role.denied', message: 'Only the workspace owner changes the profile.', retryable: false }))
    const { user } = renderApp('/settings/profile')
    await user.type(await screen.findByTestId('profile-description'), 'x')
    await user.click(screen.getByTestId('profile-save'))
    expect(await screen.findByTestId('profile-error')).toHaveTextContent('Only the workspace owner')
    expect(screen.queryByRole('textbox', { name: /type/i })).toBeNull()
  })

  it('is a Settings tab', async () => {
    renderApp('/settings/general')
    const nav = await screen.findByRole('navigation', { name: 'Settings' })
    expect(within(nav).getByRole('link', { name: 'Profile' })).toBeInTheDocument()
  })
})

describe('cleanProfile', () => {
  it('trims, drops empty items and keeps eight', () => {
    const got = cleanProfile({ ...EMPTY_PROFILE, tags: [' a ', '', ...Array.from({ length: 12 }, (_, i) => `t${i}`)], hours: '  9-5 ' })
    expect(got.tags).toHaveLength(8)
    expect(got.tags[0]).toBe('a')
    expect(got.hours).toBe('9-5')
  })
})
