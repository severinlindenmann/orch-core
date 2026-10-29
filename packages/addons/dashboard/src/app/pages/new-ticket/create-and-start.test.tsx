// "Create and start agent" (U4 PR2): the default path, Only create, agent access only without a usable grant, the step
// limit, the start compared with the review, the risk notices, partial failures, one idem, and a reload that never starts.
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { agentsApi, type AgentPreview } from '@/api/agents'
import { api, mockStore } from '@/api/client'
import { routingApi, type RoutingChoices } from '@/api/modelRouting'
import { ApiError } from '@/api/types'
import { clearMark, markOf } from '@/app/pages/ticket/progress'
import { fakeRequest, state } from '@/test/handoffHarness'
import { renderApp } from '@/test/renderApp'
import { nowMs } from '@/lib/time'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), handoffMode: () => true }))
// The signing dialog is its own feature; here it shows the action, the "not signed" block, and reports how it ended.
vi.mock('@/components/sign/SignPopover', () => ({
  SignPopover: (p: { title: string; startsAgent?: boolean; controls?: React.ReactNode; lead?: React.ReactNode; action: { action: string }; unsigned?: React.ReactNode; onSettled?: (phase: string, st: unknown, req: unknown, how?: { via: string; unsignedShown: boolean }) => void; onClose: () => void }) => {
    const req = { ...fakeRequest({ key: 'DEMO-0044', id: 'hr_' + '0'.repeat(25) + '7' }), op: 'new' }
    const done = (how?: { via: string; unsignedShown: boolean }) => p.onSettled?.('done', state(req, { state: 'submitted', key: 'DEMO-0044' }), req, how)
    return (
      <div role="dialog" aria-label={p.title}>
        <pre data-testid="sent-action">{JSON.stringify(p.action)}</pre>
        <span data-testid="starts-agent">{String(!!p.startsAgent)}</span>
        {p.lead}
        {p.controls}
        {p.unsigned}
        {/* "signed": one click from this popover, with the unsigned slot on screen (what the real popover reports then) */}
        <button onClick={() => done({ via: 'click', unsignedShown: true })}>signed</button>
        <button onClick={() => done({ via: 'click', unsignedShown: false })}>signed-click-slot-not-shown</button>
        <button onClick={() => done({ via: 'terminal', unsignedShown: false })}>signed-in-terminal</button>
        <button onClick={() => done({ via: 'hub', unsignedShown: false })}>signed-on-hub</button>
        <button onClick={() => done()}>signed-unknown-path</button>
        <button onClick={() => p.onSettled?.('done', state({ ...req, id: 'hr_' + '0'.repeat(25) + '8' }, { state: 'submitted', key: 'DEMO-0099' }), req)}>signed-other-request</button>
        <button onClick={() => p.onSettled?.('failed', state(req, { state: 'submitted', key: 'DEMO-0044', progress: { done: 4, of: 5, last: 'grant' } }), req)}>access-missing</button>
        <button onClick={() => p.onSettled?.('failed', state(req, { state: 'submitted', key: 'DEMO-0044', progress: { done: 2, of: 5 } }), req)}>stopped-early</button>
        <button onClick={p.onClose}>cancelled</button>
      </div>
    )
  },
}))

const T = { timeout: 5000 }
const WS = () => mockStore.workspaces[0].id
const failedOf = async (key = 'DEMO-0044') => {
  await waitFor(() => expect(markOf(WS(), key)?.failed).toBeDefined(), T)
  return markOf(WS(), key)!.failed!
}
const inHours = (h: number) => new Date(nowMs() + h * 3_600_000).toISOString().replace(/\.\d+Z$/, 'Z')
const CHOICES: RoutingChoices = {
  harnesses: [
    { id: 'claude-code', label: 'Claude', models: [{ id: 'opus', label: 'Opus' }, { id: 'sonnet', label: 'Sonnet' }] },
    { id: 'codex', label: 'Codex', models: [{ id: 'gpt-5.4', label: 'GPT-5.4' }] },
  ],
}
let GRANT = { id: 'gr_01M4Y46B485AXD4CBEJZ15Z1TV', until: '', scope: 'workable' as const }
const preview = (over: Partial<AgentPreview> = {}): AgentPreview => ({
  spec: 'f'.repeat(64), ticket: 'DEMO-0044', display: 'claude "work on DEMO-0044"', display_hidden: false, model: null, model_source: 'default',
  cwd: { kind: 'workspace' }, cwd_path: '/w/billing', cwd_path_hidden: false, will_create_worktree: false, login_shell: false,
  env: ['TERM'], grant: GRANT, blocked: null, warnings: [], ...over,
})
const run = { run: 'r_01M4Y46B485AXD4CBEJZ15Z1TV' } as never

