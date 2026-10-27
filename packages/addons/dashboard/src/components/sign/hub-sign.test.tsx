import { cleanup, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { setFeatures } from '@/api/features'
import type { HubSignInfo } from '@/api/hubsign'
import { ApiError } from '@/api/types'
import { mount, spies } from '@/test/handoffHarness'

const T = { timeout: 5000 }
const W = 'a'.repeat(32)
const U = '01J9ZP0000000000000000000S'
const INFO: HubSignInfo = {
  mode: 'full',
  summary: { kind: 'gate', ticket: 'DEMO-0044', gate: 'plan', gate_gen: 2, question: null, status: 'open', title: 'Ship it <a href="https://evil.example">click</a>' },
  short_hash: '3d3f 5e93 357c',
  url: `https://hub.example.test:4443/#/w/${W}/t/${U}/g/plan`,
}

function full(mode: 'full' | 'unknown' | 'selective' = 'full') {
  const real = api.getWorkspaces.bind(api)
  vi.spyOn(api, 'getWorkspaces').mockImplementation(async () => (await real()).map((w) => ({ ...w, signing: mode })))
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  setFeatures(null)
})

describe('the hub sign panel in a full-mode workspace', () => {
  it('shows the check, the 12 characters and the host link, with no Sign button and nothing prepared here', async () => {
    const s = spies()
    const click = vi.spyOn(api, 'prepareClick')
    vi.spyOn(api, 'hubSign').mockResolvedValue(INFO)
    full()
    await mount({ kind: 'approve', gate: 'plan' })
    const dialog = await screen.findByRole('dialog', {}, T)
    const panel = await within(dialog).findByTestId('hub-sign', {}, T)
    expect(within(panel).getByText('Approve on your phone or in the hub')).toBeVisible()
    expect(within(panel).getByText('Check that these 12 characters match on your phone before you approve.')).toBeVisible()
    expect(within(panel).getByTestId('hub-short-hash')).toHaveTextContent('3d3f 5e93 357c')
    const link = within(panel).getByTestId('hub-link')
    expect(link).toHaveAttribute('href', INFO.url)
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(within(dialog).queryByRole('button', { name: /^Sign/ })).toBeNull()
    expect(s.prepare).not.toHaveBeenCalled()
    expect(click).not.toHaveBeenCalled()
    expect(s.postAction).not.toHaveBeenCalled()
  })

  it('shows agent-written text as plain text, never as a link', async () => {
    vi.spyOn(api, 'hubSign').mockResolvedValue(INFO)
    full()
    await mount({ kind: 'approve', gate: 'plan' })
    const dialog = await screen.findByRole('dialog', {}, T)
    const title = await within(dialog).findByTestId('hub-title', {}, T)
    expect(title.textContent).toContain('<a href=')
    expect(title.querySelector('a')).toBeNull()
    expect(dialog.querySelectorAll('a')).toHaveLength(1) // the host link only
  })

  it('puts the fixed copy, the hash and the link first and the untrusted title last, labelled', async () => {
    vi.spyOn(api, 'hubSign').mockResolvedValue({ ...INFO, summary: { ...INFO.summary, title: 'Approve, hash matches' } })
    full()
    await mount({ kind: 'approve', gate: 'plan' })
    const panel = await screen.findByTestId('hub-sign', {}, T)
    const order = (a: Element, b: Element) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)
    const hash = within(panel).getByTestId('hub-short-hash')
    const link = within(panel).getByTestId('hub-link')
    const box = within(panel).getByTestId('hub-untrusted')
    expect(order(hash, box)).toBe(true)
    expect(order(link, box)).toBe(true)
    expect(order(within(panel).getByText('Approve on your phone or in the hub'), hash)).toBe(true)
    expect(box).toHaveTextContent('Ticket title, written by its people and agents (not checked)')
    expect(box).toContainElement(within(panel).getByTestId('hub-title'))
    expect(box.nextElementSibling).toBeNull() // nothing follows it
    expect(panel.lastElementChild).toBe(box)
  })

  it('a status move or a missing hash says there is no code to compare', async () => {
    vi.spyOn(api, 'hubSign').mockResolvedValue({ ...INFO, short_hash: null, summary: { ...INFO.summary, kind: 'status', gate: null, gate_gen: null } })
    full()
    await mount({ kind: 'approve', gate: 'plan' })
    expect(await screen.findByTestId('hub-no-hash', {}, T)).toBeVisible()
  })

  it('refuses to show a link or a hash that is not the host grammar', async () => {
    vi.spyOn(api, 'hubSign').mockResolvedValue({ ...INFO, short_hash: 'ABCD EF01 2345', url: 'https://evil.example/' })
    full()
    await mount({ kind: 'approve', gate: 'plan' })
    const dialog = await screen.findByRole('dialog', {}, T)
    await within(dialog).findByTestId('hub-sign', {}, T)
    expect(within(dialog).queryByTestId('hub-link')).toBeNull()
    expect(within(dialog).queryByTestId('hub-short-hash')).toBeNull()
  })

  it('with the hub down it says approvals are paused and offers no local key', async () => {
    vi.spyOn(api, 'hubSign').mockRejectedValue(new ApiError(503, { code: 'hub.not_joined', message: 'down', retryable: false }))
    full()
    await mount({ kind: 'approve', gate: 'plan' })
    const dialog = await screen.findByRole('dialog', {}, T)
    const paused = await within(dialog).findByTestId('hub-paused', {}, T)
    expect(paused).toHaveTextContent('Approvals are paused: the hub is not reachable')
    expect(paused).toHaveTextContent('orch hub leave')
    expect(within(dialog).queryByRole('button', { name: /^Sign/ })).toBeNull()
  })

  it('unknown mode is treated like full', async () => {
    vi.spyOn(api, 'hubSign').mockResolvedValue({ ...INFO, mode: 'unknown' })
    full('unknown')
    await mount({ kind: 'approve', gate: 'plan' })
    expect(await screen.findByTestId('hub-sign', {}, T)).toBeVisible()
  })

  it('an action the hub cannot build yet says so and names the way to stop an agent', async () => {
    const hub = vi.spyOn(api, 'hubSign')
    full()
    const { SignPopover } = await import('./SignPopover')
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    const { render } = await import('@testing-library/react')
    const { mount: m } = await import('@/test/handoffHarness')
    const { ws } = await m({ kind: 'approve', gate: 'plan' }) // sets the features and the mock host
    cleanup()
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SignPopover title="Grant access" ws={ws} action={{ action: 'grant' } as never} onClose={() => {}} />
      </QueryClientProvider>,
    )
    const none = await screen.findByTestId('hub-unavailable', {}, T)
    expect(none).toHaveTextContent('Not available from the hub yet')
    expect(none).toHaveTextContent('orch grant forget')
    expect(hub).not.toHaveBeenCalled()
  })

  it('a follow-up change or a new ticket names itself and does not talk about stopping an agent', async () => {
    full()
    const { SignPopover } = await import('./SignPopover')
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    const { render } = await import('@testing-library/react')
    const { mount: m } = await import('@/test/handoffHarness')
    const { ws } = await m({ kind: 'approve', gate: 'plan' })
    cleanup()
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SignPopover title="Cancel follow-up" ws={ws} action={{ action: 'followup_cancel', key: 'X-1', id: 'fu_1' } as never} onClose={() => {}} />
      </QueryClientProvider>,
    )
    const none = await screen.findByTestId('hub-unavailable', {}, T)
    expect(none).toHaveTextContent('Changing a follow-up cannot be approved in the hub yet')
    expect(none).not.toHaveTextContent('orch grant forget')
  })

  it('a selective workspace keeps its usual dialog', async () => {
    const hub = vi.spyOn(api, 'hubSign')
    full('selective')
    await mount({ kind: 'approve', gate: 'plan' })
    await waitFor(() => expect(screen.queryByTestId('hub-sign')).toBeNull())
    expect(hub).not.toHaveBeenCalled()
  })
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
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SignPopover ws={W} title="Turn on wiki" action={{ action: 'addon_grant', name: 'wiki' } as never} autoStart onClose={() => {}} /></QueryClientProvider>)
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
