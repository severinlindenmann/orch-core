// "Create and start agent" on the REAL one-click path (SignPopover and useClickSign are not mocked): a click sequence
// that stops at the agent access, and one whose runner hand-over is not confirmed, never start an agent and never leave
// a second ticket to be created from the same typed text.
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { agentsApi } from '@/api/agents'
import { api, mockStore } from '@/api/client'
import { ARM_MS, CLICK_RISK, type ClickViewAnswer } from '@/api/click'
import { routingApi } from '@/api/modelRouting'
import { ApiError, type TicketDocument } from '@/api/types'
import { clearMark, markOf, phaseOf } from '@/app/pages/ticket/progress'
import * as gesture from '@/components/sign/trustedPress'
import { EXECUTION_SHORT } from '@/app/terminal/real/executionLine'
import { renderApp } from '@/test/renderApp'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), handoffMode: () => true }))

const T = { timeout: 8000 }
const REQ = 'hr_' + '0'.repeat(25) + '1'
const DIGEST = 'sha256:' + 'c'.repeat(64)
const failedOf = async (key = 'DEMO-0044') => {
  const ws = mockStore.workspaces[0].id
  await waitFor(() => expect(markOf(ws, key)?.failed).toBeDefined(), T)
  return markOf(ws, key)!.failed!
}
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
  vi.spyOn(gesture, 'trustedPress').mockReturnValue(true) // fireEvent is untrusted: stand in for a real press of the outcome button
  clearMark(mockStore.workspaces[0].id, 'DEMO-0044') // marks live in memory for the page's life: never one from the test before
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
    start: vi.spyOn(agentsApi, 'startAfter').mockRejectedValue(new Error('must not start')),
    sign: vi.spyOn(api, 'signClick'),
    state: vi.spyOn(api, 'stateClick'),
  }
})
afterEach(async () => {
  cleanup()
  await new Promise((r) => setTimeout(r, 20))
  vi.restoreAllMocks()
})

async function signed(beforeSign?: (dialog: HTMLElement) => void) {
  const r = renderApp('/board')
  await waitFor(() => expect(screen.getByTestId('topbar-title')).not.toHaveTextContent(/^$/), T)
  await r.user.click(await screen.findByRole('button', { name: /New ticket/ }, T))
  const sheet = await screen.findByRole('dialog', { name: 'New ticket' }, T)
  await r.user.type(await within(sheet).findByLabelText('Title', {}, T), 'Export CSV')
  await r.user.type(within(sheet).getByLabelText(/^What should be true/), 'Empty months export a header row')
  await r.user.click(await within(sheet).findByRole('button', { name: 'Create and start' }, T))
  const dialog = (await screen.findAllByRole('dialog', {}, T)).find((d) => d.hasAttribute('data-handoff-dialog'))!
  expect(await within(dialog).findByTestId('not-signed-start')).toBeInTheDocument() // the real dialog carries the unsigned block
  expect(api.prepareClick).not.toHaveBeenCalled() // opening the dialog prepares nothing
  // the trusted press of the outcome button prepares; the review then shows and its armed Sign signs
  fireEvent.click(await within(dialog).findByRole('button', { name: 'Sign and start agent' }, T))
  await within(dialog).findByTestId('click-panel', {}, T)
  expect(s.sign).not.toHaveBeenCalled()
  const go = within(dialog).getByRole('button', { name: 'Sign and start agent' })
  await waitFor(() => expect(go).toBeEnabled(), { timeout: ARM_MS + 3000 })
  beforeSign?.(dialog)
  fireEvent.pointerDown(go)
  fireEvent.click(go)
  await waitFor(() => expect(s.sign).toHaveBeenCalled(), { timeout: 3000 })
  return { r, sheet, dialog }
}

