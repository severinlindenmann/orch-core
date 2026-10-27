import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configure, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { HubSignInfo } from '@/api/hubsign'
import { ApiError, type Workspace } from '@/api/types'
import { workspaceRequest } from '@/test/hostHarness'
import { state } from '@/test/handoffHarness'
import { BuiltInAddOns } from '../settings/host/AddOns'
// FB15 / run 12a: the Add-ons switch in a Full-mode workspace ends in the hub approval, not in a refusal.
configure({ asyncUtilTimeout: 15000 })
vi.mock('../settings/host/useViewerRole', () => ({ useViewerRole: () => 'owner' }))
vi.mock('@/components/sign/SignMethods', () => ({ useSignMethods: () => ({ allowed: ['click'], loading: false }) }))
vi.mock('@/api/click', async importOriginal => ({ ...await importOriginal<object>(), clickReady: () => true }))
afterEach(() => { cleanup(); vi.restoreAllMocks() })

const W = 'a'.repeat(32)
const ROW = { id: 'wiki', title: 'Wiki', desc: '', state: 'off' as const, locked: null, blocked: null, version: '0.1.0' }
const INFO = { mode: 'full', kind: 'addon', short_hash: 'aaaa aaaa aaaa', url: `https://hub.example.test:4443/#/w/${W}/addon/eyJhIjoxfQ`, summary: { kind: 'addon', ticket: '', gate: null, gate_gen: null, question: null, status: '', title: '', name: 'wiki', version: '0.1.0', capabilities: [] } } as HubSignInfo

function press(workspace: Partial<Workspace>) {
  const request = { ...workspaceRequest('addon.grant', { type: 'addon.granted', name: 'wiki' }), id: 'hr_01J9ZP0000000000000000HND1' }
  vi.spyOn(api, 'prepareHandoff').mockResolvedValue(request)
  vi.spyOn(api, 'getHandoff').mockResolvedValue(state(request))
  vi.spyOn(api, 'hubSign').mockResolvedValue(INFO)
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([{ id: W, ...workspace }] as Workspace[])
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><BuiltInAddOns workspace={{ id: W, ...workspace } as Workspace} rows={[ROW]} /></QueryClientProvider>)
  fireEvent.click(screen.getByRole('switch', { name: 'Wiki' }))
}

describe('Add-ons switch in a Full-mode workspace', () => {
  it('shows the hub approval button, no refusal', async () => {
    const click = vi.spyOn(api, 'prepareClick')
    press({ signing: 'full' })
    expect(await screen.findByTestId('hub-link')).toHaveAttribute('href', INFO.url)
    expect(click).not.toHaveBeenCalled()
    expect(screen.queryByText(/Approvals go through the hub in this workspace/)).toBeNull()
  })
  it('a stale workspace card: the host\'s sign.hub_only turns the review into the hub approval', async () => {
    vi.spyOn(api, 'prepareClick').mockRejectedValue(new ApiError(403, { code: 'sign.hub_only', message: 'Approvals go through the hub in this workspace: approve on your phone or in the hub.', retryable: false }))
    press({})
    expect(await screen.findByTestId('hub-link')).toHaveAttribute('href', INFO.url)
  })
})
