// "Create and start agent" on the REAL one-click path (SignPopover and useClickSign are not mocked): a click sequence
// that stops at the agent access, and one whose runner hand-over is not confirmed, never start an agent and never leave
// a second ticket to be created from the same typed text.
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { agentsApi } from '@/api/agents'
import { api } from '@/api/client'
import { ARM_MS, CLICK_RISK, type ClickViewAnswer } from '@/api/click'
import { routingApi } from '@/api/modelRouting'
import { ApiError } from '@/api/types'
import { renderApp } from '@/test/renderApp'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), handoffMode: () => true }))

const T = { timeout: 8000 }
const REQ = 'hr_' + '0'.repeat(25) + '1'
const DIGEST = 'sha256:' + 'c'.repeat(64)
const view = (): ClickViewAnswer => ({
  view_digest: DIGEST,
  view: {
    risk: CLICK_RISK, op: 'new', workspace: { id: 'x'.repeat(32), name: 'billing', prefix: 'DEMO' }, fingerprint: '0123456789ab',
    lines: ['Create DEMO-0044: Export CSV'], checks: [], confirm: { kind: 'click' },
    expires_at: new Date(Date.now() + 600_000).toISOString(), auth: 'convenience', device: 'd_x', artifacts: null,
  },
})
let s: { preview: ReturnType<typeof vi.spyOn>; start: ReturnType<typeof vi.spyOn>; sign: ReturnType<typeof vi.spyOn>; state: ReturnType<typeof vi.spyOn> }

beforeEach(() => {
  const real = api.getWorkspaces.bind(api)
  vi.spyOn(api, 'getWorkspaces').mockImplementation(async () => (await real()).map((w) => ({ ...w, click: { custody: 'convenience' as const, enabled: true, available: true } })))
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ available: true, grant_hours: 168 } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(routingApi, 'choices').mockResolvedValue({ harnesses: [{ id: 'claude-code', label: 'Claude', models: [] }, { id: 'codex', label: 'Codex', models: [] }] })
  vi.spyOn(agentsApi, 'runner').mockResolvedValue({ runner: { connected: true, enabled: true, terminals: false, grant: null } })
  vi.spyOn(agentsApi, 'riskNotices').mockResolvedValue({ acknowledged: { start_agent: true } })
  vi.spyOn(api, 'prepareClick').mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'new', confirm: 'click' })
  vi.spyOn(api, 'viewClick').mockResolvedValue(view())
  vi.spyOn(api, 'cancelClick').mockResolvedValue({ id: REQ, state: 'cancelled' })
  s = {
    preview: vi.spyOn(agentsApi, 'preview').mockRejectedValue(new Error('must not be asked')),
    start: vi.spyOn(agentsApi, 'start').mockRejectedValue(new Error('must not start')),
    sign: vi.spyOn(api, 'signClick'),
    state: vi.spyOn(api, 'stateClick'),
  }
})
afterEach(async () => {
  cleanup()
  await new Promise((r) => setTimeout(r, 20))
  vi.restoreAllMocks()
})

async function signed() {
  const r = renderApp('/board')
  await waitFor(() => expect(screen.getByTestId('topbar-title')).not.toHaveTextContent(/^$/), T)
  await r.user.click(await screen.findByRole('button', { name: /New ticket/ }, T))
  const sheet = await screen.findByRole('dialog', { name: 'New ticket' }, T)
  await r.user.type(await within(sheet).findByLabelText('Title', {}, T), 'Export CSV')
  await r.user.type(within(sheet).getByLabelText(/^What should be true/), 'Empty months export a header row')
  await r.user.click(await within(sheet).findByRole('button', { name: 'Create and start' }, T))
  const dialog = (await screen.findAllByRole('dialog', {}, T)).find((d) => d.hasAttribute('data-handoff-dialog'))!
  expect(await within(dialog).findByTestId('not-signed-start')).toBeInTheDocument() // the real dialog carries the unsigned block
  await within(dialog).findByTestId('click-panel', {}, T)
  const go = within(dialog).getByRole('button', { name: 'Sign and start agent' })
  await waitFor(() => expect(go).toBeEnabled(), { timeout: ARM_MS + 3000 })
  fireEvent.pointerDown(go)
  fireEvent.click(go)
  await waitFor(() => expect(s.sign).toHaveBeenCalled(), { timeout: 3000 })
  return { r, sheet, dialog }
}

describe('the real click path', () => {
  it('stopped at the agent access: the ticket exists, no second ticket can be made, nothing starts, Start agent is offered', async () => {
    s.sign.mockRejectedValue(new ApiError(409, { code: 'human.click_refused', message: 'the host refused the grant. 4 of 5 signatures landed; the ticket DEMO-0044 stays a draft.', retryable: false, reason: 'stopped' }))
    s.state.mockResolvedValue({ id: REQ, state: 'bound', key: 'DEMO-0044', progress: { done: 4, of: 5, last: 'grant' } })
    const { r } = await signed()
    // the review folded into the button while signing and opens again with the error inside: look it up afresh
    await screen.findByTestId('click-problem', {}, T)
    const dialog = (await screen.findAllByRole('dialog', {}, T)).find((d) => d.hasAttribute('data-handoff-dialog'))!
    await waitFor(() => expect(s.state).toHaveBeenCalledWith(expect.any(String), REQ), T)
    await r.user.click(within(dialog).getAllByRole('button', { name: /^(Close|Cancel)$/ }).at(-1)!)
    const info = await screen.findByTestId('agent-no-access', {}, T)
    expect(info).toHaveTextContent('DEMO-0044 exists. Agent access is not confirmed yet')
    expect(s.preview).not.toHaveBeenCalled()
    expect(s.start).not.toHaveBeenCalled()
    // what was typed is cleared: a second Create would be an empty form, not a second ticket
    expect(Object.keys(localStorage).filter((k) => k.includes('new-ticket.draft'))).toHaveLength(0)
  })

  it.each(['rejected', 'unknown'] as const)('signed but the hand-over to the runner is %s: not "access given", no automatic start, the dialog is offered', async (deposit) => {
    s.sign.mockResolvedValue({ state: 'submitted', duplicate: false, key: 'DEMO-0044', seq: 9, deposit, deposit_id: REQ.slice(3), grant: 'gr_01M4Y46B485AXD4CBEJZ15Z1TV' })
    await signed()
    expect(await screen.findByTestId('agent-no-access', {}, T)).toHaveTextContent('DEMO-0044 exists')
    expect(s.preview).not.toHaveBeenCalled()
    expect(s.start).not.toHaveBeenCalled()
  })

  it('signed and the hand-over confirmed: the start goes on to preview for the ticket\'s own key', async () => {
    s.sign.mockResolvedValue({ state: 'submitted', duplicate: false, key: 'DEMO-0044', seq: 9, deposit: 'confirmed', deposit_id: REQ.slice(3), grant: 'gr_01M4Y46B485AXD4CBEJZ15Z1TV' })
    s.preview.mockRejectedValue(new ApiError(503, { code: 'host.unavailable', message: 'Host not available', retryable: true }))
    await signed()
    await waitFor(() => expect(s.preview).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ ticket: 'DEMO-0044' })), T)
    expect(s.start).not.toHaveBeenCalled()
  })
})
