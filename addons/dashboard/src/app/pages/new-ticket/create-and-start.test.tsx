// "Create and start agent" (U4 PR2): the default path, Only create, agent access only without a usable grant, the step
// limit, the start compared with the review, the risk notices, partial failures, one idem, and a reload that never starts.
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { agentsApi, LOGIN_NOTICE, type AgentPreview } from '@/api/agents'
import { api, mockStore } from '@/api/client'
import { routingApi, type RoutingChoices } from '@/api/modelRouting'
import { ApiError } from '@/api/types'
import { fakeRequest, state } from '@/test/handoffHarness'
import { renderApp } from '@/test/renderApp'
import { nowMs } from '@/lib/time'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), handoffMode: () => true }))
// The signing dialog is its own feature; here it shows the action, the "not signed" block, and reports how it ended.
vi.mock('@/components/sign/HandoffDialog', () => ({
  HandoffDialog: (p: { title: string; startsAgent?: boolean; action: { action: string }; unsigned?: React.ReactNode; onSettled?: (phase: string, st: unknown, req: unknown) => void; onClose: () => void }) => {
    const req = { ...fakeRequest({ key: 'DEMO-0044', id: 'hr_' + '0'.repeat(25) + '7' }), op: 'new' }
    return (
      <div role="dialog" aria-label={p.title}>
        <pre data-testid="sent-action">{JSON.stringify(p.action)}</pre>
        <span data-testid="starts-agent">{String(!!p.startsAgent)}</span>
        {p.unsigned}
        <button onClick={() => p.onSettled?.('done', state(req, { state: 'submitted', key: 'DEMO-0044' }), req)}>signed</button>
        <button onClick={() => p.onSettled?.('done', state({ ...req, id: 'hr_' + '0'.repeat(25) + '8' }, { state: 'submitted', key: 'DEMO-0099' }), req)}>signed-other-request</button>
        <button onClick={() => p.onSettled?.('failed', state(req, { state: 'submitted', key: 'DEMO-0044', progress: { done: 4, of: 5, last: 'grant' } }), req)}>access-missing</button>
        <button onClick={() => p.onSettled?.('failed', state(req, { state: 'submitted', key: 'DEMO-0044', progress: { done: 2, of: 5 } }), req)}>stopped-early</button>
        <button onClick={p.onClose}>cancelled</button>
      </div>
    )
  },
}))
vi.mock('@/app/terminal/real/StartAgentDialog', () => ({
  StartAgentDialog: (p: { ticket?: string; initial?: unknown; onClose: () => void }) => (
    <div role="dialog" aria-label="Start agent dialog">
      <span data-testid="dialog-ticket">{p.ticket}</span>
      <pre data-testid="dialog-initial">{JSON.stringify(p.initial)}</pre>
      <button onClick={p.onClose}>close-start-dialog</button>
    </div>
  ),
}))

const T = { timeout: 5000 }
const WS = () => mockStore.workspaces[0].id
const inHours = (h: number) => new Date(nowMs() + h * 3_600_000).toISOString().replace(/\.\d+Z$/, 'Z')
const CHOICES: RoutingChoices = {
  harnesses: [
    { id: 'claude-code', label: 'Claude', models: [{ id: 'opus', label: 'Opus' }, { id: 'sonnet', label: 'Sonnet' }] },
    { id: 'codex', label: 'Codex', models: [{ id: 'gpt-5.4', label: 'GPT-5.4' }] },
  ],
  modes: [{ id: 'work', label: 'Do the work' }],
}
let GRANT = { id: 'gr_01M4Y46B485AXD4CBEJZ15Z1TV', until: '', scope: 'workable' as const }
const preview = (over: Partial<AgentPreview> = {}): AgentPreview => ({
  spec: 'f'.repeat(64), ticket: 'DEMO-0044', display: 'claude "work on DEMO-0044"', display_hidden: false, model: 'opus', model_source: 'workspace',
  cwd: { kind: 'workspace' }, cwd_path: '/w/billing', cwd_path_hidden: false, will_create_worktree: false, login_shell: false,
  env: ['TERM'], grant: GRANT, blocked: null, warnings: [], ...over,
})
const run = { run: 'r_01M4Y46B485AXD4CBEJZ15Z1TV' } as never

