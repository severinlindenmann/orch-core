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
  it('the button says "Opening hub…" and is disabled from the press until the browser leaves; an error brings it back', async () => {
    setHubScenario('selective')
    let release: (u: string) => void = () => {}
    let fail: (e: Error) => void = () => {}
    h.hubUrl.mockReset().mockImplementationOnce(() => new Promise<string>((res) => { release = res })).mockImplementationOnce(() => new Promise<string>((_r, rej) => { fail = rej }))
    renderApp('/w/DEMO/settings/hub')
    fireEvent.click(await screen.findByTestId('hub-open'))
    await waitFor(() => expect(screen.getByTestId('hub-open').textContent).toBe('Opening hub…'))
    expect((screen.getByTestId('hub-open') as HTMLButtonElement).disabled).toBe(true)
    expect(assign).not.toHaveBeenCalled()
    release('https://hub.example/t/abc')
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://hub.example/t/abc'))
    cleanup()
    renderApp('/w/DEMO/settings/hub')
    fireEvent.click(await screen.findByTestId('hub-open'))
    await waitFor(() => expect(screen.getByTestId('hub-open').textContent).toBe('Opening hub…'))
    fail(new Error('no'))
    await waitFor(() => expect(screen.getByTestId('hub-open').textContent).toBe('Open hub'))
    expect((screen.getByTestId('hub-open') as HTMLButtonElement).disabled).toBe(false)
  })
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

describe('Open hub next to the hub badge', () => {
  it('shows with a connected hub, not standalone, and takes the ticket', async () => {
    setHubScenario('selective')
    renderApp('/w/DEMO')
    fireEvent.click(await screen.findByTestId('hub-open-chip'))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://hub.example/t/abc'))
    cleanup()
    setHubScenario('standalone')
    renderApp('/w/DEMO')
    await screen.findByTestId('hub-chip')
    expect(screen.queryByTestId('hub-open-chip')).toBeNull()
  })
})
