import { cleanup, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore, resetMockStoreForTests } from '@/api/client'
import { setFeatures } from '@/api/features'
import type { HubSignInfo } from '@/api/hubsign'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { spies } from '@/test/handoffHarness'
import { SignPopover } from './SignPopover'
import type { HandoffAction } from '@/api/handoff'

// Settings Save in a Full-mode workspace ends in the hub approval (run 12 rerun 2, 4.4): no dead end, no raw refusal.
const T = { timeout: 5000 }
const W = 'a'.repeat(32)

async function mount(action: HandoffAction) {
  resetMockStoreForTests()
  mockStore.setViewer('p_sev')
  const ws = mockStore.workspaces[0].id
  setFeatures({ features: ['identity', 'today', 'tickets', 'board', 'ticket', 'actions'], workspaces: { [ws]: ['identity', 'today', 'tickets', 'board', 'ticket', 'actions'] } })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><SignPopover forceHub ws={ws} title="Change the workspace settings" action={action} autoStart onClose={() => {}} /></QueryClientProvider>)
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

describe('a settings change in a Full-mode workspace', () => {
  it('shows the change in words and the hub link, and prepares nothing here', async () => {
    const s = spies()
    const hub = vi.spyOn(api, 'hubSign').mockResolvedValue(INFO)
    full()
    await mount({ action: 'settings_set', grant_hours: 24 })
    const dialog = await screen.findByRole('dialog', {}, T)
    const panel = await within(dialog).findByTestId('hub-settings', {}, T)
    expect(within(panel).getByText('Longest agent access: 1 day')).toBeVisible()
    expect(within(panel).getByTestId('hub-link')).toHaveAttribute('href', INFO.url)
    expect(hub).toHaveBeenCalledWith(expect.any(String), { kind: 'settings', set: { grant_hours: 24 } })
    expect(s.prepare).not.toHaveBeenCalled()
    expect(within(dialog).queryByText(/Refused/)).toBeNull()
  })

  it('a setting the hub may not change says so plainly and asks the host for nothing', async () => {
    const hub = vi.spyOn(api, 'hubSign')
    full()
    await mount({ action: 'settings_set', sign_methods: ['click'] })
    const dialog = await screen.findByRole('dialog', {}, T)
    const off = await within(dialog).findByTestId('hub-settings-off', {}, T)
    expect(off).toHaveTextContent('the allowed ways to approve')
    expect(off).toHaveTextContent('leave the hub')
    expect(hub).not.toHaveBeenCalled()
  })
})