let s: { runner: ReturnType<typeof vi.spyOn>; preview: ReturnType<typeof vi.spyOn>; start: ReturnType<typeof vi.spyOn>; notices: ReturnType<typeof vi.spyOn> }
function setup(opts: { grant?: typeof GRANT | null; runner?: { connected: boolean; enabled: boolean } | null; acked?: { start_agent?: boolean; login_shell?: boolean } } = {}) {
  const grant = opts.grant === undefined ? GRANT : opts.grant
  const runner = opts.runner === undefined ? { connected: true, enabled: true } : opts.runner
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ available: true, grant_hours: 168, models: { 'claude-code': { work: 'opus', refine: 'opus' } } } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(routingApi, 'choices').mockResolvedValue(CHOICES)
  s = {
    runner: vi.spyOn(agentsApi, 'runner').mockResolvedValue({ runner: runner ? { ...runner, terminals: false, grant } : null }),
    preview: vi.spyOn(agentsApi, 'preview').mockResolvedValue(preview()),
    start: vi.spyOn(agentsApi, 'start').mockResolvedValue({ run }),
    notices: vi.spyOn(agentsApi, 'riskNotices').mockResolvedValue({ acknowledged: opts.acked ?? { start_agent: true } }),
  }
}
afterEach(() => vi.restoreAllMocks())

async function open(storage: Record<string, string> = {}) {
  const r = renderApp('/board', { storage })
  await waitFor(() => expect(screen.getByTestId('topbar-title')).not.toHaveTextContent(/^$/), T)
  await r.user.click(await screen.findByRole('button', { name: /New ticket/ }, T))
  const sheet = await screen.findByRole('dialog', { name: 'New ticket' }, T)
  await r.user.type(await within(sheet).findByLabelText('Title', {}, T), 'Export CSV')
  await r.user.type(within(sheet).getByLabelText(/^Requirements/), 'Empty months export a header row')
  return { r, sheet }
}
const sent = async () => JSON.parse((await screen.findByTestId('sent-action')).textContent ?? '{}')
const startBtn = (sheet: HTMLElement) => within(sheet).findByRole('button', { name: 'Create and start agent' }, T)

beforeEach(() => { GRANT = { ...GRANT, until: inHours(48) }; setup() })

