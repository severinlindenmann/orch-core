// M7b Start agent on a real host: the preview before anything starts, the approved texts verbatim, Start with the
// previewed values and one idem, `launch.changed`, blocked reasons, and the runner grant card (gate, refusals, the three
// deposit outcomes, Forget). The server and the signer are faked at the api boundary.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routingApi, type RoutingChoices } from '@/api/modelRouting'
import { agentsApi, LOGIN_NOTICE, type AgentPreview, type Runner } from '@/api/agents'
import { api } from '@/api/client'
import { ARM_MS, CLICK_RISK, DEPOSIT_LINE, DEPOSIT_RISK, type ClickViewAnswer } from '@/api/click'
import { mergeRunItems, type Run, type RunList } from '@/api/runs'
import { ApiError } from '@/api/types'
import { nowMs } from '@/lib/time'
import { TooltipProvider } from '@/components/ui/tooltip'
import * as gesture from '@/components/sign/trustedPress'
import { openInfo } from '@/test/info'
import { EXECUTION_LINE } from './executionLine'
import { RunnerGrantCard, StartAgentDialog } from './StartAgentDialog'

const WS = 'a'.repeat(32)
const T = { timeout: 5000 }
const GRANT = 'gr_01M4Y46B485AXD4CBEJZ15Z1TV'
const REQ = 'hr_01M4Y46B485AXD4CBEJZ15Z1TV'
const UNTIL = '2026-10-15T20:00:00Z'
const SOON = new Date(Date.now() + 2 * 86_400_000).toISOString() // a grant that is still valid

const preview = (over: Partial<AgentPreview> = {}): AgentPreview => ({
  spec: 'f'.repeat(64), ticket: 'DEMO-1', display: "claude 'Run orch status, then work on ticket DEMO-1.'", display_hidden: false,
  cwd: { kind: 'worktree', repo: 'billing', ticket: 'DEMO-1' }, cwd_path: '/w/billing.worktrees/0a1b2c3d-DEMO-1', cwd_path_hidden: false,
  will_create_worktree: true, login_shell: false, env: ['TERM', 'ORCH_GRANT', 'ORCH_SESSION'], grant: { id: GRANT, until: UNTIL, scope: 'workable' },
  blocked: null, warnings: [], ...over,
})
const runner = (over: Partial<Runner> = {}): Runner => ({ connected: true, enabled: true, terminals: false, grant: null, ...over })
const run = { run: 'r_01M4Y46B485AXD4CBEJZ15Z1TV', rev: 1, kind: 'agent', ticket: 'DEMO-1', harness: 'claude-code', mode: 'work', model: null, as_person: false,
  state: 'starting', started_at: UNTIL, viewers: 0, writer: false, cols: 120, rows: 40, replay_until: null, label: null, label_hidden: false,
  cwd: { kind: 'worktree' }, exit: null, grant_state: 'ok' } as Run
const grantView = (over: Partial<ClickViewAnswer['view']> = {}): ClickViewAnswer => ({
  view_digest: 'sha256:' + 'c'.repeat(64),
  view: {
    risk: CLICK_RISK, op: 'grant', workspace: { id: WS, name: 'billing', prefix: 'DEMO' }, fingerprint: '0123456789ab',
    lines: ['grant to agents, 8 hours, workable', '', DEPOSIT_RISK, DEPOSIT_LINE], checks: [], confirm: { kind: 'click' },
    expires_at: new Date(Date.now() + 600_000).toISOString(), auth: 'convenience', device: 'd_x', artifacts: null,
    deposit: true, deposit_line: DEPOSIT_LINE, ...over,
  },
})

const CHOICES: RoutingChoices = {
  harnesses: [
    { id: 'claude-code', label: 'Claude', models: [{ id: 'fable', label: 'Fable' }, { id: 'opus', label: 'Opus' }, { id: 'sonnet', label: 'Sonnet' }, { id: 'haiku', label: 'Haiku' }] },
    { id: 'codex', label: 'Codex', models: [{ id: 'gpt-5.4', label: 'GPT-5.4' }, { id: 'o3', label: 'o3' }] },
  ],
}

let s: Record<string, ReturnType<typeof vi.spyOn>>
beforeEach(() => {
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([{ id: WS, grant_hours: 168, click: { custody: 'convenience', enabled: true, available: true } }] as Awaited<ReturnType<typeof api.getWorkspaces>>)
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(routingApi, 'choices').mockResolvedValue(CHOICES)
  s = {
    preview: vi.spyOn(agentsApi, 'preview').mockResolvedValue(preview()),
    start: vi.spyOn(agentsApi, 'start').mockResolvedValue({ run }),
    runner: vi.spyOn(agentsApi, 'runner').mockResolvedValue({ runner: runner({ grant: { id: GRANT, until: SOON, scope: 'workable' } }) }),
    status: vi.spyOn(agentsApi, 'depositStatus').mockResolvedValue({ deposit: 'unknown' }),
    forget: vi.spyOn(agentsApi, 'forget').mockResolvedValue({ forgotten: true }),
    notices: vi.spyOn(agentsApi, 'riskNotices').mockResolvedValue({ acknowledged: {} }),
    acknowledge: vi.spyOn(agentsApi, 'acknowledge').mockResolvedValue({ acknowledged: { start_agent: true } }),
    prepare: vi.spyOn(api, 'prepareClick').mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'grant', confirm: 'click' }),
    view: vi.spyOn(api, 'viewClick').mockResolvedValue(grantView()),
    sign: vi.spyOn(api, 'signClick').mockResolvedValue({ state: 'submitted', duplicate: false, deposit: 'confirmed', deposit_id: REQ.slice(3), grant: GRANT }),
    cancel: vi.spyOn(api, 'cancelClick').mockResolvedValue({ id: REQ, state: 'cancelled' }),
  }
})
afterEach(async () => {
  cleanup()
  await new Promise((r) => setTimeout(r, 20))
  vi.restoreAllMocks()
})

function mount(onStarted = vi.fn(), onClose = vi.fn(), extra: { choose?: boolean; initial?: import('./StartAgentDialog').StartInitial } = { choose: true }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}><TooltipProvider>
      <StartAgentDialog ws={WS} ticket="DEMO-1" open onClose={onClose} onStarted={onStarted} {...extra} />
    </TooltipProvider></QueryClientProvider>,
  )
  return { qc, onStarted, onClose }
}
const card = () => screen.getByTestId('runner-grant-card')
/** The access review (it holds the length select, so it never prepares by itself): its outcome button prepares on a
 * real press. fireEvent is untrusted, so trustedPress stands in for the real press. Resolves to the signer's View. */
