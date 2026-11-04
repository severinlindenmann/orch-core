import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configure, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { CLICK_RISK } from '@/api/click'
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
  /** C1: nothing signs by itself; the trusted press on the row's outcome button prepares and opens the one-click review. */
  async function openReview() {
    const row = await screen.findByTestId('addon-wiki-accordion')
    const outcome = within(row).getAllByRole('button').find(b => !/close|cancel/i.test(b.getAttribute('aria-label') ?? b.textContent ?? ''))!
    vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
    fireEvent.click(outcome)
    return screen.findByTestId('click-panel')
  }
  // FB15: the hub approval goes through the one narrow route (`POST /hub/approve/prepare`): the host prepares the grant
  // for the hub and this page shows the host's link; nothing is signed here.
  it('joined in full mode: "Approve in the hub instead" sits next to the one-click approval; pressing it shows the hub panel', async () => {
    const { sign } = clickFlow(true)
    const id = 'hr_01J9ZP0000000000000000HND1'
    const expires_at = new Date(Date.now() + 600000).toISOString()
    const prepare = vi.spyOn(api, 'prepareHubApproval').mockResolvedValue({ ...INFO, id, state: 'prepared', expires_at })
    vi.spyOn(api, 'getHubApproval').mockResolvedValue({ id, state: 'prepared', expires_at, error: null })
    expect(await openReview()).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Approve in the hub instead' }))
    expect(await screen.findByTestId('hub-addon')).toBeInTheDocument()
    expect(screen.getByTestId('hub-link')).toHaveAttribute('href', INFO.url)
    expect(prepare).toHaveBeenCalledTimes(1)
    expect(prepare.mock.calls[0]).toEqual([W, { action: 'addon_grant', name: 'wiki' }])
    expect(screen.queryByTestId('click-panel')).toBeNull()
    expect(sign).not.toHaveBeenCalled()
  })
  it('never joined: no hub approval is offered on the one-click review, and nothing signs by itself', async () => {
    const { sign } = clickFlow(false)
    expect(await openReview()).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve in the hub instead' })).toBeNull()
    expect(sign).not.toHaveBeenCalled()
  })
})
