import { cleanup, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { setFeatures } from '@/api/features'
import type { HubSignInfo } from '@/api/hubsign'

const T = { timeout: 5000 }
const W = 'a'.repeat(32)
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  setFeatures(null)
})

describe('an add-on approval in a full-mode workspace (FB15)', () => {
  it('prepares the grant as a hand-off, shows the hub link for it and signs nothing here', async () => {
    const { workspaceRequest } = await import('@/test/hostHarness')
    const request = { ...workspaceRequest('addon.grant', { type: 'addon.granted', name: 'wiki' }), id: 'hr_01J9ZP0000000000000000HND1' }
    const prepare = vi.spyOn(api, 'prepareHandoff').mockResolvedValue(request)
    const { state } = await import('@/test/handoffHarness')
    vi.spyOn(api, 'getHandoff').mockResolvedValue(state(request))
    const click = vi.spyOn(api, 'prepareClick')
    const hub = vi.spyOn(api, 'hubSign').mockResolvedValue({
      mode: 'full', kind: 'addon', short_hash: 'aaaa aaaa aaaa', url: `https://hub.example.test:4443/#/w/${W}/addon/eyJhIjoxfQ`, other_url: `http://100.64.0.9:8100/#/w/${W}/addon/eyJhIjoxfQ`,
      summary: { kind: 'addon', ticket: '', gate: null, gate_gen: null, question: null, status: '', title: '', name: 'wiki', version: '1.2.0', capabilities: ['network'] },
    } as HubSignInfo)
    vi.spyOn(api, 'getWorkspaces').mockResolvedValue([{ id: W, signing: 'full' }] as unknown as Awaited<ReturnType<typeof api.getWorkspaces>>)
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    const { render } = await import('@testing-library/react')
    const { SignPopover } = await import('./SignPopover')
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SignPopover forceHub ws={W} title="Turn on wiki" action={{ action: 'addon_grant', name: 'wiki' } as never} autoStart onClose={() => {}} /></QueryClientProvider>)
    const dialog = await screen.findByRole('dialog', {}, T)
    const panel = await within(dialog).findByTestId('hub-addon', {}, T)
    expect(prepare).toHaveBeenCalled()
    expect(hub).toHaveBeenCalledWith(expect.any(String), { kind: 'addon', request: 'hr_01J9ZP0000000000000000HND1' })
    expect(within(panel).getByTestId('hub-link')).toHaveAttribute('href', `https://hub.example.test:4443/#/w/${W}/addon/eyJhIjoxfQ`)
    expect(within(panel).getByTestId('hub-link-other')).toHaveTextContent(`http://100.64.0.9:8100/#/w/${W}/addon/eyJhIjoxfQ`)
    expect(within(panel).getByText(/use the network/)).toBeVisible()
    expect(within(dialog).queryByRole('button', { name: /^Sign/ })).toBeNull()
    expect(click).not.toHaveBeenCalled()
  })
})