async function prepareAccess() {
  vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
  expect(s.prepare).not.toHaveBeenCalled() // nothing is prepared before the outcome button is pressed
  fireEvent.click(await screen.findByRole('button', { name: /^Sign and start agent$/ }, T))
  return screen.findByTestId('click-panel', {}, T)
}
/** The runner grant card on its own (Settings > Agent access uses it for Extend; Start agent signs through the popover). */
async function mountCard(over: Partial<Runner> = {}, onChanged = vi.fn()) {
  const p = await agentsApi.preview(WS, { harness: 'claude-code', mode: 'work', worktree: true })
  const r = runner(over)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><TooltipProvider><RunnerGrantCard ws={WS} runner={r} loading={false} blocked={p.blocked} onChanged={onChanged} /></TooltipProvider></QueryClientProvider>)
  return { onChanged }
}

describe('the preview', () => {
  it('previews first and shows the host values and the approved texts verbatim', async () => {
    mount()
    expect(await screen.findByTestId('agent-cwd', {}, T)).toHaveTextContent('A worktree for DEMO-1 in billing will be created at /w/billing.worktrees/0a1b2c3d-DEMO-1.')
    expect(s.preview).toHaveBeenCalledWith(WS, { ticket: 'DEMO-1', harness: 'claude-code', mode: 'work', worktree: true, policy: { delegate: false, max_children: 3 } })
    expect(screen.getByTestId('execution-line')).toHaveTextContent(EXECUTION_LINE)
    // The scope and end stay in view; only the grant id sits behind the "i".
    const grant = screen.getByTestId('agent-grant')
    expect(grant).toHaveTextContent('Tickets you may work on, until')
    expect(grant).not.toHaveTextContent(GRANT)
    expect(await openInfo(grant)).toHaveTextContent(GRANT)
  })

  it('says "shared working copy" in the approved words for cwd.kind repo', async () => {
    s.preview.mockResolvedValue(preview({ cwd: { kind: 'repo', repo: 'billing' }, will_create_worktree: false, cwd_path: '/w/billing' }))
    mount()
    expect(await screen.findByTestId('agent-cwd', {}, T)).toHaveTextContent("the repository's shared working copy (not a separate worktree)")
  })

  it('shows hidden host text as withheld and a host message only as text', async () => {
    s.preview.mockResolvedValue(preview({ display: null, display_hidden: true, blocked: { code: 'launch.claimed', message: '<b>claimed</b>', detail: {} } }))
    mount()
    const blocked = await screen.findByTestId('agent-blocked', {}, T)
    expect(blocked).toHaveTextContent('An agent session already claims this ticket.')
    expect(blocked.querySelector('b')).toBeNull()
    expect(screen.getByText('Hidden: the command has invisible characters')).toBeInTheDocument()
    expect(screen.getByTestId('agent-start')).toBeDisabled()
  })
})

