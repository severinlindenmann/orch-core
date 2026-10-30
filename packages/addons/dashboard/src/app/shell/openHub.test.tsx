// P04 (run 12): a visible "Open hub" on the Hub page and in the workspace menu, both through the rail icon's one ticketed hop.
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setHubScenario } from '@/mocks/hub'
import { renderApp } from '@/test/renderApp'

const h = vi.hoisted(() => ({ rail: undefined as unknown, hubUrl: vi.fn() }))
vi.mock('@/api/switch', async (orig) => ({ ...(await orig<typeof import('@/api/switch')>()), hubUrl: h.hubUrl }))
vi.mock('./CompanyRail', async (orig) => ({ ...(await orig<typeof import('./CompanyRail')>()), useRail: () => ({ data: h.rail }) }))

const withHub = { hub: { name: 'Acme AG' }, workspaces: [] }
let assign: ReturnType<typeof vi.fn>
beforeEach(() => {
  h.rail = withHub
  h.hubUrl.mockReset().mockResolvedValue('https://hub.example/t/abc')
  assign = vi.fn()
  vi.stubGlobal('location', { ...window.location, assign })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('Open hub', () => {
  for (const mode of ['selective', 'full'] as const) {
    it(`${mode}: the Hub page starts with Open hub, which takes the ticket and goes there`, async () => {
      setHubScenario(mode)
      renderApp('/w/DEMO/settings/hub')
      expect(await screen.findByText('Acme AG hub')).toBeTruthy()
      fireEvent.click(await screen.findByTestId('hub-open'))
      await waitFor(() => expect(assign).toHaveBeenCalledWith('https://hub.example/t/abc'))
      expect(h.hubUrl).toHaveBeenCalledWith(expect.any(String), 'open', '/')
    })
  }
  it('standalone: no Open hub', async () => {
    setHubScenario('standalone')
    renderApp('/w/DEMO/settings/hub')
    await screen.findByTestId('hub-connect')
    expect(screen.queryByTestId('hub-open')).toBeNull()
  })
  it('the workspace menu has an Open hub row only with a hub', async () => {
    setHubScenario('selective')
    const user = userEvent.setup()
    renderApp('/w/DEMO')
    await user.click(await screen.findByRole('button', { name: 'Switch workspace' }))
    await user.click(await screen.findByTestId('switcher-open-hub'))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://hub.example/t/abc'))
  })
  it('no hub, no row', async () => {
    h.rail = { hub: null, workspaces: [] }
    setHubScenario('standalone')
    const user = userEvent.setup()
    renderApp('/w/DEMO')
    await user.click(await screen.findByRole('button', { name: 'Switch workspace' }))
    await screen.findByText('Workspaces')
    expect(screen.queryByTestId('switcher-open-hub')).toBeNull()
  })
})