let s: { runner: ReturnType<typeof vi.spyOn>; preview: ReturnType<typeof vi.spyOn>; start: ReturnType<typeof vi.spyOn>; notices: ReturnType<typeof vi.spyOn> }
function setup(opts: { grant?: typeof GRANT | null; runner?: { connected: boolean; enabled: boolean } | null; acked?: { start_agent?: boolean; login_shell?: boolean } } = {}) {
  const grant = opts.grant === undefined ? GRANT : opts.grant
  const runner = opts.runner === undefined ? { connected: true, enabled: true } : opts.runner
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ available: true, grant_hours: 168 } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(routingApi, 'choices').mockResolvedValue(CHOICES)
  s = {
    runner: vi.spyOn(agentsApi, 'runner').mockResolvedValue({ runner: runner ? { ...runner, terminals: false, now: new Date(nowMs()).toISOString(), grant: grant ? { ...grant, startable: true } : null } : null }),
    preview: vi.spyOn(agentsApi, 'preview').mockResolvedValue(preview()),
    start: vi.spyOn(agentsApi, 'startAfter').mockResolvedValue({ run }),
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
  await r.user.type(within(sheet).getByLabelText(/^What should be true/), 'Empty months export a header row')
  return { r, sheet }
}
const sent = async () => JSON.parse((await screen.findByTestId('sent-action')).textContent ?? '{}')
const createOnly = async (r: { user: { click: (e: Element) => Promise<void> } }, sheet: HTMLElement) => {
  await r.user.click(await within(sheet).findByRole('button', { name: 'More ways to create' }, T))
  await r.user.click(await screen.findByRole('menuitem', { name: 'Create without starting' }, T))
}
const startBtn = (sheet: HTMLElement) => within(sheet).findByRole('button', { name: 'Create and start' }, T)

beforeEach(() => { clearMark(WS(), 'DEMO-0044'); GRANT = { ...GRANT, until: inHours(48) }; setup() })