describe('start settings wording and layout (MR3/MR4)', () => {
  it('says what the agent will do in one sentence, offers the agents as coloured choices and keeps the rest under More options', async () => {
    mount()
    expect(await screen.findByTestId('agent-sentence', {}, T)).toHaveTextContent('Claude Code will work through this ticket and submit it for your review.')
    expect(screen.getByRole('radio', { name: 'Claude Code' })).toBeChecked()
    expect(screen.getByRole('radio', { name: 'Codex' }).querySelector('[data-agent-icon="codex"]')).not.toBeNull()
    expect(await screen.findByLabelText('Model for helper agents', {}, T)).toBeInTheDocument()
    expect(screen.getByText('Project folder')).toBeInTheDocument()
    expect(screen.getByLabelText(/Use this ticket's separate project folder/)).toBeChecked()
    expect(screen.getByTestId('agent-more')).not.toHaveAttribute('open')
    expect(screen.queryByRole('button', { name: 'Check start settings' })).toBeNull() // the check runs by itself
    expect(screen.queryByTestId('agent-ready')).toBeNull() // no "Ready to start" line: the button says it
    const form = screen.getByLabelText('Label').closest('form')!
    expect(form.className).toContain('grid-cols-1')
    expect(form.className).toContain('sm:grid-cols-[110px_minmax(0,1fr)]')
  })
})

describe('P2-3: the delegation policy block', () => {
  it('is off by default, previews again with the limit when turned on, and is not offered for Codex', async () => {
    mount()
    const on = (await screen.findByLabelText('Let the main agent delegate', {}, T)) as HTMLInputElement
    expect(on).not.toBeChecked()
    s.preview.mockClear()
    fireEvent.click(on)
    fireEvent.change(await screen.findByLabelText('Most delegated tasks'), { target: { value: '5' } })
    fireEvent.submit(screen.getByLabelText('Label').closest('form')!)
    await waitFor(() => expect(s.preview).toHaveBeenLastCalledWith(WS, { ticket: 'DEMO-1', harness: 'claude-code', mode: 'work', worktree: true, policy: { delegate: true, max_children: 5 } }), T)
    fireEvent.click(screen.getByRole('radio', { name: 'Codex' }))
    expect(await screen.findByTestId('agent-policy')).toHaveTextContent('Codex: delegation limits not available')
    await waitFor(() => expect(s.preview.mock.calls.at(-1)![1]).not.toHaveProperty('policy'), T)
  })
})

describe('the model of a start', () => {
  it('shows the model an add-on chose, says who chose it, and keeps the command behind Details', async () => {
    s.preview.mockResolvedValue(preview({ model: 'sonnet', model_source: 'addon', model_by: 'model-routing' }))
    mount()
    expect(await screen.findByTestId('agent-model-line', {}, T)).toHaveTextContent('Sonnet (chosen by model-routing)')
    expect(screen.getByTestId('agent-details')).not.toHaveAttribute('open')
  })

  it('says "the agent\'s own default" when nothing is set', async () => {
    s.preview.mockResolvedValue(preview({ model: null, model_source: 'default' }))
    mount()
    expect(await screen.findByTestId('agent-model-line', {}, T)).toHaveTextContent('Chosen by the agent app')
  })

  it('the model is a choice from the list, never typed text; picking one previews again with that name', async () => {
    mount()
    const pick = (await screen.findByLabelText('Model', {}, T)) as HTMLSelectElement
    expect([...pick.options].map((o) => o.value)).toEqual(['', 'fable', 'opus', 'sonnet', 'haiku'])
    s.preview.mockResolvedValue(preview({ model: 'haiku', model_source: 'chosen' }))
    fireEvent.change(pick, { target: { value: 'haiku' } })
    expect(screen.getByTestId('agent-start')).toBeDisabled() // changed since the preview: preview again
    await waitFor(() => expect(s.preview).toHaveBeenLastCalledWith(WS, expect.objectContaining({ model: 'haiku' })), T)
    expect(await screen.findByTestId('agent-model-line')).toHaveTextContent('Haiku (your choice)')
    fireEvent.click(screen.getByTestId('agent-start'))
    await waitFor(() => expect(s.start).toHaveBeenCalledWith(WS, expect.objectContaining({ model: 'haiku' }), 'f'.repeat(64), expect.any(String)))
  })

  it('changing the agent drops a model that agent does not have', async () => {
    mount()
    const pick = (await screen.findByLabelText('Model', {}, T)) as HTMLSelectElement
    fireEvent.change(pick, { target: { value: 'opus' } })
    fireEvent.click(screen.getByRole('radio', { name: 'Codex' }))
    await waitFor(() => expect((screen.getByLabelText('Model') as HTMLSelectElement).value).toBe(''))
    expect([...(screen.getByLabelText('Model') as HTMLSelectElement).options].map((o) => o.value)).toEqual(['', 'gpt-5.4', 'o3'])
  })

  it('a failing check only suggests a stronger model; nothing changes until the person presses it', async () => {
    vi.spyOn(api, 'getTicket').mockResolvedValue({ tasks_state: [{ id: 't1', state: 'doing', receipt: { exit: 1, ms: 5 } }] } as unknown as Awaited<ReturnType<typeof api.getTicket>>)
    mount()
    const note = await screen.findByTestId('agent-strong-suggestion', {}, T)
    expect(note).toHaveTextContent('A check on this ticket is failing')
    expect((screen.getByLabelText('Model') as HTMLSelectElement).value).toBe('') // not changed by itself
    expect(s.start).not.toHaveBeenCalled()
    fireEvent.click(within(note).getByRole('button', { name: 'Use Fable' }))
    expect((screen.getByLabelText('Model') as HTMLSelectElement).value).toBe('fable')
    expect(screen.queryByTestId('agent-strong-suggestion')).toBeNull()
  })

  it('no suggestion when no check is failing', async () => {
    vi.spyOn(api, 'getTicket').mockResolvedValue({ tasks_state: [{ id: 't1', state: 'doing', receipt: { exit: 0, ms: 5 } }, { id: 't2', state: 'done', receipt: { exit: 1, ms: 5 } }] } as unknown as Awaited<ReturnType<typeof api.getTicket>>)
    mount()
    await screen.findByLabelText('Model', {}, T)
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByTestId('agent-strong-suggestion')).toBeNull()
  })
})

describe('start', () => {
  it('posts the previewed values, its spec and one idem; a retry keeps the idem', async () => {
    const { onStarted } = mount()
    await screen.findByTestId('agent-preview', {}, T)
    s.start.mockRejectedValueOnce(new ApiError(503, { code: 'outcome.unknown', message: 'lost', retryable: true }))
    fireEvent.click(screen.getByTestId('agent-start'))
    expect(await screen.findByTestId('agent-error', {}, T)).toHaveTextContent('lost')
    fireEvent.click(screen.getByTestId('agent-start'))
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith(run), T)
    const [a, b] = s.start.mock.calls
    expect(a[1]).toEqual({ ticket: 'DEMO-1', harness: 'claude-code', mode: 'work', worktree: true, policy: { delegate: false, max_children: 3 } })
    expect(a[2]).toBe('f'.repeat(64))
    expect(a[3]).toMatch(/^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$/)
    expect(b[3]).toBe(a[3])
  })

  it('a changed choice must be previewed again before Start', async () => {
    mount()
    await screen.findByTestId('agent-preview', {}, T)
    fireEvent.change(screen.getByLabelText('What it does'), { target: { value: 'fix' } })
    expect(screen.getByTestId('agent-start')).toBeDisabled()
    await waitFor(() => expect(s.preview).toHaveBeenLastCalledWith(WS, expect.objectContaining({ mode: 'fix' })), T)
    await waitFor(() => expect(screen.getByTestId('agent-start')).toBeEnabled(), T)
  })

  it('launch.changed previews again and asks for a new Start', async () => {
    mount()
    await screen.findByTestId('agent-preview', {}, T)
    s.start.mockRejectedValueOnce(new ApiError(409, { code: 'launch.changed', message: 'changed', retryable: false }))
    fireEvent.click(screen.getByTestId('agent-start'))
    expect(await screen.findByTestId('agent-changed', {}, T)).toBeInTheDocument()
    expect(s.preview).toHaveBeenCalledTimes(2)
    expect(s.start).toHaveBeenCalledTimes(1)
  })

  it('run.limit shows the host message as text', async () => {
    mount()
    await screen.findByTestId('agent-preview', {}, T)
    s.start.mockRejectedValueOnce(new ApiError(409, { code: 'run.limit', message: '8 agent runs at most per workspace', retryable: true }))
    fireEvent.click(screen.getByTestId('agent-start'))
    expect(await screen.findByTestId('agent-error', {}, T)).toHaveTextContent('8 agent runs at most per workspace')
  })
})

const noGrant = (reason: 'none' | 'expiring' | 'scope' | 'revoked' = 'none') =>
  preview({ grant: null, blocked: { code: 'launch.no_grant', message: 'no runner grant', detail: { reason } } })

async function issueAndSign() {
  fireEvent.click(await screen.findByTestId('runner-grant-issue', {}, T))
  const panel = await screen.findByTestId('click-panel', {}, T)
  const sign = within(panel).getByRole('button', { name: /^Sign and allow access$/ })
  await waitFor(() => expect(sign).toBeEnabled(), { timeout: ARM_MS + 2000 })
  fireEvent.pointerDown(sign)
  fireEvent.click(sign)
  await waitFor(() => expect(s.sign).toHaveBeenCalled(), { timeout: 3000 })
}