describe('the real click path', () => {
  it('stopped at the agent access: the ticket exists, no second ticket can be made, nothing starts, the failure is marked', async () => {
    s.sign.mockRejectedValue(new ApiError(409, { code: 'human.click_refused', message: 'the host refused the grant. 4 of 5 signatures landed; the ticket DEMO-0044 stays a draft.', retryable: false, reason: 'stopped' }))
    s.state.mockResolvedValue({ id: REQ, state: 'bound', key: 'DEMO-0044', progress: { done: 4, of: 5, last: 'grant' } })
    const { r } = await signed()
    // the review folded into the button while signing and opens again with the error inside: look it up afresh
    await screen.findByTestId('click-problem', {}, T)
    const dialog = (await screen.findAllByRole('dialog', {}, T)).find((d) => d.hasAttribute('data-handoff-dialog'))!
    await waitFor(() => expect(s.state).toHaveBeenCalledWith(expect.any(String), REQ), T)
    await r.user.click(within(dialog).getAllByRole('button', { name: /^(Close|Cancel)$/ }).at(-1)!)
    const failed = await failedOf()
    expect(failed.reason).toBe('Agent access is not confirmed yet. Start an agent to give access.')
    expect(s.preview).not.toHaveBeenCalled()
    expect(s.start).not.toHaveBeenCalled()
    // what was typed is cleared: a second Create would be an empty form, not a second ticket
    expect(Object.keys(localStorage).filter((k) => k.includes('new-ticket.draft'))).toHaveLength(0)
  })

  it.each(['rejected', 'unknown'] as const)('signed but the hand-over to the runner is %s: not "access given", no automatic start, the failure is marked', async (deposit) => {
    s.sign.mockResolvedValue({ state: 'submitted', duplicate: false, key: 'DEMO-0044', seq: 9, deposit, deposit_id: REQ.slice(3), grant: 'gr_01M4Y46B485AXD4CBEJZ15Z1TV' })
    await signed()
    expect((await failedOf()).reason).toBeTruthy()
    expect(s.preview).not.toHaveBeenCalled()
    expect(s.start).not.toHaveBeenCalled()
  })

  it.each([
    ['local (no hub)', 'standalone'],
    ['hub', 'selective'],
  ] as const)('%s One click workspace, "runs as you" never read before: signing starts the agent, with "Getting started…"', async (_mode, signing) => {
    const listed = api.getWorkspaces as unknown as ReturnType<typeof vi.spyOn>
    const before = listed.getMockImplementation() as () => Promise<Awaited<ReturnType<typeof api.getWorkspaces>>>
    listed.mockImplementation(async () => (await before()).map((w) => ({ ...w, signing })))
    const notices = (agentsApi.riskNotices as unknown as ReturnType<typeof vi.spyOn>).mockResolvedValue({ acknowledged: {} })
    const ack = vi.spyOn(agentsApi, 'acknowledge').mockResolvedValue({ acknowledged: { start_agent: true } })
    const GRANT = 'gr_01M4Y46B485AXD4CBEJZ15Z1TV'
    s.sign.mockResolvedValue({ state: 'submitted', duplicate: false, key: 'DEMO-0044', seq: 9, deposit: 'confirmed', deposit_id: REQ.slice(3), grant: GRANT })
    s.preview.mockResolvedValue({
      spec: 'f'.repeat(64), ticket: 'DEMO-0044', display: 'claude "work on DEMO-0044"', display_hidden: false, model: null, model_source: 'default',
      cwd: { kind: 'workspace' }, cwd_path: '/w/billing', cwd_path_hidden: false, will_create_worktree: false, login_shell: false,
      env: ['TERM'], grant: { id: GRANT, until: new Date(Date.now() + 86_400_000).toISOString(), scope: 'workable' }, blocked: null, warnings: [],
    })
    s.start.mockResolvedValue({ run: { run: 'r_01M4Y46B485AXD4CBEJZ15Z1TV' } as never })
    const { r } = await signed((dialog) => {
      const notice = within(dialog).getByTestId('not-signed-notice') // the review shows the notice
      expect(within(notice).getByTestId('not-signed-execution-line')).toHaveTextContent(EXECUTION_SHORT)
      expect(notice).toHaveTextContent('Signing means you know this.')
      expect(EXECUTION_SHORT).toMatch(/as you/); expect(EXECUTION_SHORT).toMatch(/this computer/); expect(EXECUTION_SHORT).toMatch(/open your files/)
    })
    expect(notices).toHaveBeenCalled()
    await waitFor(() => expect(s.start).toHaveBeenCalledTimes(1), T)
    expect(s.start.mock.calls[0][1]).toBe(REQ)
    expect(s.preview).not.toHaveBeenCalled()
    await waitFor(() => expect(ack).toHaveBeenCalledWith(expect.any(String), 'start_agent'), T)
    await waitFor(() => expect(r.router.state.location.pathname).toBe('/ticket/DEMO-0044'), T)
    const mark = markOf(mockStore.workspaces[0].id, 'DEMO-0044')
    expect(mark?.failed).toBeUndefined()
    expect(phaseOf({ status: 'draft', claim: null } as unknown as TicketDocument, { needsYou: false, mark, run: null, now: Date.now() }).kind).toBe('starting')
  })

  it('"runs as you" never read, One click off: nothing is prepared or signed here, so the notice is not saved as read and nothing starts', async () => {
    const listed = api.getWorkspaces as unknown as ReturnType<typeof vi.spyOn>
    const before = listed.getMockImplementation() as () => Promise<Awaited<ReturnType<typeof api.getWorkspaces>>>
    listed.mockImplementation(async () => (await before()).map((w) => ({ ...w, click: { custody: 'convenience' as const, enabled: false, available: true } })))
    const GRANT = { id: 'gr_01M4Y46B485AXD4CBEJZ15Z1TV', until: new Date(Date.now() + 86_400_000).toISOString(), scope: 'workable' as const }
    ;(agentsApi.runner as unknown as ReturnType<typeof vi.spyOn>).mockResolvedValue({ runner: { connected: true, enabled: true, terminals: false, grant: GRANT } })
    ;(agentsApi.riskNotices as unknown as ReturnType<typeof vi.spyOn>).mockResolvedValue({ acknowledged: {} })
    const ack = vi.spyOn(agentsApi, 'acknowledge').mockResolvedValue({ acknowledged: { start_agent: true } })
    s.preview.mockResolvedValue({
      spec: 'f'.repeat(64), ticket: 'DEMO-0044', display: 'claude "work on DEMO-0044"', display_hidden: false, model: null, model_source: 'default',
      cwd: { kind: 'workspace' }, cwd_path: '/w/billing', cwd_path_hidden: false, will_create_worktree: false, login_shell: false,
      env: ['TERM'], grant: GRANT, blocked: null, warnings: [],
    })
    const r = renderApp('/board')
    await waitFor(() => expect(screen.getByTestId('topbar-title')).not.toHaveTextContent(/^$/), T)
    await r.user.click(await screen.findByRole('button', { name: /New ticket/ }, T))
    const sheet = await screen.findByRole('dialog', { name: 'New ticket' }, T)
    await r.user.type(await within(sheet).findByLabelText('Title', {}, T), 'Export CSV')
    await r.user.type(within(sheet).getByLabelText(/^What should be true/), 'Empty months export a header row')
    await r.user.click(await within(sheet).findByRole('button', { name: 'Create and start' }, T))
    const dialog = (await screen.findAllByRole('dialog', {}, T)).find((d) => d.hasAttribute('data-handoff-dialog'))!
    expect(await within(dialog).findByText('One click is not ready on this computer.', {}, T)).toBeInTheDocument()
    expect(within(dialog).queryByTestId('not-signed-notice')).toBeNull() // no review beside it to be read with
    await new Promise((r) => setTimeout(r, 200))
    expect(api.prepareClick).not.toHaveBeenCalled()
    expect(s.sign).not.toHaveBeenCalled()
    expect(s.start).not.toHaveBeenCalled()
    expect(ack).not.toHaveBeenCalled()
  })

  it('signed and the hand-over confirmed: the start names the reviewed request', async () => {
    s.sign.mockResolvedValue({ state: 'submitted', duplicate: false, key: 'DEMO-0044', seq: 9, deposit: 'confirmed', deposit_id: REQ.slice(3), grant: 'gr_01M4Y46B485AXD4CBEJZ15Z1TV' })
    s.start.mockRejectedValue(new ApiError(503, { code: 'host.unavailable', message: 'Host not available', retryable: true }))
    await signed()
    await waitFor(() => expect(s.start).toHaveBeenCalledWith(expect.any(String), REQ), T)
    await failedOf()
    expect(s.start).toHaveBeenCalledTimes(1)
  })
})

