import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { answers, groupProgress, plainReason, Setup, reviewFacts, summarize } from './Setup'
import { SetupAPI, SetupError, bootstrapSetup, isSetupPage, type SetupState, type Review } from '../api/setup'

const state: SetupState = {
  phase: 'profile',
  mode: 'new',
  dir: '/demo/project',
  dir_name: 'project',
  suggested: { name: 'Demo', workspace_name: 'Personal Workspace', prefix: 'TST' },
  addons: [
    {
      id: 'dashboard',
      title: 'Dashboard',
      desc: 'Browser',
      required: true,
      default: true,
      kind: 'addon',
      capabilities: [],
    },
    {
      id: 'worktrees',
      title: 'Own copy per ticket',
      desc: 'Separate working copies',
      required: false,
      default: false,
      kind: 'addon',
      capabilities: ['spawn_agent', 'pty'],
    },
    {
      id: 'quick',
      title: 'Quick tasks',
      desc: 'Small one-off tasks',
      required: false,
      default: true,
      kind: 'flag',
      capabilities: [],
    },
  ],
  methods: ['terminal', 'click'],
  click_available: true,
  click_reason: 'click_relay_unsupported: phone approvals need terminal signing',
  click_risk: 'Convenience signing: demo risk sentence.',
  agent_hours: [8, 24, 72, 168],
  relay: { available: true, enabled: true },
  missing: [],
  labels: { runner_grant: "Agent access for this computer's runner", custody: 'One-click approval on this computer' },
}
const current = {
  name: 'Demo',
  prefix: 'TST',
  addons_on: ['dashboard'],
  agent_hours: 72,
  runner: { enabled: true, terminals: false },
  runner_grant: false,
  method: 'terminal' as const,
}
const review: Review = {
  review_id: 'demo-review',
  digest: 'demo-digest',
  fingerprint: ['demo', 'not', 'real'],
  items: [
    {
      id: 'settings',
      title: 'Settings',
      lines: ['Agent access: 8 hours'],
      details: ['exact details'],
      volatile: ['set at signing'],
      type: 'settings.changed',
      fields: { set: { grant_hours: 24 } },
      placeholders: [],
      placeholder_details: [],
    },
  ],
  runner_grant: null,
  flags: [],
  risk: null,
  tty_confirm: false,
}
function fake(
  options: {
    finish?: boolean
    typed?: boolean
    noTty?: boolean
    waiting?: string
    running?: boolean
    stateError?: boolean
    failure?: boolean
    done?: boolean
    trust?: 'skipped' | 'done'
    trustCode?: string
    click?: boolean
    clickOff?: string
    relayUnavailable?: boolean
    claudeAvailable?: boolean
    noGit?: boolean
    complete?: boolean
    finishUrl?: string
    defaults?: boolean
    suggested?: Record<string, unknown>
    os?: string
  } = {},
) {
  const calls: { path: string; body: unknown }[] = []
  const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url).replace('/api/setup/', '')
    const body = init?.body ? JSON.parse(String(init.body)) : undefined
    calls.push({ path, body })
    let result: unknown = { ok: true }
    if (path === 'state')
      result = {
        ...state,
        ...(options.stateError ? {phase: 'failed', error: {code: 'demo.failure', message: 'Runner could not connect'}} : {}),
        ...(options.clickOff !== undefined ? { click_available: false, click_reason: options.clickOff } : {}),
        ...(options.relayUnavailable ? { relay: { available: false, enabled: false } } : {}),
        ...(options.noGit ? { git: { repo: false } } : {}),
        ...(options.claudeAvailable ? { claude: { available: true } } : {}),
        ...(options.os ? { os: options.os } : {}),
        mode: options.finish ? 'finish' : 'new',
        missing: options.finish && !options.complete ? ['addon:worktrees', 'runner_grant'] : [],
        ...(options.complete ? { complete: true } : {}),
        ...(options.defaults
          ? {
              suggested: { name: 'Demo', workspace_name: 'Personal Workspace', prefix: 'TST', agent_hours: 72, agent_grant: true },
              addons: state.addons.map((a) => (a.id === 'worktrees' ? { ...a, default: true } : a)),
            }
          : {}),
        ...(options.suggested ? { suggested: { name: 'Demo', workspace_name: 'Personal Workspace', prefix: 'TST', ...options.suggested } } : {}),
        ...(options.finish ? { current } : {}),
      }
    if (path === 'secrets/reveal')
      result = {
        passphrase: options.click ? null : 'demo-not-a-real-passphrase',
        recovery_words: Array.from({ length: 24 }, (_, i) => `demo-word-${i + 1}`),
      }
    if (path === 'secrets/confirm')
      result = { ok: true }
    if (path === 'review')
      result = {
        ...review,
        ...(options.finish
          ? {}
          : {
              workspace: {
                prefix: 'RA',
                name: 'Severin',
                workspace_name: 'Marketing',
                dir: '/demo/orch-run2-a',
                lines: ['Workspace Marketing (RA) for Severin', 'Ticket prefix: RA', 'Person: Severin', 'Folder: /demo/orch-run2-a'],
              },
            }),
        tty_confirm: !!options.finish,
        ...(options.typed
          ? {
              tty_confirm: true,
              runner_grant: { hours: 72, scope: 'tickets', holder: "this machine's runner, never shown" },
              items: [
                ...review.items,
                { id: 'runner_grant', title: 'Agent access held by this machine', lines: [], details: [], volatile: [], type: 'grant.issued', fields: { type: 'grant.issued', hours: 72, scope: 'workable', verbs: 'agent' }, placeholders: [], placeholder_details: [] },
              ],
            }
          : {}),
        ...(options.noTty ? { tty_confirm: false, fingerprint: null } : {}),
      }
    if (path === 'finish') result = { url: options.finishUrl ?? '/', workspace_id: 'ws-new-2' }
    if (path === 'create') result = { run_id: 'demo-run' }
    if (path.startsWith('progress'))
      result = {
        steps: [
          { id: 'genesis', label: 'Create workspace', status: options.failure ? 'failed' : options.running ? 'doing' : 'todo' },
          ...(options.running ? [{ id: 'register', label: 'Starting orch in the background', status: 'doing' as const }] : []),
          ...(options.trust ? [{ id: 'trust', label: 'Letting Claude Code trust this folder', status: options.trust }] : []),
        ],
        reasons: options.trust === 'skipped' ? { trust: 'Claude Code has not been started on this computer yet, so orch could not answer for you.' } : {},
        reason_codes: options.trust === 'skipped' ? { trust: options.trustCode ?? 'not_started' } : {},
        done: !!options.done,
        waiting: options.waiting,
        error: options.failure
          ? { code: 'demo.failure', message: 'Could not create workspace', finishable: !!options.finish }
          : null,
        retry: [],
        workspace: null,
      }
    return new Response(JSON.stringify(result), { status: 200 })
  })
  return { api: new SetupAPI('demo-not-a-real-token', fetcher as typeof fetch), calls, fetcher }
}
async function approval() {
  await screen.findByRole('button', { name: 'Continue →' })
  for (let i = 0; i < 3; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
}
async function reveal() {
  await approval()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
}
async function check() {
  fireEvent.click(screen.getByRole('button', { name: 'I saved my words — continue →' }))
}
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  history.replaceState(null, '', '/')
  sessionStorage.clear()
  localStorage.clear()
})

test('presents the one-time nonce from the fragment (stripped first) and stores no token', async () => {
  history.replaceState(null, '', '/#launch=demo-not-a-real-code&to=/setup')
  const fetcher = vi.fn(async (_url, init) => {
    expect(location.hash).toBe('')
    expect(location.pathname).toBe('/setup')
    expect(init.body).toBe('{"nonce":"demo-not-a-real-code"}')
    return new Response(JSON.stringify({ token: 'demo-not-a-real-token' }))
  })
  await bootstrapSetup(fetcher as typeof fetch)
  expect(fetcher).toHaveBeenCalledOnce()
  expect(sessionStorage.length).toBe(0)
  expect(localStorage.length).toBe(0)
})

test('shows secrets only as text, asks for no typing, drops secrets after confirm', async () => {
  const f = fake()
  const { container } = render(<Setup api={f.api} />)
  await reveal()
  expect(container.querySelectorAll('.recovery li')).toHaveLength(24)
  expect(screen.getByText('demo-not-a-real-passphrase').tagName).toBe('P')
  expect(
    [...container.querySelectorAll('input,textarea')].every((el) => !(el as HTMLInputElement).value.includes('demo-')),
  ).toBe(true)
  expect(screen.queryByRole('button', { name: /Copy passphrase|PDF/ })).not.toBeInTheDocument()
  expect(screen.getByText(/If setup stops before/)).toBeInTheDocument()
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(container.textContent).not.toContain('demo-word-')
  expect(container.textContent).not.toContain('demo-not-a-real-passphrase')
  expect(f.calls.find((c) => c.path === 'secrets/confirm')?.body).toEqual({ acknowledged: true })
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await waitFor(() =>
    expect(f.calls.find((c) => c.path === 'create')?.body).toEqual({ review_id: 'demo-review', digest: 'demo-digest' }),
  )
})