describe('the runner grant', () => {
  it('no grant: Issue is gated on dashboard execution', async () => {
    s.preview.mockResolvedValue(noGrant())
    await mountCard({ enabled: false })
    expect(await screen.findByTestId('runner-grant-off', {}, T)).toHaveTextContent('Turn on dashboard execution first.')
    expect(screen.getByTestId('runner-grant-issue')).toBeDisabled()
  })


  it.each([
    ['expiring', 'less than 15 minutes'],
    ['scope', 'workable tickets'],
    ['revoked', 'revoked or has expired'],
  ] as const)('launch.no_grant %s says what to do', async (reason, text) => {
    s.preview.mockResolvedValue(noGrant(reason))
    await mountCard()
    expect(await screen.findByTestId('runner-grant-needed', {}, T)).toHaveTextContent(text)
  })

  it('issues a deposit grant with hours (a length from the list) and scope; confirmed reads the grant and previews again', async () => {
    s.preview.mockResolvedValue(noGrant())
    await mountCard()
    await screen.findByTestId('runner-grant-issue', {}, T)
    expect(within(card()).getByLabelText('Length').querySelectorAll('option')).toHaveLength(4)
    expect([...within(card()).getByLabelText('Length').querySelectorAll('option')].map(o => o.textContent)).toEqual(['8 hours', '1 day', '3 days', '7 days'])
    fireEvent.change(within(card()).getByLabelText('Length'), { target: { value: '24' } })
    fireEvent.change(within(card()).getByLabelText('Tickets'), { target: { value: 'all' } })
    s.runner.mockResolvedValue({ runner: runner({ grant: { id: GRANT, until: UNTIL, scope: 'all' } }) })
    await issueAndSign()
    expect(s.prepare.mock.calls[0][1]).toEqual({ action: 'runner_grant', hours: 24, scope: 'all' })
    expect(s.sign.mock.calls[0][3]).toBeUndefined() // nothing typed, no word sent
    expect(await screen.findByTestId('runner-grant-confirmed', {}, T)).toBeInTheDocument()
  })

  it('rejected: the reason sentence of the closed code, and Revoke', async () => {
    s.preview.mockResolvedValue(noGrant())
    s.sign.mockResolvedValue({ state: 'submitted', duplicate: false, deposit: 'rejected', deposit_id: REQ.slice(3), grant: GRANT, deposit_reason: 'grant.expiring' })
    await mountCard()
    await issueAndSign()
    const box = await screen.findByTestId('runner-grant-rejected', {}, T)
    expect(box).toHaveTextContent('The runner did not take the grant: The grant has less than 15 minutes left.')
    expect(within(box).getByRole('button', { name: 'Revoke grant' })).toBeInTheDocument()
  })

  it('an unknown reason code reads as other', async () => {
    const { depositReason } = await import('@/api/agents')
    expect(depositReason('something.new')).toBe('The runner refused it.')
    expect(depositReason(undefined)).toBe('The runner refused it.')
  })

  it('unknown: asks grant.deposit_status a bounded number of times, then says what to do', async () => {
    s.preview.mockResolvedValue(noGrant())
    s.sign.mockResolvedValue({ state: 'submitted', duplicate: false, deposit: 'unknown', deposit_id: REQ.slice(3), grant: GRANT })
    await mountCard()
    await issueAndSign()
    expect(await screen.findByTestId('runner-grant-unknown', {}, { timeout: 15000 })).toHaveTextContent('The grant may or may not be with your runner. Revoke it to be sure.')
    expect(s.status).toHaveBeenCalledTimes(5)
    expect(s.status).toHaveBeenCalledWith(WS, REQ.slice(3))
  }, 20000)

  it('unknown that the host then confirms is confirmed', async () => {
    s.preview.mockResolvedValue(noGrant())
    s.sign.mockResolvedValue({ state: 'submitted', duplicate: false, deposit: 'unknown', deposit_id: REQ.slice(3), grant: GRANT })
    s.status.mockResolvedValueOnce({ deposit: 'unknown' }).mockResolvedValueOnce({ deposit: 'confirmed' })
    await mountCard()
    await issueAndSign()
    expect(await screen.findByTestId('runner-grant-confirmed', {}, { timeout: 8000 })).toBeInTheDocument()
  }, 15000)

  it.each([
    ['grant.forgotten', 'A newer grant replaced this one before the runner took it.'],
    ['runner_restarted', 'The runner refused it.'], // a host code outside the closed set (the server already maps it)
  ] as const)('unknown that the host then rejects (%s) names the reason from grant.deposit_status', async (reason, text) => {
    s.preview.mockResolvedValue(noGrant())
    s.sign.mockResolvedValue({ state: 'submitted', duplicate: false, deposit: 'unknown', deposit_id: REQ.slice(3), grant: GRANT })
    s.status.mockResolvedValueOnce({ deposit: 'unknown' }).mockResolvedValueOnce({ deposit: 'rejected', reason: reason as never })
    await mountCard()
    await issueAndSign()
    const box = await screen.findByTestId('runner-grant-rejected', {}, { timeout: 8000 })
    expect(box).toHaveTextContent(`The runner did not take the grant: ${text}`)
    expect(within(box).getByRole('button', { name: 'Revoke grant' })).toBeInTheDocument()
  }, 15000)

  it('a View without the deposit line gets no Sign button', async () => {
    s.preview.mockResolvedValue(noGrant())
    s.view.mockResolvedValue(grantView({ deposit_line: undefined, lines: ['grant', DEPOSIT_RISK] }))
    await mountCard()
    fireEvent.click(await screen.findByTestId('runner-grant-issue', {}, T))
    expect(await screen.findByTestId('runner-grant-bad-view', {}, T)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Sign( |$)/ })).toBeNull()
  })

  it.each([
    [{ code: 'human.click_refused', reason: 'terminal_only' }, 'Turn on dashboard execution first.'],
    [{ code: 'click.unavailable' }, 'Run `orch grant --runner` in your terminal.'],
  ])('refusal %o says %s', async (err, text) => {
    s.preview.mockResolvedValue(noGrant())
    s.prepare.mockRejectedValue(new ApiError(409, { message: 'x', retryable: false, ...err }))
    await mountCard()
    fireEvent.click(await screen.findByTestId('runner-grant-issue', {}, T))
    expect(await screen.findByTestId('runner-grant-refused', {}, T)).toHaveTextContent(text)
  })

  it('Forget runner grant asks first, then forgets with an idem', async () => {
    await mountCard({ grant: { id: GRANT, until: SOON, scope: 'workable' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Forget runner grant' }, T))
    expect(s.forget).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Forget' }))
    await waitFor(() => expect(s.forget).toHaveBeenCalledWith(WS, GRANT, expect.stringMatching(/^a_/)), T)
  })
})

describe('a short, calm dialog (U6)', () => {
  const grantSoon = () => s.runner.mockResolvedValue({ runner: runner({ grant: { id: GRANT, until: SOON, scope: 'workable' } }) })

  it('first time: the full notice, nothing hidden', async () => {
    mount()
    expect(await screen.findByTestId('agent-risk-full', {}, T)).toHaveTextContent(EXECUTION_LINE)
    expect(screen.getByTestId('execution-line')).toHaveTextContent(EXECUTION_LINE)
    expect(screen.queryByTestId('agent-access')).toBeNull() // access is valid: no select
  })

  it('the login-shell notice is shown in the first-time notice', async () => {
    s.preview.mockResolvedValue(preview({ login_shell: true, warnings: [LOGIN_NOTICE] }))
    mount()
    expect(await screen.findByTestId('agent-login-notice', {}, T)).toHaveTextContent('login shell')
    expect(within(screen.getByTestId('agent-risk-full')).getByTestId('agent-login-notice')).toBeInTheDocument()
  })

  it('acknowledged: one small line, the same words under Details, the login-shell notice there too', async () => {
    s.notices.mockResolvedValue({ acknowledged: { start_agent: true, login_shell: true } })
    s.preview.mockResolvedValue(preview({ login_shell: true, warnings: [LOGIN_NOTICE] }))
    mount()
    await screen.findByTestId('agent-preview', {}, T)
    expect(screen.queryByTestId('agent-risk-full')).toBeNull()
    const details = screen.getByTestId('agent-details')
    expect(details).not.toHaveAttribute('open')
    expect(within(details).getByText(/Runs as you on this computer/)).toBeInTheDocument()
    expect(within(details).getByTestId('execution-line')).toHaveTextContent(EXECUTION_LINE)
    expect(within(details).getByTestId('agent-login-notice')).toHaveTextContent('login shell')
  })

  it('MED-2: the login-shell notice is its own notice: full the first time even when the main notice was read', async () => {
    s.notices.mockResolvedValue({ acknowledged: { start_agent: true } })
    s.acknowledge.mockResolvedValue({ acknowledged: { start_agent: true, login_shell: true } })
    s.preview.mockResolvedValue(preview({ login_shell: true, warnings: [LOGIN_NOTICE] }))
    mount()
    const full = await screen.findByTestId('agent-risk-full', {}, T)
    expect(within(full).getByTestId('agent-login-notice')).toHaveTextContent('login shell')
    expect(within(full).queryByTestId('execution-line')).toBeNull()
    expect(within(screen.getByTestId('agent-details')).getByTestId('execution-line')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('agent-risk-ack'))
    await waitFor(() => expect(s.acknowledge).toHaveBeenCalledWith(WS, 'login_shell'))
    expect(s.acknowledge).not.toHaveBeenCalledWith(WS, 'start_agent')
    await waitFor(() => expect(screen.queryByTestId('agent-risk-full')).toBeNull())
    expect(within(screen.getByTestId('agent-details')).getByTestId('agent-login-notice')).toBeInTheDocument()
  })

  it('MED-2: only the host\'s exact sentence is the login-shell notice; other warnings stay visible', async () => {
    s.notices.mockResolvedValue({ acknowledged: { start_agent: true } })
    s.preview.mockResolvedValue(preview({ warnings: ['something else mentions a login shell'] }))
    mount()
    await screen.findByTestId('agent-preview', {}, T)
    expect(screen.queryByTestId('agent-login-notice')).toBeNull()
    expect(screen.queryByTestId('agent-risk-full')).toBeNull()
    expect(screen.getByText('something else mentions a login shell')).toBeInTheDocument()
  })

  it('L1: while the acknowledgement is being read, Start stays off', async () => {
    let done: (v: { acknowledged: { start_agent: boolean } }) => void = () => {}
    s.notices.mockReturnValue(new Promise((r) => { done = r }))
    mount()
    await screen.findByTestId('agent-preview', {}, T)
    expect(screen.getByTestId('agent-start')).toBeDisabled()
    done({ acknowledged: { start_agent: true } })
    await waitFor(() => expect(screen.getByTestId('agent-start')).toBeEnabled(), T)
  })

  it('"I understand" saves the acknowledgement and folds the notice', async () => {
    mount()
    fireEvent.click(await screen.findByTestId('agent-risk-ack', {}, T))
    await waitFor(() => expect(s.acknowledge).toHaveBeenCalledWith(WS, 'start_agent'))
    await waitFor(() => expect(screen.queryByTestId('agent-risk-full')).toBeNull())
    expect(screen.getByTestId('execution-line')).toHaveTextContent(EXECUTION_LINE) // still there, under Details
  })

  it('#1128: opened after Create and start agent, Start is the one button; the notice stays and starting acknowledges it', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><TooltipProvider><StartAgentDialog ws={WS} ticket="DEMO-1" open onClose={vi.fn()} initial={{ harness: 'claude-code', mode: 'work' }} /></TooltipProvider></QueryClientProvider>)
    const full = await screen.findByTestId('agent-risk-full', {}, T)
    expect(within(full).getByTestId('execution-line')).toHaveTextContent(EXECUTION_LINE)
    expect(screen.queryByTestId('agent-risk-ack')).toBeNull()
    await waitFor(() => expect(screen.getByTestId('agent-start')).toBeEnabled(), T)
    fireEvent.click(screen.getByTestId('agent-start'))
    await waitFor(() => expect(s.acknowledge).toHaveBeenCalledWith(WS, 'start_agent'), T)
  })

  it('starting with the full notice on screen counts as having read it', async () => {
    mount()
    await screen.findByTestId('agent-risk-full', {}, T)
    await waitFor(() => expect(screen.getByTestId('agent-start')).toBeEnabled(), T)
    fireEvent.click(screen.getByTestId('agent-start'))
    await waitFor(() => expect(s.acknowledge).toHaveBeenCalledWith(WS, 'start_agent'), T)
  })

  it('an acknowledgement that cannot be read shows the full notice (fails open to more disclosure)', async () => {
    s.notices.mockRejectedValue(new ApiError(404, { code: 'not_found', message: 'x', retryable: false }))
    mount()
    expect(await screen.findByTestId('agent-risk-full', {}, T)).toBeInTheDocument()
  })

  it('no access: the access length is a select in the dialog and the button signs and starts', async () => {
    s.runner.mockResolvedValue({ runner: runner() })
    s.preview.mockResolvedValue(noGrant())
    mount()
    expect(await screen.findByTestId('agent-access', {}, T)).toHaveTextContent('Access ends')
    expect(screen.getByTestId('agent-access')).toHaveTextContent('Every agent the runner starts can use it')
    expect([...(screen.getByLabelText('Give agents access for') as HTMLSelectElement).options].map((o) => o.textContent)).toEqual(['8 hours', '1 day', '3 days', '7 days'])
    expect(await screen.findByTestId('agent-start', {}, T)).toHaveTextContent('Sign and start agent')
  })

  it('no access: signing the access (one click) starts the agent and says it once', async () => {
    s.runner.mockResolvedValue({ runner: runner() })
    s.preview.mockResolvedValue(noGrant())
    const { onStarted } = mount()
    await screen.findByTestId('agent-access', {}, T)
    await waitFor(() => expect(screen.getByTestId('agent-start')).toBeEnabled(), T)
    fireEvent.click(screen.getByTestId('agent-start'))
    await prepareAccess()
    expect(s.prepare.mock.calls[0][1]).toEqual({ action: 'runner_grant', hours: 8, scope: 'workable' })
    s.runner.mockResolvedValue({ runner: runner({ grant: { id: GRANT, until: SOON, scope: 'workable' } }) })
    s.preview.mockResolvedValue(preview())
    const sign = screen.getByRole('button', { name: /^Sign and start agent$/ })
    await waitFor(() => expect(sign).toBeEnabled(), { timeout: ARM_MS + 2000 })
    fireEvent.pointerDown(sign)
    fireEvent.click(sign)
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith(run), { timeout: 8000 })
    expect(s.start).toHaveBeenCalledTimes(1)
    // The grant that landed made the access valid while the review was still open: the dialog did not turn silent and
    // prepare the same access a second time (the host answers that with "the earlier attempt ended").
    expect(s.prepare).toHaveBeenCalledTimes(1)
  }, 15000)

  it.each([
    ['the command changed', preview({ display: 'claude --other' })],
    ['the model changed', preview({ model: 'haiku', model_source: 'addon', model_by: 'model-routing' })],
    ['a new warning appeared', preview({ warnings: ['A new thing to read.'] })],
    ['a new environment name appeared', preview({ env: ['NEW_SECRET_NAME'] })],
    ['an unread login-shell notice appeared', preview({ warnings: [LOGIN_NOTICE] })],
  ])('no access: signing the access does not start the agent when %s after the grant; it asks for a new Start', async (_n, after) => {
    s.runner.mockResolvedValue({ runner: runner() })
    s.preview.mockResolvedValue(noGrant())
    const { onStarted } = mount()
    await screen.findByTestId('agent-access', {}, T)
    await waitFor(() => expect(screen.getByTestId('agent-start')).toBeEnabled(), T)
    fireEvent.click(screen.getByTestId('agent-start'))
    await prepareAccess()
    s.runner.mockResolvedValue({ runner: runner({ grant: { id: GRANT, until: SOON, scope: 'workable' } }) })
    s.preview.mockResolvedValue(after)
    const sign = screen.getByRole('button', { name: /^Sign and start agent$/ })
    await waitFor(() => expect(sign).toBeEnabled(), { timeout: ARM_MS + 2000 })
    fireEvent.pointerDown(sign)
    fireEvent.click(sign)
    expect(await screen.findByTestId('agent-changed', {}, { timeout: 8000 })).toHaveTextContent('Something changed since you looked. Check and press Start again.')
    expect(s.start).not.toHaveBeenCalled()
    expect(onStarted).not.toHaveBeenCalled()
    expect(screen.queryByTestId('agent-error')).toBeNull()
    expect(screen.getByTestId('agent-start')).toHaveTextContent('Start agent')
  }, 15000)

  it('valid access and nothing to read: no dialog at all, the agent just starts', async () => {
    s.notices.mockResolvedValue({ acknowledged: { start_agent: true } })
    const { onStarted, onClose } = mount(vi.fn(), vi.fn(), {})
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith(run), T)
    expect(onClose).toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByTestId('agent-start')).toBeNull()
  })

  it('valid access but the first-start notice is unread: the dialog opens and nothing starts by itself', async () => {
    const { onStarted } = mount(vi.fn(), vi.fn(), {})
    expect(await screen.findByTestId('agent-risk-full', {}, T)).toBeInTheDocument()
    expect(onStarted).not.toHaveBeenCalled()
    expect(s.start).not.toHaveBeenCalled()
  })

  it('less than 20 minutes of access left counts as no access (the select shows)', async () => {
    s.runner.mockResolvedValue({ runner: runner({ grant: { id: GRANT, until: new Date(nowMs() + 10 * 60_000).toISOString(), scope: 'workable' } }) })
    mount(vi.fn(), vi.fn(), {})
    expect(await screen.findByTestId('agent-access', {}, T)).toBeInTheDocument()
    expect(s.start).not.toHaveBeenCalled()
  })

  it('the runner is off: one plain sentence and a Turn on button, no terminal command, no signing', async () => {
    s.runner.mockResolvedValue({ runner: runner({ enabled: false }) })
    s.preview.mockResolvedValue(preview({ spec: null, grant: null, blocked: { code: 'runner.disabled', message: null, detail: {} } }))
    mount()
    expect(await screen.findByTestId('agent-runner-off', {}, T)).toHaveTextContent('Dashboard execution is off, so agents cannot start yet.')
    expect(screen.getByTestId('runner-enable')).toHaveTextContent('Turn on dashboard execution')
    expect(screen.getByTestId('agent-runner-off')).not.toHaveTextContent('orch runner')
    expect(screen.queryByTestId('agent-access')).toBeNull()
  })

  it('blocked: one status line with the reason, Start off, no grant line', async () => {
    grantSoon()
    s.preview.mockResolvedValue(preview({ spec: null, blocked: { code: 'launch.claimed', message: null, detail: {} } }))
    mount()
    expect(await screen.findByTestId('agent-blocked', {}, T)).toHaveTextContent('An agent session already claims this ticket.')
    expect(screen.getByTestId('agent-start')).toBeDisabled()
  })

  it('the main view has no check button, no Details-free paragraph about dashboard execution', async () => {
    s.notices.mockResolvedValue({ acknowledged: { start_agent: true } })
    mount()
    await screen.findByTestId('agent-preview', {}, T)
    expect(screen.queryByRole('button', { name: /Check start settings/ })).toBeNull()
    expect(screen.queryByTestId('agent-risk-full')).toBeNull()
    expect(screen.queryByTestId('runner-grant-card')).toBeNull()
  })
})