describe('the form', () => {
  it('offers Create and start agent first and Only create second, with one agent line', async () => {
    const { sheet } = await open()
    await startBtn(sheet)
    expect(within(sheet).getByRole('button', { name: 'Only create' })).toBeInTheDocument()
    expect(within(sheet).queryByRole('button', { name: 'Create and open another' })).toBeNull() // only on Only create
    const line = await within(sheet).findByTestId('agent-line', {}, T)
    expect(line).toHaveTextContent('Agent: Claude Code · Write requirements and plan · Opus (set in Settings) · Change')
  })

  it('shows only Create, and says why, when the runner is not connected', async () => {
    setup({ runner: { connected: false, enabled: true } })
    const { sheet } = await open()
    expect(await within(sheet).findByTestId('agent-line-off', {}, T)).toHaveTextContent('No agent runner is connected.')
    expect(within(sheet).getByRole('button', { name: 'Review ticket' })).toBeInTheDocument()
    expect(within(sheet).queryByRole('button', { name: 'Create and start agent' })).toBeNull()
  })

  it('Change opens the shared picker; the choice is remembered for this workspace', async () => {
    const { r, sheet } = await open()
    await r.user.click(await within(sheet).findByRole('button', { name: 'Change' }, T))
    const dlg = await screen.findByTestId('agent-change')
    await r.user.selectOptions(within(dlg).getByLabelText('Agent'), 'codex')
    await r.user.selectOptions(within(dlg).getByLabelText('What it does'), 'refine')
    await r.user.click(within(dlg).getByRole('button', { name: 'Done' }))
    expect(within(sheet).getByTestId('agent-line')).toHaveTextContent('Agent: Codex · Write requirements and plan')
    const stored = JSON.parse(localStorage.getItem(`orch.dashboard.new-ticket.agent.p_sev.${WS()}`) ?? '{}')
    expect(stored).toMatchObject({ harness: 'codex', mode: 'refine', start: true })
  })

  it('a new ticket defaults to writing the requirements and plan; a kind of work the person picked is remembered, an old default is not', async () => {
    const key = `orch.dashboard.new-ticket.agent.p_sev.${WS()}`
    const { readPref } = await import('./useStartPlan')
    expect(readPref(key).mode).toBe('refine')
    localStorage.setItem(key, JSON.stringify({ start: true, harness: 'claude-code', mode: 'work', model: '' })) // saved by an older page: indistinguishable from the old default
    expect(readPref(key).mode).toBe('refine')
    localStorage.setItem(key, JSON.stringify({ start: true, harness: 'claude-code', mode: 'work', modeChosen: true, model: '' }))
    expect(readPref(key).mode).toBe('work')
    localStorage.setItem(key, JSON.stringify({ start: true, harness: 'claude-code', mode: 'fix', model: '' }))
    expect(readPref(key).mode).toBe('fix')
    localStorage.removeItem(key)
  })

  it('remembers Only create as the default button for this workspace', async () => {
    const { r, sheet } = await open()
    await r.user.click(await within(sheet).findByRole('button', { name: 'Only create' }, T))
    expect((await sent()).agent).toBeUndefined()
    expect(JSON.parse(localStorage.getItem(`orch.dashboard.new-ticket.agent.p_sev.${WS()}`) ?? '{}').start).toBe(false)
    cleanup()
    const again = await open({ [`orch.dashboard.new-ticket.agent.p_sev.${WS()}`]: JSON.stringify({ start: false, harness: 'claude-code', mode: 'work', model: '' }) })
    expect(await within(again.sheet).findByRole('button', { name: 'Only create' }, T)).toBeInTheDocument()
    expect(within(again.sheet).getByRole('button', { name: 'Create and start agent' })).toBeInTheDocument()
    expect(within(again.sheet).getByRole('button', { name: 'Create and open another' })).toBeInTheDocument()
  })
})