test('one click disables relay with its named reason and never shows a passphrase', async () => {
  // Even an unexpected non-null passphrase from core must stay hidden for One click.
  const f = fake()
  render(<Setup api={f.api} />)
  await approval()
  fireEvent.click(screen.getByLabelText(/Connect relay and phone/))
  fireEvent.click(screen.getByLabelText(/One click on this computer/))
  expect(screen.getByLabelText(/Connect relay and phone/)).toBeDisabled()
  expect(screen.getByLabelText(/Connect relay and phone/)).not.toBeChecked()
  expect(screen.getByText(/Phone approvals are not available with One click yet/)).toBeInTheDocument()
  expect(screen.queryByText(/click_relay_unsupported/)).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  expect(screen.queryByRole('heading', { name: 'Your password' })).not.toBeInTheDocument()
  expect(f.calls.find((c) => c.path === 'profile')?.body).toMatchObject({ method: 'click', relay: false })
})

test('1236: an unknown or empty One click reason gets a generic plain sentence, never the raw code', async () => {
  render(<Setup api={fake({ clickOff: 'some_reason' }).api} />)
  await approval()
  expect(screen.getByText('One click is not available on this computer; you approve in your terminal.')).toBeInTheDocument()
  expect(screen.queryByText(/some_reason/)).not.toBeInTheDocument()
})

