import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configure, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { HubSignInfo } from '@/api/hubsign'
import { ApiError, type Workspace } from '@/api/types'
import * as gesture from '@/components/sign/trustedPress'
import { BuiltInAddOns } from '../settings/host/AddOns'
// A1/D83: the Add-ons switch in a Full-mode workspace signs locally; the workspace's hub mode never routes the review.
configure({ asyncUtilTimeout: 15000 })
vi.mock('../settings/host/useViewerRole', () => ({ useViewerRole: () => 'owner' }))
vi.mock('@/api/click', async importOriginal => ({ ...await importOriginal<object>(), clickReady: () => true }))
afterEach(() => { cleanup(); vi.restoreAllMocks() })

const W = 'a'.repeat(32)
const ROW = { id: 'wiki', title: 'Wiki', desc: '', state: 'off' as const, locked: null, blocked: null, version: '0.1.0' }
const INFO = { mode: 'full', kind: 'addon', short_hash: 'aaaa aaaa aaaa', url: `https://hub.example.test:4443/#/w/${W}/addon/eyJhIjoxfQ`, summary: { kind: 'addon', ticket: '', gate: null, gate_gen: null, question: null, status: '', title: '', name: 'wiki', version: '0.1.0', capabilities: [] } } as HubSignInfo

function press(workspace: Partial<Workspace>) {
  vi.spyOn(api, 'hubSign').mockResolvedValue(INFO)
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([{ id: W, ...workspace }] as Workspace[])
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><BuiltInAddOns workspace={{ id: W, ...workspace } as Workspace} rows={[ROW]} /></QueryClientProvider>)
  fireEvent.click(screen.getByRole('switch', { name: 'Wiki' }))
}

describe('Add-ons switch in a Full-mode workspace (A1: local signing in every mode)', () => {
  it('opens the local review in the row; the trusted outcome press prepares locally, with no hub link', async () => {
    const click = vi.spyOn(api, 'prepareClick').mockRejectedValue(new ApiError(500, { code: 'internal', message: 'stop here', retryable: false }))
    press({ signing: 'full' })
    const row = await screen.findByTestId('addon-wiki-accordion')
    // the synthetic switch press grants no intent: nothing is prepared yet
    expect(click).not.toHaveBeenCalled()
    const outcome = within(row).getAllByRole('button').find(b => !/close|cancel/i.test(b.getAttribute('aria-label') ?? b.textContent ?? ''))!
    vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
    fireEvent.click(outcome)
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1))
    expect(click.mock.calls[0][0]).toBe(W)
    expect(screen.queryByTestId('hub-link')).toBeNull()
  })
})
