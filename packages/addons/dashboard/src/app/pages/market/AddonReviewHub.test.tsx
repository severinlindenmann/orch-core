import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configure, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { CLICK_RISK } from '@/api/click'
import type { HubSignInfo } from '@/api/hubsign'
import { ApiError, type Workspace } from '@/api/types'
import { workspaceRequest } from '@/test/hostHarness'
import { state } from '@/test/handoffHarness'
import { BuiltInAddOns } from '../settings/host/AddOns'
// A1: the Add-ons switch in a Full-mode workspace signs locally; the hub approval is an optional extra.
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

describe('Add-ons switch in a Full-mode workspace (A1: local signing stays the default)', () => {
  it('signs locally first and offers the hub approval as an extra', async () => {
    const click = vi.spyOn(api, 'prepareClick').mockRejectedValue(new ApiError(500, { code: 'internal', message: 'stop here', retryable: false }))
    press({ signing: 'full' })
    await waitFor(() => expect(click).toHaveBeenCalled())
    expect(screen.queryByTestId('hub-link')).toBeNull()
  })
})

const hubStatus = (joined: boolean) => ({ mode: joined ? 'full' : 'standalone', joined, hub_id: joined ? 'ab'.repeat(8) : null, member: joined, link: joined ? 'bound' : null, reachable: null, not_told: false, owed: 0, reseal_needed: false, key_change_pending: false, log_sync_stuck: false, groups: null, snapshot_version: null }) as Awaited<ReturnType<typeof api.getHubStatus>>

describe('the one-click review offers the hub approval only when joined (run 13)', () => {
  function clickFlow(joined: boolean) {
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    const status = vi.spyOn(api, 'getHubStatus').mockResolvedValue(hubStatus(joined))
    vi.spyOn(api, 'prepareClick').mockResolvedValue({ request_id: 'req', expires_at: new Date(Date.now() + 600000).toISOString(), op: 'addon.grant', confirm: 'click' })
    vi.spyOn(api, 'viewClick').mockResolvedValue({ view_digest: 'sha256:' + 'a'.repeat(64), view: { risk: CLICK_RISK, op: 'addon.grant', workspace: { id: W, name: 'Test', prefix: 'T' }, fingerprint: '0123456789ab', lines: ['version 0.1.0'], checks: [], confirm: { kind: 'click' }, expires_at: new Date(Date.now() + 600000).toISOString(), auth: 'convenience', device: 'd_test', artifacts: null, content: { headline: 'Turn on Wiki', summary: { title: 'Turn on Wiki', lines: ['It declares no capabilities.'], risk: 'An add-on runs its own code.' } } } } as Awaited<ReturnType<typeof api.viewClick>>)
    const sign = vi.spyOn(api, 'signClick').mockImplementation(() => new Promise(() => {}))
    press({ signing: joined ? 'full' : 'standalone' })
    return { status, sign }
  }
  it('joined in full mode: "Approve in the hub instead" sits next to the one-click approval', async () => {
    const { sign } = clickFlow(true)
    await waitFor(() => expect(sign).toHaveBeenCalled())
    expect(await screen.findByTestId('click-panel')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Approve in the hub instead' })).toBeInTheDocument()
  })
  it('never joined: no hub approval is offered on the one-click review', async () => {
    const { status, sign } = clickFlow(false)
    await waitFor(() => expect(sign).toHaveBeenCalled())
    await waitFor(() => expect(status).toHaveBeenCalled())
    expect(await screen.findByTestId('click-panel')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve in the hub instead' })).toBeNull()
  })
})