describe('the form', () => {
  it('offers Create and start as the main button, Create without starting in the menu, and a quiet agent summary', async () => {
    const { r, sheet } = await open()
    await startBtn(sheet)
    expect(within(sheet).queryByTestId('agent-line')).toBeNull()
    expect(sheet).not.toHaveTextContent('will draft a plan')
    expect(within(sheet).getByTestId('nt-agent-summary')).toHaveTextContent('Claude Code · Write requirements and plan')
    expect(within(sheet).queryByRole('button', { name: 'Change' })).toBeNull()
    await r.user.click(within(sheet).getByRole('button', { name: 'More ways to create' }))
    expect(await screen.findByRole('menuitem', { name: 'Create without starting' }, T)).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Choose agent…' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Create and start' })).toBeNull()
  })

  it('Choose agent… opens the shared picker; the footer summary follows the choice, which is remembered for this workspace', async () => {
    const { r, sheet } = await open()
    await r.user.click(await within(sheet).findByRole('button', { name: 'More ways to create' }, T))
    await r.user.click(await screen.findByRole('menuitem', { name: 'Choose agent…' }, T))
    const dlg = await screen.findByTestId('agent-change')
    await r.user.click(within(dlg).getByRole('radio', { name: 'Codex' }))
    await r.user.selectOptions(within(dlg).getByLabelText('What it does'), 'work')
    await r.user.click(within(dlg).getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByTestId('agent-change')).toBeNull(), T)
    expect(within(sheet).getByTestId('nt-agent-summary')).toHaveTextContent('Codex · Do the work')
    const stored = JSON.parse(localStorage.getItem(`orch.dashboard.new-ticket.agent.p_sev.${WS()}`) ?? '{}')
    expect(stored).toMatchObject({ harness: 'codex', mode: 'work', start: true })
  })

  it('Create without starting from the menu is used once: the main button and the stored choice stay as they were', async () => {
    const { r, sheet } = await open()
    await createOnly(r, sheet)
    expect((await sent()).agent).toBeUndefined()
    expect(await screen.findByTestId('nt-lead')).toHaveTextContent('Creates the ticket. No agent starts.')
    expect(JSON.parse(localStorage.getItem(`orch.dashboard.new-ticket.agent.p_sev.${WS()}`) ?? '{}').start).not.toBe(false)
    expect(await startBtn(sheet)).toBeInTheDocument()
    expect(within(sheet).queryByRole('button', { name: 'Create ticket' })).toBeNull()
  })

  it('the main button keeps its choice: with start=false stored, Create ticket is main and the menu start is used once', async () => {
    const key = `orch.dashboard.new-ticket.agent.p_sev.${WS()}`
    const stored = JSON.stringify({ start: false, harness: 'claude-code', mode: 'work', model: '' })
    const { r, sheet } = await open({ [key]: stored })
    expect(await within(sheet).findByRole('button', { name: 'Create ticket' }, T)).toBeInTheDocument()
    await r.user.click(within(sheet).getByRole('button', { name: 'More ways to create' }))
    await r.user.click(await screen.findByRole('menuitem', { name: 'Create and start' }, T))
    expect(await screen.findByTestId('not-signed-start')).toBeInTheDocument()
    expect(JSON.parse(localStorage.getItem(key) ?? '{}').start).toBe(false) // the menu did not change the default
    cleanup()
    const again = await open({ [key]: stored })
    await again.r.user.click(await within(again.sheet).findByRole('button', { name: 'Create ticket' }, T))
    expect((await sent()).agent).toBeUndefined()
    expect(JSON.parse(localStorage.getItem(key) ?? '{}').start).toBe(false) // the main button persists its choice
  })

  it('shows only Create, and says why, when the runner is not connected', async () => {
    setup({ runner: { connected: false, enabled: true } })
    const { sheet } = await open()
    expect(await within(sheet).findByTestId('agent-line-off', {}, T)).toHaveTextContent('No agent runner is connected.')
    expect(within(sheet).getByRole('button', { name: 'Create ticket' })).toBeInTheDocument()
    expect(within(sheet).queryByRole('button', { name: 'Create and start' })).toBeNull()
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
})