describe('what is signed', () => {
  it('with a usable grant the intent has no agent access, and the start is said to be not signed', async () => {
    const { r, sheet } = await open()
    await r.user.click(await startBtn(sheet))
    const action = await sent()
    expect(action.agent).toBeUndefined()
    expect(await screen.findByTestId('not-signed-start')).toHaveTextContent('Not signed: the agent start')
    expect(screen.getByTestId('not-signed-start').textContent!.length).toBeLessThan(160) // one line, not a paragraph
    expect(screen.getByTestId('starts-agent')).toHaveTextContent('true') // the dialog's steps then say the agent starts with the access already there
  })

  it('without a grant (or one with under 15 minutes left) the access is signed last in the same request, 1/3/7 days', async () => {
    setup({ grant: { ...GRANT, until: inHours(0.2) } })
    const { r, sheet } = await open()
    const hours = await within(sheet).findByLabelText(/Agent access/, {}, T)
    expect([...hours.querySelectorAll('option')].map((o) => o.textContent)).toEqual(['1 day', '3 days', '7 days'])
    await r.user.selectOptions(hours, '72')
    await r.user.click(await startBtn(sheet))
    expect((await sent()).agent).toEqual({ hours: 72 })
  })

  it('Only create never asks for agent access', async () => {
    setup({ grant: null })
    const { r, sheet } = await open()
    await r.user.click(await within(sheet).findByRole('button', { name: 'Only create' }, T))
    expect((await sent()).agent).toBeUndefined()
    await r.user.click(screen.getByRole('button', { name: 'signed' }))
    await waitFor(() => expect(r.router.state.location.pathname).toBe('/ticket/DEMO-0044'), T)
    expect(s.preview).not.toHaveBeenCalled()
    expect(s.start).not.toHaveBeenCalled()
  })

  it('over the step limit with the access: created without starting, said in the form', async () => {
    setup({ grant: null })
    const draft = { type: 'feature', title: 'Many parts', priority: 'high', size: null, labels: [], parent: null, due: '', people: { owner: null, assignees: [], reviewers: [] }, visibility: 'workspace',
      sections: { summary: 'a', context: 'b', requirements: 'c', out_of_scope: 'd', plan: 'e', decisions: 'f' }, acceptance: ['1', '2', '3'].concat(['4', '5', '6', '7']) } // 1 + 1 + 6 + 7 = 15 (+1 access = 16)
    const key = `orch.dashboard.new-ticket.draft.p_sev.${WS()}`
    const fits = await open({ [key]: JSON.stringify(draft) })
    await within(fits.sheet).findByRole('button', { name: 'Create and start agent' }, T)
    expect(within(fits.sheet).queryByTestId('agent-too-many')).toBeNull()
    cleanup()
    const over = await open({ [key]: JSON.stringify({ ...draft, acceptance: [...draft.acceptance, '8'] }) }) // 16 + 1 access = 17
    expect(await within(over.sheet).findByTestId('agent-too-many', {}, T)).toHaveTextContent('too many parts')
    expect(within(over.sheet).queryByRole('button', { name: 'Create and start agent' })).toBeNull()
    await over.r.user.click(within(over.sheet).getByRole('button', { name: 'Only create' }))
    expect((await sent()).agent).toBeUndefined()
  })
})
describe('after the ticket exists', () => {
  async function signed(opts: { button?: string } = {}) {
    const { r, sheet } = await open()
    await r.user.click(await startBtn(sheet))
    await r.user.click(await screen.findByRole('button', { name: opts.button ?? 'signed' }))
    return { r, sheet }
  }

  it('previews for the ticket\'s own key, then starts once with the preview\'s spec and the review\'s idem', async () => {
    const { r } = await signed()
    await waitFor(() => expect(s.start).toHaveBeenCalledTimes(1), T)
    expect(s.preview).toHaveBeenCalledWith(WS(), { ticket: 'DEMO-0044', harness: 'claude-code', mode: 'refine', worktree: true })
    const [ws, args, spec, idem] = s.start.mock.calls[0]
    expect([ws, args, spec]).toEqual([WS(), { ticket: 'DEMO-0044', harness: 'claude-code', mode: 'refine', worktree: true }, 'f'.repeat(64)])
    expect(String(idem)).toMatch(/^[0-9A-Za-z_-]{8,}$/)
    await waitFor(() => expect(r.router.state.location.pathname).toBe('/ticket/DEMO-0044'), T)
    expect(screen.queryByRole('dialog', { name: 'Start agent dialog' })).toBeNull()
  })

  it('never starts for a key that is not the one of its own request (the state is another request\'s)', async () => {
    await signed({ button: 'signed-other-request' })
    await new Promise((res) => setTimeout(res, 150))
    expect(s.preview).not.toHaveBeenCalled()
    expect(s.start).not.toHaveBeenCalled()
  })

  it.each([
    ['a different model', { model: 'sonnet', model_source: 'chosen' as const }],
    ['a different model source', { model_source: 'default' as const }],
    ['a new worktree', { will_create_worktree: true }],
    ['a different grant', { grant: { ...GRANT, id: 'gr_01M4Y46B485AXD4CBEJZ15Z1TW' } }],
    ['no grant', { grant: null }],
    ['a block', { blocked: { code: 'launch.claimed', message: 'DEMO-0044 is claimed', detail: {} } }],
    ['another warning', { warnings: ['ORCH_WORKTREES leaves out x'] }],
    ['a hidden command', { display: null }],
  ])('%s in the preview: no start, the Start agent dialog opens prefilled', async (_n, over) => {
    s.preview.mockResolvedValue(preview(over as Partial<AgentPreview>))
    await signed()
    const dlg = await screen.findByRole('dialog', { name: 'Start agent dialog' }, T)
    expect(within(dlg).getByTestId('dialog-ticket')).toHaveTextContent('DEMO-0044')
    expect(JSON.parse(within(dlg).getByTestId('dialog-initial').textContent ?? '{}')).toMatchObject({ harness: 'claude-code', mode: 'refine' })
    expect(s.start).not.toHaveBeenCalled()
    await userClose()
  })

  it('an unacknowledged "runs as you" notice: the dialog, not an automatic start', async () => {
    setup({ acked: {} })
    await signed()
    const dlg = await screen.findByRole('dialog', { name: 'Start agent dialog' }, T)
    expect(dlg).toBeInTheDocument()
    expect(s.start).not.toHaveBeenCalled()
  })

  it('the login-shell notice starts only once acknowledged', async () => {
    s.preview.mockResolvedValue(preview({ warnings: [LOGIN_NOTICE] }))
    setup({ acked: { start_agent: true } })
    s.preview.mockResolvedValue(preview({ warnings: [LOGIN_NOTICE] }))
    await signed()
    await screen.findByRole('dialog', { name: 'Start agent dialog' }, T)
    expect(s.start).not.toHaveBeenCalled()
  })

  it('a changed start (launch.changed, runner.stale text as it is) is not retried: the dialog shows the host words', async () => {
    s.start.mockRejectedValue(new ApiError(409, { code: 'launch.changed', message: 'What would run changed: the runner restarted (runner.stale).', retryable: false }))
    await signed()
    const dlg = await screen.findByRole('dialog', { name: 'Start agent dialog' }, T)
    const initial = JSON.parse(within(dlg).getByTestId('dialog-initial').textContent ?? '{}')
    expect(initial.note).toContain('What would run changed: the runner restarted (runner.stale).')
    expect(initial.idem).toBeUndefined() // a refused start is not a lost answer: the dialog gets its own idem
    expect(s.preview).toHaveBeenCalledTimes(1)
    expect(s.start).toHaveBeenCalledTimes(1)
  })

  it('a lost answer (network error): the dialog reuses the review\'s idem, so Start returns the same run', async () => {
    s.start.mockRejectedValue(new TypeError('network'))
    await signed()
    const dlg = await screen.findByRole('dialog', { name: 'Start agent dialog' }, T)
    const initial = JSON.parse(within(dlg).getByTestId('dialog-initial').textContent ?? '{}')
    expect(initial.idem).toBe(s.start.mock.calls[0][3])
  })

  it('a preview that cannot be read goes to the dialog and nothing starts', async () => {
    s.preview.mockRejectedValue(new ApiError(503, { code: 'host.unavailable', message: 'Host not available', retryable: true }))
    await signed()
    await screen.findByRole('dialog', { name: 'Start agent dialog' }, T)
    expect(s.start).not.toHaveBeenCalled()
  })

  it('a reload never starts: a fresh page with nothing in memory previews and starts nothing', async () => {
    s.preview.mockResolvedValue(preview({ blocked: { code: 'launch.claimed', message: 'x', detail: {} } }))
    await signed()
    await screen.findByRole('dialog', { name: 'Start agent dialog' }, T)
    s.preview.mockClear()
    cleanup()
    const again = renderApp('/ticket/DEMO-0044')
    await waitFor(() => expect(again.router.state.location.pathname).toBe('/ticket/DEMO-0044'), T)
    await new Promise((res) => setTimeout(res, 150))
    expect(s.start).not.toHaveBeenCalled()
    expect(s.preview).not.toHaveBeenCalled()
  })
})
async function userClose() {
  const b = screen.queryByRole('button', { name: 'close-start-dialog' })
  if (b) b.click()
}

