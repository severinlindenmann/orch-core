import { screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { setHubScenario } from '@/mocks/hub'
import { renderApp } from '@/test/renderApp'

afterEach(() => { vi.restoreAllMocks(); setHubScenario(null) })

describe('Settings > Hub > Network (R6)', () => {
  it('says "directly" first, keeps the form behind Details, and never shows the password again', async () => {
    setHubScenario('standalone')
    const { user } = renderApp('/w/DEMO/settings/hub')
    await user.click(await screen.findByTestId('hub-standalone-details'))
    expect(await screen.findByTestId('network-state')).toHaveTextContent('This computer can reach a hub directly.')
    expect(screen.queryByTestId('network-proxy')).toBeNull()
    await user.click(screen.getByTestId('network-details'))
    const put = vi.spyOn(api, 'putHubNetwork')
    await user.type(screen.getByTestId('network-proxy'), 'https://proxy.office:3128')
    await user.type(screen.getByTestId('network-user'), 'anna')
    await user.type(screen.getByTestId('network-password'), 's3cret')
    await user.click(screen.getByTestId('network-save'))
    await waitFor(() => expect(put).toHaveBeenCalledWith(expect.any(String), { proxy: 'https://proxy.office:3128', user: 'anna', password: 's3cret' }))
    expect(await screen.findByText(/company proxy https:\/\/proxy.office:3128/)).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('s3cret')
    await user.click(screen.getByTestId('network-clear'))
    await waitFor(() => expect(put).toHaveBeenLastCalledWith(expect.any(String), { proxy: null }))
  })
})