describe('review fixes', () => {
  it.each([
    [{ display: null, display_hidden: true }],
    [{ cwd_path: null, cwd_path_hidden: true }],
  ])('L2: Start stays off when the command or the folder is withheld (%o)', async (over) => {
    s.preview.mockResolvedValue(preview(over))
    mount()
    expect(await screen.findByTestId('agent-unreadable', {}, T)).toBeInTheDocument()
    expect(screen.getByTestId('agent-start')).toBeDisabled()
  })

  it.each([
    [{ workspace: { id: 'f'.repeat(32), name: 'other', prefix: 'DEMO' } }],
  ])('L3: a grant View of another workspace gets no Sign button (%o)', async (over) => {
    s.preview.mockResolvedValue(noGrant())
    s.view.mockResolvedValue(grantView(over))
    await mountCard()
    fireEvent.click(await screen.findByTestId('runner-grant-issue', {}, T))
    expect(await screen.findByTestId('runner-grant-bad-view', {}, T)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Sign( |$)/ })).toBeNull()
  })

  it('L1: a lost sign answer settled at the host still asks where the deposit went, by the request id', async () => {
    s.preview.mockResolvedValue(noGrant())
    s.sign.mockRejectedValue(new TypeError('network'))
    vi.spyOn(api, 'stateClick').mockResolvedValue({ id: REQ, state: 'submitted' })
    s.status.mockResolvedValue({ deposit: 'confirmed' })
    await mountCard()
    await issueAndSign()
    expect(await screen.findByTestId('runner-grant-confirmed', {}, { timeout: 8000 })).toBeInTheDocument()
    expect(s.status).toHaveBeenCalledWith(WS, REQ.slice(3))
  }, 15000)

  it('L1: still unknown without a grant id says where to find it', async () => {
    s.preview.mockResolvedValue(noGrant())
    s.sign.mockRejectedValue(new TypeError('network'))
    vi.spyOn(api, 'stateClick').mockResolvedValue({ id: REQ, state: 'submitted' })
    await mountCard()
    await issueAndSign()
    expect(await screen.findByTestId('runner-grant-find', {}, { timeout: 20000 })).toBeInTheDocument()
  }, 25000)

  it('I4: click switched off for the workspace says to use the terminal', async () => {
    s.preview.mockResolvedValue(noGrant())
    s.prepare.mockRejectedValue(new ApiError(409, { code: 'human.click_refused', reason: 'disabled', message: 'x', retryable: false }))
    await mountCard()
    fireEvent.click(await screen.findByTestId('runner-grant-issue', {}, T))
    expect(await screen.findByTestId('runner-grant-refused', {}, T)).toHaveTextContent('Run `orch grant --runner` in your terminal.')
  })
})