describe('what is signed', () => {
  it('asks the host to conditionally grant access and binds the launch settings', async () => {
    setup({ grant: null }) // no usable access: the grant is proposed (with valid access the request carries none, see create-and-start-click)
    const { r, sheet } = await open()
    await r.user.click(await startBtn(sheet))
    const action = await sent()
    expect(action.agent).toEqual({ hours: 168 })
    expect(action.launch).toEqual({ harness: 'claude-code', mode: 'refine' })
    expect(await screen.findByTestId('not-signed-start')).toHaveTextContent('Then: start an agent on the new ticket.')
    expect(screen.getByTestId('not-signed-start').textContent!.length).toBeLessThan(230) // one line, not a paragraph
    expect(screen.getByTestId('starts-agent')).toHaveTextContent('true') // the dialog's steps then say the agent starts with the access already there
  })

  it('without a grant (or one with under 15 minutes left) the access is signed last in the same request, 1/3/7 days', async () => {
    setup({ grant: { ...GRANT, until: inHours(0.2) } })
    const { r, sheet } = await open()
    expect(within(sheet).queryByLabelText('Give agents access for')).toBeNull() // asked in the review, not in the form
    await r.user.click(await startBtn(sheet))
    const hours = await screen.findByLabelText('Give agents access for', {}, T)
    expect([...hours.querySelectorAll('option')].map((o) => o.textContent)).toEqual(['8 hours', '1 day', '3 days', '7 days'])
    await r.user.selectOptions(hours, '72')
    await waitFor(async () => expect((await sent()).agent).toEqual({ hours: 72 }), T)
  })

  it('Only create never asks for agent access', async () => {
    setup({ grant: null })
    const { r, sheet } = await open()
    await createOnly(r, sheet)
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
    await within(fits.sheet).findByRole('button', { name: 'Create and start' }, T)
    expect(within(fits.sheet).queryByTestId('agent-too-many')).toBeNull()
    cleanup()
    const over = await open({ [key]: JSON.stringify({ ...draft, acceptance: [...draft.acceptance, '8'] }) }) // 16 + 1 access = 17
    expect(await within(over.sheet).findByTestId('agent-too-many', {}, T)).toHaveTextContent('too many parts')
    expect(within(over.sheet).queryByRole('button', { name: 'Create and start' })).toBeNull()
    await over.r.user.click(within(over.sheet).getByRole('button', { name: 'Create ticket' }))
    expect((await sent()).agent).toBeUndefined()
  })
})
describe('after the ticket exists', () => {
  async function signed() {
    const { r, sheet } = await open()
    await r.user.click(await startBtn(sheet))
    await r.user.click(await screen.findByRole('button', { name: 'signed' }))
    return r
  }
  it('starts with only the reviewed request id, without a browser preview', async () => {
    const r = await signed()
    await waitFor(() => expect(s.start).toHaveBeenCalledTimes(1), T)
    expect(s.start).toHaveBeenCalledWith(WS(), 'hr_' + '0'.repeat(25) + '7')
    expect(s.preview).not.toHaveBeenCalled()
    await waitFor(() => expect(r.router.state.location.pathname).toBe('/ticket/DEMO-0044'), T)
    expect(markOf(WS(), 'DEMO-0044')?.failed).toBeUndefined()
  })
  it('a lost response retains the request id for the host-idempotent retry', async () => {
    s.start.mockRejectedValue(new TypeError('network'))
    await signed()
    expect((await failedOf()).initial).toMatchObject({ afterRequest: 'hr_' + '0'.repeat(25) + '7' })
  })
  it('reports a host refusal and never falls back to an unbound start', async () => {
    s.start.mockRejectedValue(new ApiError(409, { code: 'sequence.incomplete', message: 'Every step must land.', retryable: false }))
    const unbound = vi.spyOn(agentsApi, 'start')
    await signed()
    expect((await failedOf()).reason).toContain('Every step must land.')
    expect(unbound).not.toHaveBeenCalled()
  })
})
describe('partial failures', () => {
  it('the ticket landed, the access did not: said in the failure mark, and nothing started', async () => {
    setup({ grant: null })
    const { r, sheet } = await open()
    await r.user.click(await startBtn(sheet))
    expect((await sent()).agent).toEqual({ hours: 168 })
    await r.user.click(await screen.findByRole('button', { name: 'access-missing' }))
    await r.user.click(screen.getByRole('button', { name: 'cancelled' }))
    const failed = await failedOf()
    expect(failed.reason).toBe('Agent access is not confirmed yet. Start an agent to give access.')
    // M-1: the emptied form is not left open; the ticket page is.
    await waitFor(() => expect(r.router.state.location.pathname).toBe('/ticket/DEMO-0044'), T)
    expect(s.preview).not.toHaveBeenCalled()
    expect(s.start).not.toHaveBeenCalled()
  })

  it('stopped earlier: the draft ticket is today\'s message only; no failure mark, no start', async () => {
    const { r, sheet } = await open()
    await r.user.click(await startBtn(sheet))
    await r.user.click(await screen.findByRole('button', { name: 'stopped-early' }))
    await r.user.click(screen.getByRole('button', { name: 'cancelled' }))
    expect(markOf(WS(), 'DEMO-0044')?.failed).toBeUndefined()
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
    const failed = await failedOf()
    expect(failed.initial).toEqual({ harness: 'claude-code', mode: 'refine' })
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
    expect((await sent()).agent).toEqual({ hours: 168 })
    spy.mockRestore()
  })
})
