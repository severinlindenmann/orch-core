import { cleanup, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore, resetMockStoreForTests } from '@/api/client'
import { setFeatures } from '@/api/features'
import type { HubSignInfo } from '@/api/hubsign'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { spies } from '@/test/handoffHarness'
import { HubSettingsPanel } from './HubSignPanel'
import { SignPopover } from './SignPopover'
import type { HandoffAction } from '@/api/handoff'

// A settings change through the hub (an optional extra path, A1): the change in words and the host's link, no raw refusal.
// The review itself never routes by the workspace's hub mode: local signing is valid in every mode.
const T = { timeout: 5000 }
const W = 'a'.repeat(32)

function setup(): { ws: string; client: QueryClient } {
  resetMockStoreForTests()
  mockStore.setViewer('p_sev')
  const ws = mockStore.workspaces[0].id
  setFeatures({ features: ['identity', 'today', 'tickets', 'board', 'ticket', 'actions'], workspaces: { [ws]: ['identity', 'today', 'tickets', 'board', 'ticket', 'actions'] } })
  return { ws, client: new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } }) }
}
function mountPanel(action: HandoffAction) {
  const { ws, client } = setup()
  render(<QueryClientProvider client={client}><HubSettingsPanel ws={ws} action={action as Extract<HandoffAction, { action: 'settings_set' }>} onDone={() => {}} /></QueryClientProvider>)
}
const INFO: HubSignInfo = {
  mode: 'full', kind: 'settings', short_hash: null,
  summary: { kind: 'settings', set: { grant_hours: 24 }, ticket: '', gate: null, gate_gen: null, question: null, status: '', title: '' },
  url: `https://hub.example.test:4443/#/w/${W}/settings/eyJncmFudF9ob3VycyI6MjR9`,
}

function full() {
  const real = api.getWorkspaces.bind(api)
  vi.spyOn(api, 'getWorkspaces').mockImplementation(async () => (await real()).map((w) => ({ ...w, signing: 'full' as const })))
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); setFeatures(null) })

describe('a settings change through the hub', () => {
  it('shows the change in words and the hub link, and prepares nothing here', async () => {
    const s = spies()
    const hub = vi.spyOn(api, 'hubSign').mockResolvedValue(INFO)
    mountPanel({ action: 'settings_set', grant_hours: 24 })
    const panel = await screen.findByTestId('hub-settings', {}, T)
    expect(within(panel).getByText('Longest agent access: 1 day')).toBeVisible()
    expect(within(panel).getByTestId('hub-link')).toHaveAttribute('href', INFO.url)
    expect(hub).toHaveBeenCalledWith(expect.any(String), { kind: 'settings', set: { grant_hours: 24 } })
    expect(s.prepare).not.toHaveBeenCalled()
    expect(screen.queryByText(/Refused/)).toBeNull()
  })

  it('a setting the hub may not change says so plainly and asks the host for nothing', async () => {
    const hub = vi.spyOn(api, 'hubSign')
    mountPanel({ action: 'settings_set', repos: ['x'] } as unknown as HandoffAction)
    const off = await screen.findByTestId('hub-settings-off', {}, T)
    expect(off).toHaveTextContent('the code folders')
    expect(off).toHaveTextContent('leave the hub')
    expect(hub).not.toHaveBeenCalled()
  })
})

describe('a settings change in a Full-mode workspace (A1)', () => {
  it('the review does not route to the hub by the workspace mode', async () => {
    const hub = vi.spyOn(api, 'hubSign').mockResolvedValue(INFO)
    full()
    const { ws, client } = setup()
    render(<QueryClientProvider client={client}><SignPopover ws={ws} title="Change the workspace settings" action={{ action: 'settings_set', grant_hours: 24 }} onClose={() => {}} /></QueryClientProvider>)
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(within(dialog).queryByTestId('hub-settings')).toBeNull()
    expect(within(dialog).queryByTestId('hub-link')).toBeNull()
    expect(hub).not.toHaveBeenCalled()
  })
})