describe('the live stream', () => {
  it('a resync (run.sub ended or reconnected) reads whoami.runner again', async () => {
    const { invalidateLive } = await import('@/api/useLiveUpdates')
    const qc = new QueryClient()
    qc.setQueryData(['runner', WS], { runner: runner({ grant: { id: GRANT, until: UNTIL, scope: 'workable' } }) })
    invalidateLive(qc, WS, 'resync', [])
    expect(qc.getQueryState(['runner', WS])?.isInvalidated).toBe(true)
  })

  it('a runner item patches the runner card and never becomes a run', () => {
    const qc = new QueryClient()
    qc.setQueryData(['runs', WS], { runs: [], runner: null, truncated: false } satisfies RunList)
    qc.setQueryData(['runner', WS], { runner: runner() })
    mergeRunItems(qc, WS, [{ t: 'runner', grant: { id: GRANT, until: UNTIL, scope: 'workable' } }])
    expect((qc.getQueryData(['runs', WS]) as RunList).runs).toEqual([])
    expect((qc.getQueryData(['runner', WS]) as { runner: Runner }).runner.grant?.id).toBe(GRANT)
    mergeRunItems(qc, WS, [{ t: 'runner', grant: null }])
    expect((qc.getQueryData(['runner', WS]) as { runner: Runner }).runner.grant).toBeNull()
  })
})