test('P01: One click with a runner grant has no terminal box and no terminal wait; Create runs straight away', async () => {
  const f = fake({ typed: true, noTty: true, suggested: { method: 'click' } })
  render(<Setup api={f.api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '3 days' }))
  fireEvent.click(screen.getByLabelText(/Let agents start on this computer now/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(screen.queryByRole('heading', { name: 'Check your terminal after you click' })).toBeNull()
  expect(screen.queryByText(/type yes/i)).toBeNull()
  expect(screen.getByRole('button', { name: 'Create workspace' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await screen.findByRole('heading', { name: 'Setting up your workspace' })
  expect(screen.queryByRole('heading', { name: 'Confirm in your terminal' })).toBeNull()
  expect(f.calls.some((c) => c.path === 'create')).toBe(true)
})

test('run 8: One click is the default where core suggests it, with the risk sentence before Continue; Terminal stays selectable', async () => {
  const f = fake({ suggested: { method: 'click' } })
  render(<Setup api={f.api} />)
  await approval()
  expect(screen.getByLabelText(/One click on this computer/)).toBeChecked()
  expect(screen.getByText('Recommended')).toBeVisible()
  expect(screen.getByText('No typing. Best on your own computer.')).toBeVisible()
  // r11-P16: Passphrase is behind "Change these settings"
  expect(screen.getByText(/Safest if others use this computer/)).not.toBeVisible()
  fireEvent.click(screen.getByText('Change these settings'))
  expect(screen.getByText(/Safest if others use this computer/)).toBeVisible()
  const risk = screen.getByTestId('click-risk') as HTMLDetailsElement
  expect(risk.open).toBe(false) // the longer meaning is one click away, not in the main view
  expect(risk).toHaveTextContent('Anything running as you on this computer, including your agents, could approve for you. Choose a password if the computer is shared.')
  fireEvent.click(screen.getByLabelText(/Password/))
  expect(screen.queryByTestId('click-risk')).not.toBeInTheDocument()
  fireEvent.click(screen.getByLabelText(/One click on this computer/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  expect(f.calls.find((c) => c.path === 'profile')?.body).toMatchObject({ method: 'click' })
})

test('run 8: the records history is ticked by default, can be switched off, and the choice is sent with the profile', async () => {
  const f = fake()
  render(<Setup api={f.api} />)
  await approval()
  const box = screen.getByRole('switch', { name: /Keep a history of this workspace/ })
  expect(box).toBeChecked()
  fireEvent.click(box)
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  expect(f.calls.find((c) => c.path === 'profile')?.body).toMatchObject({ records: false })
})

test('long runner access is one plain click after the review; digest and fingerprint stay bound while waiting for terminal', async () => {
  const f = fake({ typed: true })
  render(<Setup api={f.api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '3 days' }))
  fireEvent.click(screen.getByLabelText(/Let agents start on this computer now/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(document.querySelector('.review-summary')).toHaveTextContent('may work for 3 days')
  expect(screen.queryByText(/scope workable/)).toBeNull() // the raw values are only in the technical details
  expect(screen.getByText(/Return to the terminal window where you started orch setup/)).toBeInTheDocument()
  expect(screen.getByText(/Check the terminal shows/)).toHaveTextContent('demo not real. If it does, press Enter there.')
  // everything signed stays inspectable, collapsed in one disclosure
  expect(screen.getByText('Details').closest('details')).not.toHaveAttribute('open')
  expect(screen.getByText("this machine's runner, never shown")).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Create workspace' })).toBeEnabled()
  expect(screen.queryByLabelText(/Type/)).toBeNull()
  // B11: the terminal asks only after Create; the review says so instead of "confirm in your terminal" now
  expect(screen.getByRole('heading', { name: 'Check your terminal after you click' })).toBeInTheDocument()
  expect(screen.queryByText(/type yes/i)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await screen.findByRole('heading', { name: 'Confirm in your terminal' })
  // three large tiles, in order; the browser draws no imitation of the terminal
  expect(within(screen.getByRole('list', { name: 'Your three words' })).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['demo', 'not', 'real'])
  expect(screen.getByText(/Continue in the setup window on this computer/)).toBeInTheDocument()
  expect(screen.getByText(/press Enter in that window/)).toBeInTheDocument()
  expect(screen.getByText('Waiting for you in the setup window…')).toBeInTheDocument()
  expect(screen.getByText('Are the words different?').closest('details')).not.toHaveAttribute('open')
  expect(f.calls.find((c) => c.path === 'create')?.body).toEqual({
    review_id: 'demo-review',
    digest: 'demo-digest',
  })
})

test('finish skips secrets; progress exposes failure and Finish setup later', async () => {
  const f = fake({ finish: true, failure: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Finish setting up your workspace' })
  fireEvent.click(screen.getByLabelText(/Own copy per ticket/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(f.calls.some((c) => c.path.startsWith('secrets/'))).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
  await screen.findByRole('heading', { name: 'Setup needs your attention' })
  expect(screen.getByText('Failed')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Finish setup later' }))
  await screen.findByRole('heading', { name: 'Setup paused' })
})

test('copy is recovery-only, best effort clears after 60 seconds; print uses browser print', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  const print = vi.spyOn(window, 'print').mockImplementation(() => {})
  const f = fake()
  render(<Setup api={f.api} />)
  await reveal()
  vi.useFakeTimers()
  fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
  await Promise.resolve()
  expect(writeText.mock.calls[0][0]).toContain('demo-word-24')
  expect(writeText.mock.calls[0][0]).not.toContain('passphrase')
  await vi.advanceTimersByTimeAsync(60_000)
  expect(writeText).toHaveBeenLastCalledWith('')
  fireEvent.click(screen.getByRole('button', { name: 'Print' }))
  expect(print).toHaveBeenCalledOnce()
})

test('review.changed rebuilds the review without trying to reveal secrets again', async () => {
  const f = fake()
  const fetcher: typeof fetch = async (url, init) =>
    String(url).endsWith('/create')
      ? new Response(JSON.stringify({ error: { code: 'review.changed', message: 'changed' } }), { status: 409 })
      : f.fetcher(url, init)
  render(<Setup api={new SetupAPI('demo-not-a-real-token', fetcher)} />)
  await reveal()
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await screen.findByRole('heading', { name: 'How do you want to approve things?' })
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(f.calls.filter((c) => c.path === 'secrets/reveal')).toHaveLength(1)
  expect(f.calls.filter((c) => c.path === 'review')).toHaveLength(2)
})

test('the shared launch link (root with to=/setup) and a reload of /setup are the setup page; a login link is not', () => {
  history.replaceState(null, '', '/#launch=demo-not-a-real-code&to=/setup')
  expect(isSetupPage()).toBe(true)
  history.replaceState(null, '', '/setup')
  expect(isSetupPage()).toBe(true)
  history.replaceState(null, '', '/#launch=demo-not-a-real-code&to=/w/ABC')
  expect(isSetupPage()).toBe(false)
})

test('without a nonce nothing is claimed', async () => {
  history.replaceState(null, '', '/setup')
  const fetcher = vi.fn()
  await expect(bootstrapSetup(fetcher)).rejects.toThrow('session.required')
  expect(fetcher).not.toHaveBeenCalled()
})

test('a service-worker-controlled page refuses the exchange', async () => {
  history.replaceState(null, '', '/setup#launch=demo-not-a-real-code')
  const previous = Object.getOwnPropertyDescriptor(navigator, 'serviceWorker')
  Object.defineProperty(navigator, 'serviceWorker', { value: { controller: {} }, configurable: true })
  const fetcher = vi.fn()
  try {
    await expect(bootstrapSetup(fetcher)).rejects.toThrow('setup.service_worker')
    expect(fetcher).not.toHaveBeenCalled()
  } finally {
    if (previous) Object.defineProperty(navigator, 'serviceWorker', previous)
    else Reflect.deleteProperty(navigator, 'serviceWorker')
  }
})

async function finishFinishMode(f: ReturnType<typeof fake>) {
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Finish setting up your workspace' })
  fireEvent.click(screen.getByLabelText(/Own copy per ticket/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
  await screen.findByRole('heading', { name: 'Your workspace is ready' })
}

test('r10-P10: a skipped Claude Code trust step is said on the progress page, with the plain reason behind Why?', async () => {
  await finishFinishMode(fake({ finish: true, done: true, trust: 'skipped' }))
  expect(screen.getByText('Your AI helper will ask once to trust this folder. Say yes there.')).toBeInTheDocument()
  expect(screen.getByText('Why?')).toBeInTheDocument()
  expect(screen.getByText(/has not been started on this computer yet/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Open your workspace →' })).toBeEnabled()
})

test("r10-P10: someone else's Claude Code settings never get the 'say yes' advice", async () => {
  await finishFinishMode(fake({ finish: true, done: true, trust: 'skipped', trustCode: 'foreign_config' }))
  expect(screen.queryByText(/Say yes there/)).toBeNull()
  expect(screen.getByText('Your AI helper will ask once about this folder. See why below.')).toBeInTheDocument()
  expect(screen.getByText('Why?')).toBeInTheDocument()
})

test('r10-P10: no trust note when the step was done or not asked for', async () => {
  await finishFinishMode(fake({ finish: true, done: true, trust: 'done' }))
  expect(screen.queryByText(/Your AI helper will ask once to trust this folder/)).toBeNull()
})

test('successful progress offers the workspace, while finish mode never reveals secrets', async () => {
  const f = fake({ finish: true, done: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Finish setting up your workspace' })
  fireEvent.click(screen.getByLabelText(/Own copy per ticket/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
  await screen.findByRole('heading', { name: 'Your workspace is ready' })
  expect(screen.getByRole('button', { name: 'Open your workspace →' })).toBeEnabled()
  localStorage.setItem('orch.workspace', 'ws-old') // the dashboard already knows another workspace
  const assign = vi.fn()
  vi.stubGlobal('location', { origin: location.origin, assign })
  fireEvent.click(screen.getByRole('button', { name: 'Open your workspace →' }))
  await waitFor(() => expect(assign).toHaveBeenCalledWith('/'))
  // F1: a second workspace's setup selects the NEW workspace, not the one remembered before
  expect(localStorage.getItem('orch.workspace')).toBe('ws-new-2')
  expect(f.calls.some((c) => c.path.startsWith('secrets/'))).toBe(false)
})

test('relay is only offered when the add-on is available and enabled', async () => {
  const f = fake({ relayUnavailable: true })
  render(<Setup api={f.api} />)
  await approval()
  expect(screen.queryByLabelText(/Connect relay and phone/)).not.toBeInTheDocument()
})

test('state failure shows core message and a next step', async () => {
  render(<Setup api={fake({ stateError: true }).api} />)
  expect(await screen.findByText('Runner could not connect')).toBeInTheDocument()
  expect(screen.getByText(/in this folder to resume/)).toBeInTheDocument()
})

test('extended access without a runner grant is one plain click, with no word to type', async () => {
  const f = fake()
  render(<Setup api={f.api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '1 day' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(screen.queryByLabelText(/Type/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await waitFor(() => expect(f.calls.find(c => c.path === 'create')?.body).toEqual({ review_id: 'demo-review', digest: 'demo-digest' }))
  expect(f.calls.find(c => c.path === 'profile')?.body).toMatchObject({ agent_grant: false, agent_hours: 24 })
})

test.each(['waiting for terminal', 'waiting for runner'])('shows progress waiting: %s', async (waiting) => {
  const f = fake({ waiting })
  render(<Setup api={f.api} />)
  await reveal()
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  expect(await screen.findByRole('status')).toHaveTextContent(
    waiting === 'waiting for terminal' ? 'Now look at your terminal: orch asks you to confirm there.' : 'Waiting for the agent runner to connect…')
})

function failing(status: number, code?: string) {
  const fetcher = vi.fn(
    async () => new Response(JSON.stringify(code ? { error: { code } } : {}), { status }),
  ) as unknown as typeof fetch
  return new SetupAPI('tok', fetcher)
}

test('the cause is in plain words and the code sits under Technical details', () => {
  expect(plainReason('session.required')).toMatch(/lost its link to orch/)
  expect(plainReason('whatever')).toBe('Setup could not go on.')
  for (const code of ['session.required', 'setup.ended', 'setup.token', 'http.502', 'client: Error: x']) expect(plainReason(code)).not.toMatch(/session\.|setup\.|http\./)
})

test.each([
  [410, 'setup.ended'],
  [401, 'setup.token'],
])('initial state %i shows Reason: %s', async (status, code) => {
  const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
  render(<Setup api={failing(status, code)} />)
  expect(await screen.findByText(`Reason: ${code}`)).toBeInTheDocument()
  expect(screen.getByText(/This session has ended/)).toBeInTheDocument()
  // An ended link is an ordinary outcome the page explains: nothing is written to the console.
  expect(spy).not.toHaveBeenCalled()
  spy.mockRestore()
})

test('an HTTP error without a JSON body shows the status', async () => {
  const fetcher = vi.fn(async () => new Response('nope', { status: 502 })) as unknown as typeof fetch
  const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
  render(<Setup api={new SetupAPI('tok', fetcher)} />)
  expect(await screen.findByText('Reason: http.502')).toBeInTheDocument()
  spy.mockRestore()
})

test('a client exception while handling state is reported as client:, not an API code', async () => {
  const fetcher = vi.fn(
    async () => new Response(JSON.stringify({ ...state, addons: null }), { status: 200 }),
  ) as unknown as typeof fetch
  const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
  render(<Setup api={new SetupAPI('tok', fetcher)} />)
  expect(await screen.findByText(/^Reason: client: TypeError: /)).toBeInTheDocument()
  expect(spy).toHaveBeenCalledWith('[orch setup]', expect.stringMatching(/^client: TypeError/))
  spy.mockRestore()
})

test('a bootstrap failure on mount shows its reason without fragments', async () => {
  document.body.innerHTML = '<div id="root"></div>'
  const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
  const { mount } = await import('./Setup')
  await mount(Promise.reject(new Error('boom#launch=SECRET')))
  expect(await screen.findByText('Reason: client: Error: boom')).toBeInTheDocument()
  expect(document.body.textContent).not.toContain('SECRET')
  spy.mockRestore()
})

test('a used-up setup link leads to the dashboard and keeps the terminal steps behind a small link', async () => {
  document.body.innerHTML = '<div id="root"></div>'
  const { mount } = await import('./Setup')
  await mount(Promise.reject(new SetupError('setup.ended')))
  expect(await screen.findByRole('heading', { name: 'This setup link was already used' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Open dashboard/ })).toHaveAttribute('href', '/')
  const again = screen.getByText('Need to set up again?').closest('details')!
  expect(again).not.toHaveAttribute('open')
  expect(again).toHaveTextContent('press Enter for a fresh link')
  expect(screen.getByText('Reason: setup.ended')).toBeInTheDocument()
})

test('another bootstrap failure keeps a generic heading with the same way out', async () => {
  document.body.innerHTML = '<div id="root"></div>'
  const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
  const { mount } = await import('./Setup')
  await mount(Promise.reject(new SetupError('setup.service_worker')))
  expect(await screen.findByRole('heading', { name: 'Setup could not open' })).toBeInTheDocument()
  expect(screen.queryByText('This setup link was already used')).toBeNull()
  expect(screen.getByRole('link', { name: /Open dashboard/ })).toHaveAttribute('href', '/')
  expect(screen.getByText('Reason: setup.service_worker')).toBeInTheDocument()
  spy.mockRestore()
})

test('review is a compact plain summary; blanks and "set at signing" lines stay out of the details', async () => {
  const f = fake({ typed: true })
  render(<Setup api={f.api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '3 days' }))
  fireEvent.click(screen.getByLabelText(/Let agents start on this computer now/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(screen.getByText('Review your setup')).toBeInTheDocument()
  expect(screen.queryByText('""')).toBeNull()
  expect(screen.queryByText(/: set at signing/)).toBeNull()
  expect(screen.getByText('Times and IDs are filled in when you sign.')).toBeInTheDocument()
})

const page = (container: HTMLElement) => container.textContent ?? ''

test('finish shows the current values read-only and offers only what is missing, never a downgrade', async () => {
  const f = fake({ finish: true })
  const { container } = render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Finish setting up your workspace' })
  expect(page(container)).toContain('Agents may work for3 days')
  expect(screen.getByText('Already set up')).toBeInTheDocument()
  // r10-P11: whether agents can start now is the top bar's message only, so setup lists no agent-access row
  expect(page(container)).not.toMatch(/Agent access for this|Given before|Not given/)
  // the name and the short name are not inputs, the add-on that is on is not a switch
  expect(screen.queryByLabelText('Your name')).toBeNull()
  expect(screen.getAllByRole('switch').map((el) => el.closest('label')?.textContent ?? '')).toEqual([
    expect.stringContaining('Own copy per ticket'),
    expect.stringContaining('Let agents start on this computer now'),
    expect.stringContaining('Approve with one click'),
  ])
  expect(screen.getByRole('button', { name: 'Continue →' })).toBeDisabled()
  fireEvent.click(screen.getByLabelText(/Own copy per ticket/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(f.calls.find((c) => c.path === 'profile')?.body).toMatchObject({
    name: 'Demo',
    prefix: 'TST',
    addons: ['dashboard', 'worktrees'],
    agent_hours: 72,
    agent_grant: false,
    method: 'terminal',
  })
  // no id and no code in the words of the page
  expect(page(container)).not.toMatch(/runner_grant|custody|addon:/)
})

test('finish never lists custody or ids; missing items are in words', async () => {
  const f = fake({ finish: true })
  const { container } = render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Finish setting up your workspace' })
  expect(page(container)).toContain('Let agents start on this computer now') // the offer stays; the old read-only row is gone (r10-P11)
  expect(page(container)).not.toMatch(/runner_grant|custody|addon:/)
})

test('the steps have visible names next to the dots', async () => {
  render(<Setup api={fake().api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  const nav = screen.getByRole('navigation', { name: 'Setup progress' })
  expect(nav).toHaveTextContent('WelcomeYouWhat agents can doApprovalRecovery wordsReview')
  expect(nav.querySelector('[aria-current="step"]')).toHaveTextContent('Welcome')
})

test('the name is not prefilled and the preview follows the typed prefix', async () => {
  const f = fake()
  const fetcher: typeof fetch = async (url, init) => {
    const res = await f.fetcher(url, init)
    if (String(url).endsWith('/state')) {
      const body = await res.json()
      return new Response(JSON.stringify({ ...body, suggested: { name: '', workspace_name: 'Personal Workspace', prefix: 'TST' } }))
    }
    return res
  }
  render(<Setup api={new SetupAPI('tok', fetcher)} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  expect(screen.getByLabelText('Your name')).toHaveValue('')
  expect(screen.getByRole('button', { name: 'Continue →' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Change' }))
  fireEvent.change(screen.getByLabelText(/^Ticket prefix/), { target: { value: 'RA' } })
  expect(screen.getByLabelText('Ticket number preview')).toHaveTextContent('RA-0001')
  expect(screen.getByLabelText('Ticket number preview')).toHaveTextContent('RA-0003')
})

test('the prefix is suggested from the workspace name until the person edits it', async () => {
  const f = fake()
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.change(screen.getByLabelText('Your name'), { target: { value: 'Ann' } })
  fireEvent.click(screen.getByRole('button', { name: 'Change' }))
  const prefix = screen.getByLabelText(/^Ticket prefix/)
  fireEvent.change(screen.getByLabelText(/^Workspace name/), { target: { value: 'Other' } })
  fireEvent.change(screen.getByLabelText(/^Workspace name/), { target: { value: 'Personal Workspace' } })
  expect(prefix).toHaveValue('PW')
  expect(screen.getByLabelText('Ticket number preview')).toHaveTextContent('PW-0001')
  fireEvent.change(screen.getByLabelText(/^Workspace name/), { target: { value: 'Marketing' } })
  expect(prefix).toHaveValue('MARK')
  fireEvent.change(prefix, { target: { value: 'mx' } })
  expect(prefix).toHaveValue('MX')
  fireEvent.change(screen.getByLabelText(/^Workspace name/), { target: { value: 'Sales Team' } })
  expect(prefix).toHaveValue('MX') // edited by hand: no longer suggested
  fireEvent.change(prefix, { target: { value: 'M' } })
  expect(screen.getByRole('button', { name: 'Continue →' })).toBeDisabled()
})

test('a hand-edited workspace name keeps driving the prefix when the person name changes; the fallback is MW', async () => {
  const f = fake()
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByRole('button', { name: 'Change' }))
  fireEvent.change(screen.getByLabelText(/^Workspace name/), { target: { value: 'Marketing' } })
  fireEvent.change(screen.getByLabelText('Your name'), { target: { value: 'Zed Zebra' } })
  expect(screen.getByLabelText(/^Ticket prefix/)).toHaveValue('MARK')
  fireEvent.change(screen.getByLabelText(/^Workspace name/), { target: { value: '!!!' } })
  expect(screen.getByLabelText(/^Ticket prefix/)).toHaveValue('MW')
})

test('the ready list gives every status an icon and a word, never colour alone', async () => {
  const f = fake({ finish: true, running: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Finish setting up your workspace' })
  fireEvent.click(screen.getByLabelText(/Own copy per ticket/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
  const running = await screen.findByLabelText('Your way to approve: Running')
  expect(running).toHaveClass('status', 'doing')
  expect(running).toHaveTextContent('Running')
})

test('agent step: no raw capability codes, plain worktrees text, quick tasks has no switch, No agent access is the default', async () => {
  const f = fake()
  const { container } = render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  expect(page(container)).not.toMatch(/spawn_agent|pty/)
  expect(screen.getByText('Quick tasks').closest('.toggle')?.querySelector('input')).toBeNull()
  expect(screen.getByText('Always on')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  expect(screen.getByRole('button', { name: 'No agent access' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.queryByLabelText(/Let agents start on this computer now/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '1 day' }))
  expect(screen.getByRole('button', { name: 'No agent access' })).toHaveAttribute('aria-pressed', 'false')
  expect(screen.getByLabelText(/Let agents start on this computer now/)).not.toBeChecked()
  fireEvent.click(screen.getByRole('button', { name: 'No agent access' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  expect(f.calls.find((c) => c.path === 'profile')?.body).toMatchObject({ agent_hours: 8, agent_grant: false })
})

test('the "If setup fails" warning shows once, after the words, and not again on the review', async () => {
  const { container } = render(<Setup api={fake().api} />)
  await reveal()
  expect(container.querySelectorAll('.warning')).toHaveLength(1)
  const words = container.querySelector('.recovery-print')!
  expect(words.compareDocumentPosition(container.querySelector('.warning')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(page(container)).not.toContain('If setup fails')
})

test('P11: step 4 has no text input, no checkbox and no word check; Save as file downloads the words in the browser', async () => {
  const f = fake()
  const created: Blob[] = []
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  Object.assign(URL, {
    createObjectURL: (b: Blob) => (created.push(b), 'blob:demo'),
    revokeObjectURL: () => {},
  })
  const { container } = render(<Setup api={f.api} />)
  await reveal()
  expect(container.querySelectorAll('input,textarea')).toHaveLength(0)
  expect(screen.queryByRole('checkbox')).toBeNull()
  expect(screen.queryByText(/Tap word|One quick check|tries|attempts/i)).toBeNull()
  const before = f.calls.length
  fireEvent.click(screen.getByRole('button', { name: 'Save as file' }))
  expect(click).toHaveBeenCalledOnce()
  expect(created).toHaveLength(1)
  expect(await created[0].text()).toContain('24. demo-word-24')
  expect(f.calls.length).toBe(before) // the words never go back to the server
  expect(JSON.stringify(f.calls)).not.toContain('demo-word-')
  expect(screen.getByRole('button', { name: 'I saved my words — continue →' })).toBeEnabled()
})

test('the progress words are shown, and the unfinished items are named in words', async () => {
  const f = fake({ failure: true, finish: true })
  const fetcher: typeof fetch = async (url, init) => {
    const res = await f.fetcher(url, init)
    if (String(url).includes('/progress')) {
      const body = await res.json()
      return new Response(JSON.stringify({ ...body, retry: ['runner_grant', 'custody'], error: { ...body.error, message: 'one-click could not be set up because this computer refused; your workspace uses the terminal for now.' } }))
    }
    return res
  }
  const { container } = render(<Setup api={new SetupAPI('tok', fetcher)} />)
  await screen.findByRole('heading', { name: 'Finish setting up your workspace' })
  fireEvent.click(screen.getByLabelText(/Own copy per ticket/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
  await screen.findByRole('heading', { name: 'Setup needs your attention' })
  expect(page(container)).toContain('one-click could not be set up because')
  expect(page(container)).toContain("Still to finish: Agent access for this computer's runner, One-click approval on this computer")
})

test('when the terminal setup ends the page notices within 2 seconds, clears the words and says paused', async () => {
  const f = fake()
  let calls = 0
  const fetcher: typeof fetch = async (url, init) => {
    if (String(url).endsWith('/state') && ++calls > 1)
      return new Response(JSON.stringify({ error: { code: 'setup.unavailable' } }), { status: 503 })
    return f.fetcher(url, init)
  }
  const { container } = render(<Setup api={new SetupAPI('tok', fetcher)} />)
  await reveal()
  expect(container.querySelectorAll('.recovery li')).toHaveLength(24)
  await screen.findByRole('heading', { name: 'Setup paused', level: 1 }, { timeout: 2000 })
  expect(container.querySelectorAll('.recovery li')).toHaveLength(0)
  expect(container.textContent).not.toContain('demo-word-')
  expect(container.textContent).not.toContain('demo-not-a-real-passphrase')
  expect(screen.getByRole('alert')).toHaveTextContent('The setup in your terminal has ended')
})

function withReview(patch: Partial<Review>) {
  const f = fake()
  const fetcher: typeof fetch = async (url, init) => {
    const res = await f.fetcher(url, init)
    if (String(url).endsWith('/review')) return new Response(JSON.stringify({ ...(await res.json()), ...patch }))
    return res
  }
  return { f, api: new SetupAPI('tok', fetcher) }
}

test('the summary comes from the review core returned, not from the choices on the page', async () => {
  // The page chose 3 days and one click; core's review says 1 day and a terminal flag: the summary says what core says.
  const { api } = withReview({ flags: [{ id: 'terminal', text: 'Every approval asks for your passphrase in a terminal.' }] })
  render(<Setup api={api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '3 days' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  const summary = document.querySelector('.review-summary')!
  expect(summary).toHaveTextContent('may work for 1 day')
  expect(summary).not.toHaveTextContent('3 days')
  expect(summary).toHaveTextContent('In a terminal, with your password')
})

test('P06: the git flag is no row on the short card (no Also row either); its signed text and the git words are in Details', async () => {
  const { api } = withReview({
    flags: [{ id: 'git', text: 'Keep a history of changes in this folder. Your files stay as they are.', detail: 'This runs git init, keeps .orch/ and .orch-work/ out of the repository, and makes one empty first commit.' }],
  })
  render(<Setup api={api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  const summary = document.querySelector('.review-summary')!
  expect(summary).not.toHaveTextContent(/Own copy per ticket|Also|git init|\.orch/)
  const fold = document.querySelector('.review-technical') as HTMLDetailsElement
  expect(fold.open).toBe(false)
  expect(fold).toHaveTextContent('Keep a history of changes in this folder. Your files stay as they are.')
  expect(fold).toHaveTextContent('git init')
})

test('nothing about agents or approval is claimed when the review has none of it', async () => {
  const { api } = withReview({ items: [], flags: [] })
  render(<Setup api={api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '7 days' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  const summary = document.querySelector('.review-summary')!
  expect(summary).not.toHaveTextContent(/7 days|terminal|one click|Agents may/)
})

test('a review item of a kind this page does not know is named in the summary and the details stay closed', async () => {
  const { api } = withReview({
    items: [
      { id: 'mystery', title: 'Something new to sign', lines: ['Do the new thing'], details: [], volatile: [], type: 'thing.done', fields: {}, placeholders: [], placeholder_details: [] },
    ],
  })
  render(<Setup api={api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(document.querySelector('.review-summary')).toHaveTextContent('Something new to sign')
  expect(screen.getByText('Details').closest('details')).not.toHaveAttribute('open')
  expect(screen.getByText('Do the new thing')).toBeInTheDocument()
})

test('a settings change this page does not know is named, not summarised away', async () => {
  const { api } = withReview({
    items: [
      { id: 'settings', title: 'Workspace settings', lines: ['Change lease time'], details: [], volatile: [], type: 'settings.changed', fields: { set: { lease_ttl_min: 5 } }, placeholders: [], placeholder_details: [] },
    ],
  })
  render(<Setup api={api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(document.querySelector('.review-summary')).toHaveTextContent('Workspace settings')
})

test('only the lines core flags as placeholders are hidden; a line that merely ends in "set at signing" stays', async () => {
  const { api } = withReview({
    items: [
      {
        id: 'runner_grant',
        title: 'Agent access held by this machine',
        lines: ['Give agents access', 'from: set at signing', 'label: set at signing', '', 'grant id: set at signing'],
        details: ['expires_at: set at signing', 'note: set at signing'],
        volatile: [],
        type: 'grant.issued',
        fields: {},
        placeholders: [1, 4],
        placeholder_details: [0],
      },
    ],
    runner_grant: { hours: 24, scope: 'workable', holder: "this machine's runner, never shown" },
  })
  render(<Setup api={api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  const details = document.querySelector('.review-technical')!
  expect(details).toHaveTextContent('label: set at signing')
  expect(details).toHaveTextContent('note: set at signing')
  expect(details).not.toHaveTextContent('from: set at signing')
  expect(details).not.toHaveTextContent('grant id: set at signing')
  expect(details).not.toHaveTextContent('expires_at: set at signing')
})

test('an unrecognised flag is shown in the summary and the details stay closed', async () => {
  const { api } = withReview({ flags: [{ id: 'surprise', text: 'Something else happens too.' }] })
  render(<Setup api={api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(document.querySelector('.review-summary')).toHaveTextContent('Something else happens too.')
  expect(screen.getByText('Details').closest('details')).not.toHaveAttribute('open')
})

test('the runner line comes from the signed grant item, not from review.runner_grant', async () => {
  const grant = { id: 'runner_grant', title: 'Agent access held by this machine', lines: ['Give agents access'], details: [], volatile: [], type: 'grant.issued', placeholders: [], placeholder_details: [] }
  const { api } = withReview({
    items: [{ ...grant, fields: { type: 'grant.issued', hours: 24, scope: 'workable', verbs: 'agent' } }],
    runner_grant: { hours: 168, scope: 'everything', holder: 'x' },
  })
  render(<Setup api={api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  const summary = document.querySelector('.review-summary')!
  expect(summary).toHaveTextContent('may work for 1 day')
  expect(summary).not.toHaveTextContent(/7 days|everything/)
})

test.each([
  ['a grant item with another id', { id: 'other', type: 'grant.issued', fields: { type: 'grant.issued', hours: 24, scope: 'a', verbs: 'b' } }],
  ['a grant item without hours', { id: 'runner_grant', type: 'grant.issued', fields: { type: 'grant.issued' } }],
  ['a settings payload with extra keys', { id: 'settings', type: 'settings.changed', fields: { type: 'settings.changed', set: { grant_hours: 24 }, extra: 1 } }],
  ['a non-number grant_hours', { id: 'settings', type: 'settings.changed', fields: { type: 'settings.changed', set: { grant_hours: '24' } } }],
])('%s is named and the details stay closed', async (_name, patch) => {
  const { api } = withReview({
    items: [{ title: 'Odd item', lines: ['x'], details: [], volatile: [], placeholders: [], placeholder_details: [], ...patch } as Review['items'][number]],
  })
  render(<Setup api={api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(document.querySelector('.review-summary')).toHaveTextContent('Odd item')
  expect(screen.getByText('Details').closest('details')).not.toHaveAttribute('open')
})

test('finish offers no runner grant when the workspace agent time is below the shortest choice', async () => {
  const f = fake({ finish: true })
  const fetcher: typeof fetch = async (url, init) => {
    const res = await f.fetcher(url, init)
    if (String(url).endsWith('/state')) {
      const body = await res.json()
      return new Response(JSON.stringify({ ...body, current: { ...body.current, agent_hours: 4 } }))
    }
    return res
  }
  render(<Setup api={new SetupAPI('tok', fetcher)} />)
  await screen.findByRole('heading', { name: 'Finish setting up your workspace' })
  expect(screen.queryByLabelText(/Let agents start on this computer now/)).toBeNull()
})

test('N7: the review names the workspace from what core returned, not from the typed values', async () => {
  const f = fake()
  const { container } = render(<Setup api={f.api} />)
  await reveal()
  // the page's own profile says Demo/TST; the review shows what core's review holds
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  const box = container.querySelector('.review-facts')!
  expect(box).toHaveTextContent('Marketing')
  expect(box).not.toHaveTextContent('RA-0001') // r11-P15: the ticket prefix is in Details, not on the card
  expect(box).not.toHaveTextContent('orch-run2-a') // P06: the folder name is in Details, not on the card
  expect(box).not.toHaveTextContent('/demo/')
  expect(box.textContent).not.toContain('Demo')
})

test('N9: finish on a complete workspace says Setup is complete and opens the dashboard', async () => {
  const f = fake({ finish: true, complete: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Setup is complete' })
  expect(screen.queryByRole('button', { name: 'Continue →' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Finish setup later' })).toBeNull()
  expect(screen.queryByText(/paused/i)).toBeNull()
  expect(screen.getByRole('button', { name: 'Open dashboard →' })).toBeEnabled()
})

test('P06: a complete workspace shows one sentence and Open dashboard; the table sits behind What is set up', async () => {
  const f = fake({ finish: true, complete: true })
  const { container } = render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Setup is complete' })
  expect(page(container)).toContain('is ready.')
  // no welcome text, no repeats, no short code, no "Nothing is missing."
  expect(page(container)).not.toMatch(/to-do list of tickets|We will set this up|Everything stays on this computer|Nothing is missing|Short name/)
  const fold = screen.getByText('What is set up').closest('details')!
  expect(fold).not.toHaveAttribute('open')
  expect(fold).toHaveTextContent('Add-ons')
  expect(fold).not.toHaveTextContent('Features')
  expect(fold).toHaveTextContent('Agents can start here')
  expect(fold).not.toHaveTextContent('Short name')
  expect(fold).not.toHaveTextContent('Add-ons on')
})

test('P01: an unreachable dashboard address keeps the page, shows only the address and no navigation', async () => {
  const assign = vi.fn()
  vi.stubGlobal('location', { origin: 'http://127.0.0.1:47821', assign })
  vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
  const f = fake({ finish: true, done: true, finishUrl: 'http://127.0.0.1:8151/#launch=' + 'a'.repeat(30) + '&to=%2F' })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Finish setting up your workspace' })
  fireEvent.click(screen.getByLabelText(/Own copy per ticket/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
  await screen.findByRole('heading', { name: 'Your workspace is ready' })
  fireEvent.click(await screen.findByRole('button', { name: 'Open your workspace →' }))
  const box = await screen.findByTestId('unreachable')
  expect(box).toHaveTextContent('It runs on the computer where you ran setup.')
  expect(box).toHaveTextContent('http://127.0.0.1:8151/')
  expect(document.body.textContent).not.toContain('launch=') // the single-use login link is never shown
  expect(box).toHaveTextContent('Copy address')
  expect(assign).not.toHaveBeenCalled()
  // Try again asks the same address once more; when it answers the page goes there
  vi.stubGlobal('fetch', vi.fn(async () => new Response(null)))
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
  await vi.waitFor(() => expect(assign).toHaveBeenCalledTimes(1))
  expect(assign.mock.calls[0][0]).toMatch(/^http:\/\/127\.0\.0\.1:8151\/#launch=/)
  vi.unstubAllGlobals()
})

test('P01 (Opus): answers() is true for an answering address, false when nothing listens, and true (fail open) when the page CSP blocked the probe', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(null)))
  expect(await answers('http://127.0.0.1:8151')).toBe(true)
  vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
  expect(await answers('http://127.0.0.1:8151')).toBe(false)
  // the browser rejects the fetch AND queues a securitypolicyviolation event: the answer is unknown, so it opens as before
  vi.stubGlobal('fetch', vi.fn(async () => {
    setTimeout(() => document.dispatchEvent(new Event('securitypolicyviolation')), 0)
    throw new TypeError('Failed to fetch')
  }))
  expect(await answers('http://192.168.1.5:8151')).toBe(true)
  vi.unstubAllGlobals()
})

test('P01: a reachable dashboard address is opened as before', async () => {
  const assign = vi.fn()
  vi.stubGlobal('location', { origin: 'http://127.0.0.1:47821', assign })
  vi.stubGlobal('fetch', vi.fn(async () => new Response(null)))
  const f = fake({ finish: true, done: true, finishUrl: 'http://127.0.0.1:8151/#launch=' + 'a'.repeat(30) + '&to=%2F' })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Finish setting up your workspace' })
  fireEvent.click(screen.getByLabelText(/Own copy per ticket/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Open your workspace →' }))
  await vi.waitFor(() => expect(assign).toHaveBeenCalledTimes(1))
  expect(screen.queryByTestId('unreachable')).toBeNull()
  vi.unstubAllGlobals()
})

test('N8: leaving the page after ready tells the setup it is finished', async () => {
  const f = fake({ done: true })
  render(<Setup api={f.api} />)
  await reveal()
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await screen.findByRole('heading', { name: 'Your workspace is ready' })
  const before = f.calls.filter((c) => c.path === 'finish').length
  window.dispatchEvent(new Event('pagehide'))
  expect(f.calls.filter((c) => c.path === 'finish').length).toBe(before + 1)
})

test('N14: a failed step is shown with its reason in full', async () => {
  const msg = 'one-click could not be set up because macOS did not let orch use the keychain (it is locked); unlock it and run orch setup again'
  const f = fake({ failure: true })
  const orig = f.fetcher.getMockImplementation()!
  f.fetcher.mockImplementation(async (u, i) => {
    const r = await orig(u, i)
    if (String(u).includes('progress')) {
      const b = await r.json()
      return new Response(JSON.stringify({ ...b, error: { ...b.error, message: msg } }))
    }
    return r
  })
  const { container } = render(<Setup api={f.api} />)
  await reveal()
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await waitFor(() => expect(container.textContent).toContain(msg))
})

test('F7: an unusual runner grant keeps its raw scope and verbs in the summary line', () => {
  const grant = (scope: string, verbs: string) => ({
    items: [{ id: 'runner_grant', type: 'grant.issued', title: 'x', fields: { type: 'grant.issued', hours: 72, scope, verbs } }],
    flags: [],
  }) as unknown as Parameters<typeof summarize>[0]
  expect(summarize(grant('workable', 'agent')).lines).toEqual(['This computer’s agent runner may work on any ticket you can see, as you, for 3 days'])
  expect(summarize(grant('all', 'agent')).lines).toEqual(['This computer’s agent runner gets agent access for 3 days (scope all, verbs agent)'])
  expect(summarize(grant('workable', 'task.done')).lines[0]).toContain('verbs task.done')
})

test('U1: a new setup starts with the main add-ons and agent access ticked, all still visible and untickable', async () => {
  const f = fake({ defaults: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  for (let i = 0; i < 2; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  const sw = screen.getByRole('switch', { name: /Own copy per ticket/ })
  expect(sw).toBeChecked()
  expect(sw).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  expect(screen.getByRole('button', { name: '3 days' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByLabelText(/Let agents start on this computer now/)).toBeChecked()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(f.calls.find((c) => c.path === 'profile')?.body).toMatchObject({
    addons: expect.arrayContaining(['worktrees']),
    agent_grant: true,
    agent_hours: 72,
  })
})

test.each([
  ['hours without a grant', { agent_hours: 72, agent_grant: false }],
  ['hours outside the offered list', { agent_hours: 48, agent_grant: true }],
])('suggested %s: picker and profile agree on no agent access', async (_n, suggested) => {
  const f = fake({ suggested })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  for (let i = 0; i < 3; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  expect(screen.getByRole('button', { name: 'No agent access' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByRole('button', { name: '3 days' })).toHaveAttribute('aria-pressed', 'false')
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(f.calls.find((c) => c.path === 'profile')?.body).toMatchObject({ agent_grant: false, agent_hours: 8 })
})

test('a folder without git: one switch, one grey line with Details under the row, no second question, git_init true sent', async () => {
  const f = fake({ defaults: true, noGit: true })
  const { container } = render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  for (let i = 0; i < 2; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  const note = screen.getByTestId('git-note')
  expect(note).toHaveTextContent('We will set your project folder up for this. Your files stay as they are.')
  // directly under the switch row
  expect(note.previousElementSibling).toContainElement(screen.getByRole('switch', { name: /Own copy per ticket/ }))
  expect(screen.queryByText(/not set up for its own copy per ticket yet/)).not.toBeInTheDocument()
  expect(screen.queryByLabelText(/Set it up for me/)).not.toBeInTheDocument()
  expect(screen.queryByLabelText(/Skip this/)).not.toBeInTheDocument()
  expect(container.querySelectorAll('input[name=git]')).toHaveLength(0)
  const fold = within(note).getByText('Details').closest('details') as HTMLDetailsElement
  expect(fold.open).toBe(false)
  expect(fold).toHaveTextContent('.gitignore')
  expect(screen.queryByText(/git init/i)).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Continue →' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  const sent = f.calls.find((c) => c.path === 'profile')?.body as { addons: string[]; git_init: boolean }
  expect(sent.git_init).toBe(true)
  expect(sent.addons).toContain('worktrees')
})

test('switching the work-copies switch off drops worktrees, hides the line and sends git_init false', async () => {
  const f = fake({ defaults: true, noGit: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  for (let i = 0; i < 2; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByRole('switch', { name: /Own copy per ticket/ }))
  expect(screen.queryByTestId('git-note')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  const sent = f.calls.find((c) => c.path === 'profile')?.body as { addons: string[]; git_init: boolean }
  expect(sent.addons).not.toContain('worktrees')
  expect(sent.git_init).toBe(false)
})

test('after the terminal confirms, the words are replaced by a matched notice', async () => {
  const f = fake({ typed: true, running: true })
  render(<Setup api={f.api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '3 days' }))
  fireEvent.click(screen.getByLabelText(/Let agents start on this computer now/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  expect(await screen.findByText('The three words matched. Your choices are confirmed.')).toBeInTheDocument()
  expect(screen.queryByRole('list', { name: 'Your three words' })).toBeNull()
})

test('reviewFacts builds the short card in plain words from the review', () => {
  const review = {
    review_id: 'r', digest: 'd', fingerprint: null, tty_confirm: false, risk: null, runner_grant: null,
    workspace: { prefix: 'MD', name: 'Severin', workspace_name: 'Marketing', dir: '/p', lines: [] },
    flags: [{ id: 'custody', text: 'x' }, { id: 'records', text: 'y' }],
    items: [
      { id: 'runner_grant', title: 'Agent access', lines: [], details: [], volatile: [], type: 'grant.issued', fields: { hours: 72, scope: 'workable', verbs: 'agent' } },
      { id: 'addon:dashboard', title: 'Dashboard', lines: [], details: [], volatile: [], type: 'addon.granted', fields: {} },
    ],
  } as unknown as Review
  const facts = reviewFacts(review)
  expect(facts.map((f) => f.label)).toEqual(['Workspace', 'Agents', 'How you approve'])
  expect(facts.find((f) => f.label === 'Workspace')!.value).toBe('Marketing') // r11-P15: no ticket prefix on the card
  expect(facts.find((f) => f.label === 'Agents')!.value).toBe('may work for 3 days')
  expect(facts.find((f) => f.label === 'Add-ons')).toBeUndefined()
  // P06: owner, folder, own copy and history are Details, not decisions
  for (const gone of ['Owner', 'Your project', 'Own copy per ticket', 'History']) expect(facts.find((f) => f.label === gone)).toBeUndefined()
})


test('progress groups roll up done and failed steps without changing server steps', () => {
  const steps = [
    { id: 'keys', label: 'Keys', status: 'done' as const },
    { id: 'genesis', label: 'Sign', status: 'done' as const },
    { id: 'custody', label: 'Custody', status: 'skipped' as const },
    { id: 'settings', label: 'Settings', status: 'done' as const },
    { id: 'register', label: 'Host', status: 'done' as const },
    { id: 'runner', label: 'Runner', status: 'failed' as const },
    { id: 'addon:dashboard', label: 'Dashboard', status: 'done' as const },
    { id: 'flags', label: 'Flags', status: 'done' as const },
  ]
  expect(groupProgress(steps)).toEqual([
    { id: 'key', label: 'Your way to approve', status: 'done' },
    { id: 'workspace', label: 'Your workspace', status: 'done' },
    { id: 'services', label: 'Starting orch', status: 'failed' },
    { id: 'addons', label: 'Add-ons', status: 'done' },
    { id: 'approval', label: 'Your choices saved', status: 'done' },
  ])
  expect(steps).toHaveLength(8)
  expect(steps[5].status).toBe('failed')
  expect(groupProgress([])).toEqual([])
})

test('the private-folders step is its own calm row (P08)', () => {
  expect(groupProgress([{ id: 'perms', label: 'x', status: 'done' as const }])).toEqual([
    { id: 'perms', label: 'Your files are private', status: 'done' },
  ])
})

test('P10: the review has no approval-count sentence, the fold is Details, and the progress says Starting orch', async () => {
  const f = fake({ done: true })
  const { container } = render(<Setup api={f.api} />)
  await reveal()
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(page(container)).not.toMatch(/items in the list above|\d+ items/)
  expect(screen.getByText('Details').tagName).toBe('SUMMARY')
  expect(screen.queryByText(/Everything you approve/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await screen.findByRole('heading', { name: 'Your workspace is ready' })
  expect(page(container)).not.toContain('Background services')
})

test('P10: the permissions page says Own copy per ticket, not Worktrees or the CLI', async () => {
  render(<Setup api={fake().api} />)
  await approval()
  expect(page(document.body as HTMLElement)).not.toMatch(/worktree|the CLI/i)
})

test('P17: the Welcome page says what Finish setup later does; later pages do not repeat it', async () => {
  render(<Setup api={fake().api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  expect(screen.getByText('Nothing is set up yet. Come back any time.')).toHaveClass('cancel-hint')
  expect(screen.getByRole('button', { name: 'Finish setup later' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'You' })
  expect(screen.queryByText(/Come back any time/)).toBeNull()
  expect(screen.getByRole('button', { name: 'Finish setup later' })).toBeInTheDocument()
})

test('P17: in finish mode the hint says the workspace keeps working', async () => {
  render(<Setup api={fake({ finish: true }).api} />)
  await screen.findByRole('heading', { name: 'Finish setting up your workspace' })
  expect(screen.getByText('Your workspace keeps working. Finish this any time.')).toBeVisible()
  expect(screen.queryByText(/Nothing is set up yet/)).toBeNull()
})

test.each([
  [['todo', 'done'], 'done'],
  [['todo', 'skipped'], 'todo'],
  [['skipped', 'skipped'], 'skipped'],
  [['doing', 'done'], 'doing'],
  [['doing', 'failed'], 'failed'],
] as const)('progress status precedence for %s', (statuses, expected) => {
  expect(groupProgress(statuses.map((status, i) => ({ id: ['keys', 'genesis'][i], label: '', status })))[0].status).toBe(expected)
})

test('r10-P13: the workspace name is a stated suggestion, never a field that changes on its own; Change opens it', async () => {
  const f = fake({ suggested: { name: '', workspace_name: 'My workspace', prefix: 'MW' } })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  // before a name is typed: no field and no "My workspace" anywhere
  expect(screen.queryByLabelText(/^Workspace name/)).toBeNull()
  expect(screen.queryByText(/My workspace/)).toBeNull()
  expect(screen.getByText(/named after you/)).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Your name'), { target: { value: 'Anna Test' } })
  const summary = screen.getByTestId('ws-summary')
  expect(summary).toHaveAttribute('aria-live', 'polite')
  expect(summary).toHaveTextContent("Your workspace: Anna Test's workspace")
  expect(summary).toHaveTextContent('AT-0001')
  expect(screen.queryByLabelText(/^Workspace name/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Change' }))
  expect(screen.getByLabelText(/^Workspace name/)).toHaveValue("Anna Test's workspace")
  expect(screen.getByLabelText(/^Ticket prefix/)).toHaveValue('AT')
  expect(screen.getByLabelText('Ticket number preview')).toHaveTextContent('AT-0001')
  fireEvent.change(screen.getByLabelText(/^Workspace name/), { target: { value: 'Sales' } })
  fireEvent.change(screen.getByLabelText('Your name'), { target: { value: 'Ben Test' } })
  expect(screen.getByLabelText(/^Workspace name/)).toHaveValue('Sales')
})

test('the recovery page uses one name, recovery words, and names no terminal command', async () => {
  const f = fake()
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.change(screen.getByLabelText('Your name'), { target: { value: 'Ann' } })
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  expect(screen.getByRole('button', { name: 'I saved my words — continue →' })).toBeEnabled()
  const text = document.body.textContent ?? ''
  expect(text).not.toMatch(/secret words|recovery code/i)
  expect(screen.getByText(/you get new words next time/)).not.toHaveTextContent('orch setup')
})

test.each([
  ['darwin', true],
  ['linux', false],
  [undefined, false],
])('the Background Items note shows only on a Mac (os %s)', async (os, shown) => {
  const f = fake({ running: true, os })
  const { container } = render(<Setup api={f.api} />)
  await reveal()
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await waitFor(() => expect(container.querySelector('ol.progress li')).not.toBeNull())
  expect(Boolean(screen.queryByText(/Your Mac may show a note/))).toBe(shown)
})

test('P15: no Recommended badge when One click is not available here', async () => {
  const f = fake({ clickOff: 'not on this computer' })
  render(<Setup api={f.api} />)
  await approval()
  expect(screen.queryByText('Recommended')).not.toBeInTheDocument()
  expect(screen.queryByTestId('click-risk')).not.toBeInTheDocument()
})

test('N2: a repos setting for the setup folder is quiet, any other project path is shown', async () => {
  const mk = (path: string) => ({
    items: [{ id: 'settings', type: 'settings.changed', title: 'Workspace settings', lines: [], details: [], volatile: [], fields: { type: 'settings.changed', set: { repos: { proj: { path } } } } }],
    flags: [{ id: 'git', text: 'Keep a history of changes in this folder. Your files stay as they are.' }],
  })
  for (const [path, shown] of [['/demo/orch-run2-a', false], ['/elsewhere/other', true]] as const) {
    const { api } = withReview(mk(path))
    render(<Setup api={api} />)
    await approval()
    fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
    await screen.findByRole('heading', { name: 'Your recovery words' })
    await check()
    await screen.findByRole('heading', { name: 'Review your setup' })
    const summary = document.querySelector('.review-summary')!
    expect(summary.textContent?.includes(`Project folder: ${path}`) ?? false).toBe(shown)
    cleanup()
  }
})

test('r11-P01: a Finish that cannot open the dashboard says so in plain words and Try again opens it', async () => {
  const f = fake({ finish: true, done: true })
  const orig = f.fetcher.getMockImplementation()!
  let finishes = 0
  f.fetcher.mockImplementation(async (u, i) => {
    if (String(u).endsWith('/finish')) {
      finishes += 1
      if (finishes === 1)
        return new Response(JSON.stringify({ error: { code: 'setup.dashboard_unavailable', message: 'x', retry: true } }), { status: 503 })
      if (finishes === 2) return new Response('Internal Server Error', { status: 500 })
    }
    return orig(u, i)
  })
  const { container } = render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Finish setting up your workspace' })
  fireEvent.click(screen.getByLabelText(/Own copy per ticket/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
  await screen.findByRole('heading', { name: 'Your workspace is ready' })
  const assign = vi.fn()
  vi.stubGlobal('location', { origin: location.origin, assign })
  vi.spyOn(console, 'error').mockImplementation(() => {})
  for (let n = 0; n < 2; n++) {
    fireEvent.click(screen.getByRole('button', { name: n === 0 ? 'Open your workspace →' : 'Try again' }))
    await screen.findByText('The dashboard did not open yet. Your workspace is safe. Press “Try again” to open it.')
    const text = page(container)
    expect(text).not.toMatch(/terminal/i)
    expect(screen.getByRole('alert').textContent).not.toMatch(/http|500/)
    expect(text).toContain('The page could not start the dashboard on this computer. Nothing was lost.')
    expect(text).toContain('If this keeps failing, run orch setup again in your project folder.')
  }
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
  await waitFor(() => expect(assign).toHaveBeenCalledWith('/'))
  expect(finishes).toBe(3)
})

test('r11-P15: the Mac notice shows while the steps run, never on the ready or the error screen', async () => {
  for (const [opts, shown] of [[{ running: true }, true], [{ done: true }, false], [{ failure: true }, false]] as const) {
    const f = fake({ ...opts, os: 'darwin' })
    const { container, unmount } = render(<Setup api={f.api} />)
    await reveal()
    await check()
    await screen.findByRole('heading', { name: 'Review your setup' })
    fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
    await waitFor(() => expect(container.querySelector('ol.progress li')).not.toBeNull())
    if (!shown) await waitFor(() => expect(page(container)).toMatch(/ready|attention|Failed/))
    expect(page(container).includes('Your Mac may show a note')).toBe(shown)
    unmount()
  }
})


test('r11-P16: the approval step shows the recommendation, a one-line summary and Continue; the rest is one click away', async () => {
  const f = fake({ suggested: { method: 'click' } })
  const { container } = render(<Setup api={f.api} />)
  await approval()
  const more = screen.getByTestId('approval-more') as HTMLDetailsElement
  expect(more.open).toBe(false)
  const radios = [...container.querySelectorAll('input[type=radio]')] as HTMLElement[]
  expect(radios.filter((r) => !r.closest('details:not([open])'))).toHaveLength(1)
  expect(screen.getByLabelText(/One click on this computer/)).toBeVisible()
  expect(screen.getByRole('button', { name: '1 day' })).not.toBeVisible()
  expect(screen.getByRole('switch', { name: /Keep a history/ })).not.toBeVisible()
  expect(screen.getByTestId('approval-summary')).toHaveTextContent('No agent works here until you give access. Your history stays on this computer.')
  // the summary follows the hidden choices
  fireEvent.click(screen.getByText('Change these settings'))
  fireEvent.click(screen.getByRole('button', { name: '3 days' }))
  expect(screen.getByTestId('approval-summary')).toHaveTextContent('Agents may work for 3 days; none start on this computer yet.')
  fireEvent.click(screen.getByRole('switch', { name: /Let agents start on this computer now/ }))
  expect(screen.getByTestId('approval-summary')).toHaveTextContent('Agents can work on their own for 3 days. Your history stays on this computer.')
  fireEvent.click(screen.getByRole('button', { name: '1 day' }))
  fireEvent.click(screen.getByRole('switch', { name: /Keep a history/ }))
  expect(screen.getByTestId('approval-summary')).toHaveTextContent('Agents can work on their own for 1 day. No history is kept.')
  fireEvent.click(screen.getByRole('button', { name: 'No agent access' }))
  expect(screen.getByTestId('approval-summary')).toHaveTextContent('No agent works here until you give access.')
})

test('r11-P16: the posted profile is the same whether or not the settings were opened', async () => {
  const bodies: unknown[] = []
  for (const open of [false, true]) {
    const f = fake({ suggested: { method: 'click' } })
    render(<Setup api={f.api} />)
    await approval()
    if (open) fireEvent.click(screen.getByText('Change these settings'))
    fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
    await screen.findByRole('heading', { name: 'Your recovery words' })
    bodies.push(f.calls.find((c) => c.path === 'profile')?.body)
    cleanup()
  }
  expect(bodies[0]).toEqual(bodies[1])
  expect(bodies[0]).toMatchObject({ method: 'click' })
})

test('r11-P16: without One click the settings are open, so the password choice is never hidden', async () => {
  render(<Setup api={fake({ clickOff: 'not on this computer' }).api} />)
  await approval()
  expect((screen.getByTestId('approval-more') as HTMLDetailsElement).open).toBe(true)
  expect(screen.getByLabelText(/Password/)).toBeVisible()
})

async function toAgents(f: ReturnType<typeof fake>) {
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  for (let i = 0; i < 2; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByTestId('agents-summary')
}

test('r12-P15: the agents step shows the recommendation, the switches wait behind "Change these settings"', async () => {
  const f = fake({ defaults: true })
  await toAgents(f)
  expect(screen.getByTestId('agents-summary')).toHaveTextContent('Recommended for most people')
  const more = screen.getByTestId('agents-more') as HTMLDetailsElement
  expect(more.open).toBe(false)
  expect(more).toHaveTextContent('Change these settings')
  expect(more).toContainElement(screen.getByRole('switch', { name: /Own copy per ticket/, hidden: true }))
  expect(screen.queryByText('Dashboard', { selector: 'strong' })).not.toBeInTheDocument()
})

test('r12-P15: the fold opens by itself when a switch is not at its default, or Own copy per ticket is switched off in a folder without git', async () => {
  const f = fake({ defaults: true })
  await toAgents(f)
  const more = screen.getByTestId('agents-more') as HTMLDetailsElement
  fireEvent.click(screen.getByLabelText(/Own copy per ticket/))
  expect(more.open).toBe(true)
  cleanup()
  const g = fake({ defaults: true, noGit: true })
  await toAgents(g)
  const m2 = screen.getByTestId('agents-more') as HTMLDetailsElement
  expect(m2.open).toBe(false)
  fireEvent.click(screen.getByLabelText(/Own copy per ticket/))
  expect(m2.open).toBe(true)
})

test('r12-P15: the settings sent are identical whether or not the fold was opened', async () => {
  const run = async () => {
    const f = fake({ defaults: true })
    await toAgents(f)
    return f
  }
  const a = await run()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  cleanup()
  const b = await run()
  const more = screen.getByTestId('agents-more') as HTMLDetailsElement
  more.open = true
  fireEvent(more, new Event('toggle'))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  const sent = (f: ReturnType<typeof fake>) => f.calls.find((c) => c.path === 'profile')?.body
  expect(sent(b)).toBeDefined()
  expect(sent(b)).toEqual(sent(a))
})

test('r14-P07: the welcome page says one plain sentence; the folder name and path sit behind Details', async () => {
  const f = fake()
  const { container } = render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  expect(screen.getByText('We will set this up in your project folder.')).toBeInTheDocument()
  expect(container).not.toHaveTextContent('Folder:')
  expect(container).not.toHaveTextContent('Technical details')
  const fold = screen.getByText('Details').closest('details') as HTMLDetailsElement
  expect(fold.open).toBe(false)
  expect(fold.textContent).toContain('/')
})

test('r12-P15: the recovery words say Not saved yet; Continue works; Copy turns into a tick (display only)', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  const f = fake()
  render(<Setup api={f.api} />)
  await reveal()
  expect(screen.getByRole('status')).toHaveTextContent('Not saved yet')
  expect(screen.getByRole('button', { name: 'I saved my words — continue →' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
  const copied = await screen.findByRole('button', { name: 'Copied ✓' })
  expect(copied).toHaveClass('done')
  expect(screen.getByRole('status')).toHaveTextContent('Recovery words copied')
  expect(screen.getByRole('status')).not.toHaveTextContent('Not saved yet')
  // display only: no call carries it, and the step still needs its own confirm
  expect(f.calls.some((c) => c.path === 'secrets/confirm')).toBe(false)
  expect(screen.getByRole('button', { name: 'I saved my words — continue →' })).toBeEnabled()
  window.dispatchEvent(new Event('afterprint'))
  expect(await screen.findByRole('button', { name: 'Printed ✓' })).toHaveClass('done')
})

test('r14-P07: the approval page uses everyday words: Password, AI helpers; no Claude Code, Passphrase or Your key', async () => {
  const f = fake({ suggested: { method: 'click' }, claudeAvailable: true })
  const { container } = render(<Setup api={f.api} />)
  await approval()
  fireEvent.click(screen.getByText('Change these settings'))
  expect(screen.getByLabelText(/Password/)).toBeInTheDocument()
  expect(screen.getByText(/Type your password each time/)).toBeInTheDocument()
  expect(screen.getByRole('switch', { name: /Let your AI helpers open this folder without asking/ })).toBeInTheDocument()
  expect(screen.getByText('They will not stop to ask first. This folder only holds orch’s files.')).toBeInTheDocument()
  expect(container.textContent).not.toMatch(/Claude Code|assphrase|Your key/)
})
