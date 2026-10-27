import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configure, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { HubSignInfo } from '@/api/hubsign'
import { ApiError, type Workspace } from '@/api/types'
import { workspaceRequest } from '@/test/hostHarness'
import { state } from '@/test/handoffHarness'
import { BuiltInAddOns } from '../settings/host/AddOns'
// A1: the Add-ons switch in a Full-mode workspace signs locally; the hub approval is an optional extra.
configure({ asyncUtilTimeout: 15000 })
vi.mock('../settings/host/useViewerRole', () => ({ useViewerRole: () => 'owner' }))
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

describe('Add-ons switch in a Full-mode workspace (A1: local signing stays the default)', () => {
  it('signs locally first and offers the hub approval as an extra', async () => {
    const click = vi.spyOn(api, 'prepareClick').mockRejectedValue(new ApiError(500, { code: 'internal', message: 'stop here', retryable: false }))
    press({ signing: 'full' })
    await waitFor(() => expect(click).toHaveBeenCalled())
    expect(screen.queryByTestId('hub-link')).toBeNull()
  })
})