it('Extend preserves the runner scope and posts its id with the selected length', async () => {
  render(<QueryClientProvider client={new QueryClient()}><TooltipProvider><RunnerGrantCard ws={WS} runner={runner({ grant: { id: GRANT, until: UNTIL, scope: 'all' } })} loading={false} blocked={null} extend={{ id: GRANT, scope: 'all', until: UNTIL }} onChanged={() => {}} /></TooltipProvider></QueryClientProvider>)
  expect(screen.getByText('Agents that are running now stop; new agents use the extended access.')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('option', { name: '7 days' })).toBeInTheDocument())
  fireEvent.change(screen.getByLabelText('Length'), { target: { value: '168' } })
  expect(screen.getByLabelText('Tickets')).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Extend access' }))
  await waitFor(() => expect(s.prepare).toHaveBeenCalled())
  expect(s.prepare.mock.calls[0][1]).toEqual({ action: 'runner_grant', hours: 168, scope: 'all', extend: GRANT })
})

describe('sheet risk notice', () => {
  const mountSheet = () => render(<QueryClientProvider client={new QueryClient()}><TooltipProvider><RunnerGrantCard sheet ws={WS} runner={runner({ grant: null })} loading={false} blocked={null} onChanged={() => {}} /></TooltipProvider></QueryClientProvider>)
  it('first time: the full notice and I understand; saving folds it to a quiet line', async () => {
    s.notices.mockResolvedValue({ acknowledged: {} })
    const ack = s.acknowledge.mockResolvedValue({ acknowledged: { grant: true } })
    mountSheet()
    fireEvent.click(await screen.findByTestId('grant-risk-ack'))
    expect(ack).toHaveBeenCalledWith(WS, 'grant')
    const d = await screen.findByTestId('grant-risk-details')
    expect(d).toHaveTextContent('Runs as you on this computer')
    expect(d).not.toHaveAttribute('open')
    expect(d).toHaveTextContent('standing authority')
  })
  it('acknowledged: quiet line only; unreadable answer: full notice (fail closed)', async () => {
    s.notices.mockResolvedValue({ acknowledged: { grant: true } })
    mountSheet()
    expect(await screen.findByTestId('grant-risk-details')).toBeInTheDocument()
    expect(screen.queryByTestId('grant-risk-ack')).toBeNull()
    cleanup()
    s.notices.mockRejectedValue(new Error('x'))
    mountSheet()
    expect(await screen.findByTestId('grant-risk-ack')).toBeInTheDocument()
  })
})