describe('partial failures', () => {
  it('the ticket landed, the access did not: said, with Start agent, and nothing started', async () => {
    setup({ grant: null })
    const { r, sheet } = await open()
    await r.user.click(await startBtn(sheet))
    expect((await sent()).agent).toEqual({ hours: 24 })
    await r.user.click(await screen.findByRole('button', { name: 'access-missing' }))
    await r.user.click(screen.getByRole('button', { name: 'cancelled' }))
    const info = await screen.findByTestId('agent-no-access', {}, T)
    expect(info).toHaveTextContent('DEMO-0044 exists. Agent access is not confirmed yet')
    // M-1: the emptied form is not left open behind the notice and the dialog that follows.
    expect(document.querySelector('[data-ticket-created]')).not.toBeNull()
    await r.user.click(within(info).getByRole('button', { name: 'Start agent' }))
    const dlg = await screen.findByRole('dialog', { name: 'Start agent dialog' }, T)
    expect(within(dlg).getByTestId('dialog-ticket')).toHaveTextContent('DEMO-0044')
    expect(s.preview).not.toHaveBeenCalled()
    expect(s.start).not.toHaveBeenCalled()
  })

  it('stopped earlier: the draft ticket is today\'s message only; no dialog, no start', async () => {
    const { r, sheet } = await open()
    await r.user.click(await startBtn(sheet))
    await r.user.click(await screen.findByRole('button', { name: 'stopped-early' }))
    await r.user.click(screen.getByRole('button', { name: 'cancelled' }))
    expect(screen.queryByTestId('agent-no-access')).toBeNull()
    expect(s.preview).not.toHaveBeenCalled()
    expect(s.start).not.toHaveBeenCalled()
  })

  it('Start agent after a missing access uses the choice that was reviewed, not what the form says now', async () => {
    setup({ grant: null })
    const { r, sheet } = await open()
    await r.user.click(await startBtn(sheet))
    await sent()
    await r.user.click(await screen.findByRole('button', { name: 'access-missing' }))
    await r.user.click(screen.getByRole('button', { name: 'cancelled' }))
    // the person changes the agent in the form behind the dialog
    localStorage.setItem(`orch.dashboard.new-ticket.agent.p_sev.${WS()}`, JSON.stringify({ start: true, harness: 'codex', mode: 'refine', model: '' }))
    const info = await screen.findByTestId('agent-no-access', {}, T)
    await r.user.click(within(info).getByRole('button', { name: 'Start agent' }))
    const dlg = await screen.findByRole('dialog', { name: 'Start agent dialog' }, T)
    expect(JSON.parse(within(dlg).getByTestId('dialog-initial').textContent ?? '{}')).toEqual({ harness: 'claude-code', mode: 'refine' })
  })
})

describe('a grant that runs out while the form is open', () => {
  it('is read again at submit time: under 15 minutes left at the click means the access is signed with the ticket', async () => {
    const soon = new Date(nowMs() + 16 * 60_000).toISOString().replace(/\.\d+Z$/, 'Z')
    setup({ grant: { ...GRANT, until: soon } })
    const { r, sheet } = await open()
    await startBtn(sheet)
    expect(within(sheet).queryByTestId('agent-access')).toBeNull() // 16 minutes left when the form was drawn: usable
    await new Promise((res) => setTimeout(res, 0))
    const realNow = nowMs()
    const spy = vi.spyOn(await import('@/lib/time'), 'nowMs').mockReturnValue(realNow + 5 * 60_000) // five minutes later: 11 left
    await r.user.click(await startBtn(sheet))
    expect((await sent()).agent).toEqual({ hours: 24 })
    spy.mockRestore()
  })
})