it.each([null, 14, 15, 16])('one review with %s minutes access: only a grant needs the second press', async (minutes) => {
  const { DEPOSIT_RISK } = await import('@/api/click')
  vi.spyOn(gesture, 'takeTrustedPress').mockReturnValueOnce(true)
  const shown = view()
  shown.view.headline = 'Create ticket and start agent'
  const needsGrant = minutes === null || minutes < 15
  if (minutes !== null) {
    const now = Date.now() // 15 means 15 minutes and a little: the page adds the time since the read (a stopwatch), so exactly 15 would be 14:59 by submit
    vi.mocked(agentsApi.runner).mockResolvedValue({ runner: {
      connected: true, enabled: true, terminals: false, now: new Date(now).toISOString(),
      grant: { id: 'gr_demo', until: new Date(now + minutes * 60_000 + (minutes >= 15 ? 30_000 : 0)).toISOString(), scope: 'workable', startable: true },
    } })
  }
  shown.view.op = needsGrant ? 'grant' : 'new'
  shown.view.risk_lines = needsGrant ? [DEPOSIT_RISK, 'Agents can run as you for 8 hours'] : []
  vi.mocked(api.viewClick).mockResolvedValue(shown)
  s.sign.mockResolvedValue({ state: 'submitted', duplicate: false, key: 'DEMO-0044', deposit: 'confirmed' })
  s.start.mockResolvedValue({ run: { run: 'r_test' } as never })
  const r = renderApp('/board')
  await r.user.click(await screen.findByRole('button', { name: /New ticket/ }, T))
  const sheet = await screen.findByRole('dialog', { name: 'New ticket' }, T)
  await r.user.type(await within(sheet).findByLabelText('Title'), 'Export CSV')
  await r.user.type(within(sheet).getByLabelText(/^What should be true/), 'A file is exported')
  await r.user.click(await within(sheet).findByRole('button', { name: 'Create and start' }, T))
  const panel = await screen.findByTestId('click-panel', {}, T)
  expect(api.prepareClick).toHaveBeenCalledTimes(1)
  const action = vi.mocked(api.prepareClick).mock.calls[0][1]
  expect(action).toMatchObject({ action: 'new_sequence', launch: { harness: expect.any(String), mode: expect.any(String) } })
  if (needsGrant) {
    expect(action).toHaveProperty('agent.hours', 168) // the workspace's access setting (#1555), here 168 h; the mocked signer's line below is its own
    expect(panel).toHaveTextContent('Agents can run as you for 8 hours')
    const button = screen.getByRole('button', { name: 'Sign and start agent' })
    expect(screen.queryByTestId('sign-arm-status')).not.toBeInTheDocument()
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(s.sign).not.toHaveBeenCalled()
    expect(s.start).not.toHaveBeenCalled()
    await new Promise(resolve => setTimeout(resolve, 1100))
    expect(s.sign).not.toHaveBeenCalled()
    expect(s.start).not.toHaveBeenCalled()
    expect(screen.queryByTestId('sign-arm-status')).not.toBeInTheDocument()
    fireEvent.pointerDown(button)
    fireEvent.click(button)
  } else {
    expect(action).not.toHaveProperty('agent')
    expect(panel).not.toHaveTextContent(DEPOSIT_RISK)
  }
  await waitFor(() => expect(s.sign).toHaveBeenCalledTimes(1), T)
  await waitFor(() => expect(s.start).toHaveBeenCalledWith(expect.any(String), REQ), T)
  expect(api.viewClick).toHaveBeenCalledTimes(1)
})