it.each([4, 24, 500])('caps runner lengths at workspace hours %s and 168', async (limit) => {
  vi.mocked(api.getWorkspaces).mockResolvedValue([{ id: WS, grant_hours: limit }] as Awaited<ReturnType<typeof api.getWorkspaces>>)
  s.preview.mockResolvedValue(noGrant())
  await mountCard()
  await waitFor(() => expect(screen.getByTestId('runner-grant-issue')).toBeEnabled())
  const select = screen.getByLabelText('Length') as HTMLSelectElement
  expect([...select.options].every(o => Number(o.value) <= Math.min(limit, 168))).toBe(true)
  expect(Number(select.value)).toBeLessThanOrEqual(Math.min(limit, 168))
})



describe('terminal-approval workspace (no one-click)', () => {
  beforeEach(() => {
    vi.mocked(api.getWorkspaces).mockResolvedValue([{ id: WS, grant_hours: 72, click: { custody: 'passphrase', enabled: false, available: false } }] as Awaited<ReturnType<typeof api.getWorkspaces>>)
    vi.mocked(api.getHostSettings).mockResolvedValue({ } as Awaited<ReturnType<typeof api.getHostSettings>>)
  })
  it('without One click the runner grant is not prepared at all (no terminal hand-off): the review says to turn One click on; 8 h up to the workspace maximum', async () => {
    vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
    s.preview.mockResolvedValue(noGrant())
    await mountCard()
    await waitFor(() => expect(screen.getByTestId('runner-grant-issue')).toBeEnabled())
    expect([...(screen.getByLabelText('Length') as HTMLSelectElement).options].map(o => o.textContent)).toEqual(['8 hours', '1 day', '3 days'])
    fireEvent.click(screen.getByTestId('runner-grant-issue'))
    expect(await screen.findByText('One click is not ready on this computer.', {}, T)).toBeInTheDocument()
    await new Promise((r) => setTimeout(r, 50))
    expect(s.prepare).not.toHaveBeenCalled()
    expect(screen.queryByTestId('handoff-command')).toBeNull()
    expect(screen.queryByText(/orch sign/)).not.toBeInTheDocument()
    expect(screen.queryByText(/convenience/i)).not.toBeInTheDocument()
  })
  it('Extend hides lengths shorter than the access it replaces', async () => {
    const until = new Date(nowMs() + 40 * 3_600_000).toISOString()
    render(<QueryClientProvider client={new QueryClient()}><TooltipProvider><RunnerGrantCard ws={WS} runner={runner({ grant: { id: GRANT, until, scope: 'workable' } })} loading={false} blocked={null} extend={{ id: GRANT, scope: 'workable', until }} onChanged={() => {}} /></TooltipProvider></QueryClientProvider>)
    await waitFor(() => expect(screen.getByTestId('runner-grant-issue')).toBeEnabled())
    expect([...(screen.getByLabelText('Length') as HTMLSelectElement).options].map(o => o.textContent)).toEqual(['3 days'])
  })
})

it('Start agent folds the technical lines into Details and explains each mode', async () => {
  mount()
  const details = await screen.findByTestId('agent-details', {}, T)
  expect(details.tagName).toBe('DETAILS')
  expect(details).not.toHaveAttribute('open')
  expect(within(details).getByText('Runs')).toBeInTheDocument()
  expect(screen.getByTestId('agent-sentence')).toHaveTextContent(/work through this ticket/)
  fireEvent.change(screen.getByLabelText('What it does'), { target: { value: 'refine' } })
  expect(screen.getByTestId('agent-sentence')).toHaveTextContent(/changes no code/)
})

describe('prefilled by the New ticket form (initial)', () => {
  it('takes the choice, says why the person is asked, and previews the ticket as asked', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}><TooltipProvider>
        <StartAgentDialog ws={WS} ticket="DEMO-1" open onClose={vi.fn()} initial={{ harness: 'codex', mode: 'refine', model: 'o3', note: 'Created DEMO-1. The agent was not started: the model differs.' }} />
      </TooltipProvider></QueryClientProvider>,
    )
    expect(await screen.findByTestId('agent-initial-note', {}, T)).toHaveTextContent('The agent was not started: the model differs.')
    await waitFor(() => expect(s.preview).toHaveBeenCalledWith(WS, { ticket: 'DEMO-1', harness: 'codex', mode: 'refine', model: 'o3', worktree: true }), T)
    expect(s.start).not.toHaveBeenCalled() // one explicit Start
  })
  it('M-1: one state line. Once the preview is ready the "not started" note gives way to it', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}><TooltipProvider>
        <StartAgentDialog ws={WS} ticket="DEMO-1" open onClose={vi.fn()} initial={{ note: 'Created DEMO-1. The agent was not started: the notice has not been read yet.' }} />
      </TooltipProvider></QueryClientProvider>,
    )
    expect(await screen.findByTestId('agent-ready', {}, T)).toHaveTextContent('DEMO-1 is created. Check the details, then press Start agent.')
    expect(screen.queryByTestId('agent-initial-note')).toBeNull()
    expect(screen.queryByText(/Ready to start/)).toBeNull()
  })
  it('reuses the idem of a start whose answer was lost', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}><TooltipProvider>
        <StartAgentDialog ws={WS} ticket="DEMO-1" open onClose={vi.fn()} initial={{ idem: 'idem-from-review' }} />
      </TooltipProvider></QueryClientProvider>,
    )
    const start = await screen.findByTestId('agent-start', {}, T)
    await waitFor(() => expect(start).toBeEnabled(), T)
    fireEvent.click(start)
    await waitFor(() => expect(s.start).toHaveBeenCalled(), T)
    expect(s.start.mock.calls[0][3]).toBe('idem-from-review')
  })
})
