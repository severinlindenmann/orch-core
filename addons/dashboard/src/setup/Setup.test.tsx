import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { plainReason, Setup, summarize } from './Setup'
import { SetupAPI, bootstrapSetup, isSetupPage, type SetupState, type Review } from '../api/setup'

const state: SetupState = {
  phase: 'profile',
  mode: 'new',
  dir: '/demo/project',
  dir_name: 'project',
  suggested: { name: 'Demo', prefix: 'TST' },
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
      title: 'Worktrees',
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
  labels: { runner_grant: "Agent access for this Mac's runner", custody: 'One-click approval on this Mac' },
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
  typed_word: null,
  tty_confirm: false,
}
function fake(
  options: {
    finish?: boolean
    attempts?: number
    typed?: boolean
    agents?: boolean
    waiting?: string
    running?: boolean
    stateError?: boolean
    failure?: boolean
    done?: boolean
    click?: boolean
    relayUnavailable?: boolean
    noGit?: boolean
    complete?: boolean
    defaults?: boolean
    suggested?: Record<string, unknown>
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
        ...(options.relayUnavailable ? { relay: { available: false, enabled: false } } : {}),
        ...(options.noGit ? { git: { repo: false } } : {}),
        mode: options.finish ? 'finish' : 'new',
        missing: options.finish && !options.complete ? ['addon:worktrees', 'runner_grant'] : [],
        ...(options.complete ? { complete: true } : {}),
        ...(options.defaults
          ? {
              suggested: { name: 'Demo', prefix: 'TST', agent_hours: 72, agent_grant: true },
              addons: state.addons.map((a) => (a.id === 'worktrees' ? { ...a, default: true } : a)),
            }
          : {}),
        ...(options.suggested ? { suggested: { name: 'Demo', prefix: 'TST', ...options.suggested } } : {}),
        ...(options.finish ? { current } : {}),
      }
    if (path === 'secrets/reveal')
      result = {
        passphrase: options.click ? null : 'demo-not-a-real-passphrase',
        recovery_words: Array.from({ length: 24 }, (_, i) => `demo-word-${i + 1}`),
        check_positions: [8, 17, 23],
      }
    if (path === 'secrets/confirm')
      result = options.attempts !== undefined ? { ok: false, attempts_left: options.attempts } : { ok: true }
    if (path === 'review')
      result = {
        ...review,
        ...(options.finish
          ? {}
          : {
              workspace: {
                prefix: 'RA',
                name: 'Severin',
                dir: '/demo/orch-run2-a',
                lines: ['Workspace RA for Severin', 'Short name: RA', 'Person: Severin', 'Folder: /demo/orch-run2-a'],
              },
            }),
        tty_confirm: !!options.finish,
        ...(options.agents ? { typed_word: 'AGENTS' } : {}),
        ...(options.typed
          ? {
              typed_word: 'GRANT',
              tty_confirm: true,
              runner_grant: { hours: 72, scope: 'tickets', holder: "this machine's runner, never shown" },
              items: [
                ...review.items,
                { id: 'runner_grant', title: 'Agent access held by this machine', lines: [], details: [], volatile: [], type: 'grant.issued', fields: { type: 'grant.issued', hours: 72, scope: 'workable', verbs: 'agent' }, placeholders: [], placeholder_details: [] },
              ],
            }
          : {}),
      }
    if (path === 'finish') result = { url: '/', workspace_id: 'ws-new-2' }
    if (path === 'create') result = { run_id: 'demo-run' }
    if (path.startsWith('progress'))
      result = {
        steps: [{ id: 'genesis', label: 'Create workspace', status: options.failure ? 'failed' : options.running ? 'doing' : 'todo' }],
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
  await screen.findByRole('heading', { name: 'Set up orch in project' })
  for (let i = 0; i < 3; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
}
async function reveal() {
  await approval()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
}
async function check() {
  fireEvent.click(screen.getByLabelText('I saved my secret words and recovery code'))
  for (const n of [8, 17, 23])
    fireEvent.change(screen.getByLabelText(`Word ${n}`), { target: { value: `demo-word-${n}` } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm saved words →' }))
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

test('shows secrets only as text, sends exactly three words, drops secrets after confirm', async () => {
  const f = fake()
  const { container } = render(<Setup api={f.api} />)
  await reveal()
  expect(container.querySelectorAll('.recovery li')).toHaveLength(24)
  expect(screen.getByText('demo-not-a-real-passphrase').tagName).toBe('P')
  expect(
    [...container.querySelectorAll('input,textarea')].every((el) => !(el as HTMLInputElement).value.includes('demo-')),
  ).toBe(true)
  expect(screen.queryByRole('button', { name: /Copy passphrase|PDF/ })).not.toBeInTheDocument()
  expect(screen.getByText(/If setup fails before/)).toBeInTheDocument()
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(container.textContent).not.toContain('demo-word-')
  expect(container.textContent).not.toContain('demo-not-a-real-passphrase')
  expect(f.calls.find((c) => c.path === 'secrets/confirm')?.body).toEqual({
    words: { 8: 'demo-word-8', 17: 'demo-word-17', 23: 'demo-word-23' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await waitFor(() =>
    expect(f.calls.find((c) => c.path === 'create')?.body).toEqual({ review_id: 'demo-review', digest: 'demo-digest' }),
  )
})

test('fifth failed check removes secrets and requires a new session and new secrets', async () => {
  const f = fake({ attempts: 0 })
  render(<Setup api={f.api} />)
  await reveal()
  await check()
  await screen.findByRole('heading', { name: 'Setup paused' })
  expect(screen.getByRole('alert')).toHaveTextContent('new session with new secret words and a new recovery code')
  expect(screen.queryByText('demo-not-a-real-passphrase')).not.toBeInTheDocument()
  expect(f.calls.some((c) => c.path === 'review')).toBe(false)
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
  expect(screen.getByText(/Relay is unavailable with One click/)).toHaveTextContent('click_relay_unsupported')
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  expect(screen.queryByRole('heading', { name: 'Your passphrase' })).not.toBeInTheDocument()
  expect(f.calls.find((c) => c.path === 'profile')?.body).toMatchObject({ method: 'click', relay: false })
})

test('run 8: One click is the default where core suggests it, with the risk sentence before Continue; Terminal stays selectable', async () => {
  const f = fake({ suggested: { method: 'click' } })
  render(<Setup api={f.api} />)
  await approval()
  expect(screen.getByLabelText(/One click on this computer/)).toBeChecked()
  expect(screen.getByTestId('click-risk')).toHaveTextContent('Convenience signing: demo risk sentence.')
  fireEvent.click(screen.getByLabelText(/Terminal/))
  expect(screen.queryByTestId('click-risk')).not.toBeInTheDocument()
  fireEvent.click(screen.getByLabelText(/One click on this computer/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  expect(f.calls.find((c) => c.path === 'profile')?.body).toMatchObject({ method: 'click' })
})

test('typed word gates long runner access; digest and fingerprint stay bound while waiting for terminal', async () => {
  const f = fake({ typed: true })
  render(<Setup api={f.api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '3 days' }))
  fireEvent.click(screen.getByLabelText(/Let this Mac’s agent runner/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(screen.getByText('This Mac’s agent runner may work on any ticket you can see, as you, for 3 days')).toBeInTheDocument()
  expect(screen.queryByText(/scope workable/)).toBeNull() // the raw values are only in the technical details
  expect(screen.getByText(/look at your terminal: orch asks you to confirm there/)).toHaveTextContent('demo not real, type yes there.')
  // everything signed stays inspectable, collapsed in one disclosure
  expect(screen.getByText('Show technical details').closest('details')).not.toHaveAttribute('open')
  expect(screen.getByText("this machine's runner, never shown")).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Create workspace' })).toBeDisabled()
  // B11: the terminal asks only after Create; the review says so instead of "confirm in your terminal" now
  expect(screen.getByRole('heading', { name: 'How to finish' })).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText(/Type.*GRANT/), { target: { value: 'GRANT' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await screen.findByRole('heading', { name: 'Confirm in your terminal' })
  // three large tiles, in order; the browser draws no imitation of the terminal
  expect(within(screen.getByRole('list', { name: 'Your three words' })).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['demo', 'not', 'real'])
  expect(screen.getByText(/Continue in the setup window on this computer/)).toBeInTheDocument()
  expect(screen.getByText(/type yes in that window and press Enter/)).toBeInTheDocument()
  expect(screen.getByText('Waiting for you in the setup window…')).toBeInTheDocument()
  expect(screen.getByText('The words don’t match').closest('details')).not.toHaveAttribute('open')
  expect(f.calls.find((c) => c.path === 'create')?.body).toEqual({
    review_id: 'demo-review',
    digest: 'demo-digest',
    typed_word: 'GRANT',
  })
})

test('finish skips secrets; progress exposes failure and Finish setup later', async () => {
  const f = fake({ finish: true, failure: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Finish setup in project' })
  fireEvent.click(screen.getByLabelText(/Worktrees/))
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
  fireEvent.click(screen.getByRole('button', { name: 'Copy recovery code' }))
  await Promise.resolve()
  expect(writeText.mock.calls[0][0]).toContain('demo-word-24')
  expect(writeText.mock.calls[0][0]).not.toContain('passphrase')
  await vi.advanceTimersByTimeAsync(60_000)
  expect(writeText).toHaveBeenLastCalledWith('')
  fireEvent.click(screen.getByRole('button', { name: 'Print recovery code' }))
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

test('long access cannot be submitted when core omits its required typed word', async () => {
  const f = fake()
  render(<Setup api={f.api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '7 days' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(screen.getByRole('button', { name: 'Create workspace' })).toBeDisabled()
  expect(screen.getByRole('alert')).toHaveTextContent('typed confirmation is required')
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

test('successful progress offers the workspace, while finish mode never reveals secrets', async () => {
  const f = fake({ finish: true, done: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Finish setup in project' })
  fireEvent.click(screen.getByLabelText(/Worktrees/))
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

test('AGENTS gates extended access without a runner grant', async () => {
  const f = fake({ agents: true })
  render(<Setup api={f.api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '1 day' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(screen.getByRole('button', { name: 'Create workspace' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText(/Type.*AGENTS/), { target: { value: 'AGENTS' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await waitFor(() => expect(f.calls.find(c => c.path === 'create')?.body).toMatchObject({ typed_word: 'AGENTS' }))
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
  expect(plainReason('session.required')).toMatch(/no longer has its link to your terminal/)
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

test('review is a compact plain summary; blanks and "set at signing" lines stay out of the details', async () => {
  const f = fake({ typed: true })
  render(<Setup api={f.api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '3 days' }))
  fireEvent.click(screen.getByLabelText(/Let this Mac’s agent runner/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(screen.getByText('You are about to approve')).toBeInTheDocument()
  expect(screen.queryByText('""')).toBeNull()
  expect(screen.queryByText(/: set at signing/)).toBeNull()
  expect(screen.getByText('Times and IDs are filled in when you sign.')).toBeInTheDocument()
})

const page = (container: HTMLElement) => container.textContent ?? ''

test('finish shows the current values read-only and offers only what is missing, never a downgrade', async () => {
  const f = fake({ finish: true })
  const { container } = render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Finish setup in project' })
  expect(page(container)).toContain('Agents may work for3 days')
  expect(screen.getByText('Already set up')).toBeInTheDocument()
  // the name and the short name are not inputs, the add-on that is on is not a switch
  expect(screen.queryByLabelText('Your name')).toBeNull()
  expect(screen.getAllByRole('switch').map((el) => el.closest('label')?.textContent ?? '')).toEqual([
    expect.stringContaining('Worktrees'),
    expect.stringContaining('Agent access for this Mac'),
    expect.stringContaining('Approve with one click'),
  ])
  expect(screen.getByRole('button', { name: 'Continue →' })).toBeDisabled()
  fireEvent.click(screen.getByLabelText(/Worktrees/))
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
  await screen.findByRole('heading', { name: 'Finish setup in project' })
  expect(page(container)).toContain('Agent access for this Mac’s runner')
  expect(page(container)).not.toMatch(/runner_grant|custody|addon:/)
})

test('the steps have visible names next to the dots', async () => {
  render(<Setup api={fake().api} />)
  await screen.findByRole('heading', { name: 'Set up orch in project' })
  const nav = screen.getByRole('navigation', { name: 'Setup progress' })
  expect(nav).toHaveTextContent('WelcomeYouAgents mayApprovalSecret wordsReview')
  expect(nav.querySelector('[aria-current="step"]')).toHaveTextContent('Welcome')
})

test('the name is not prefilled and the hint follows the typed short name', async () => {
  const f = fake()
  const fetcher: typeof fetch = async (url, init) => {
    const res = await f.fetcher(url, init)
    if (String(url).endsWith('/state')) {
      const body = await res.json()
      return new Response(JSON.stringify({ ...body, suggested: { name: '', prefix: 'TST' } }))
    }
    return res
  }
  render(<Setup api={new SetupAPI('tok', fetcher)} />)
  await screen.findByRole('heading', { name: 'Set up orch in project' })
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  expect(screen.getByLabelText('Your name')).toHaveValue('')
  expect(screen.getByRole('button', { name: 'Continue →' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Workspace short name'), { target: { value: 'RA' } })
  expect(screen.getByText('Used in ticket names, like RA-0001.')).toBeInTheDocument()
})

test('agent step: no raw capability codes, plain worktrees text, quick tasks has no switch, No agent access is the default', async () => {
  const f = fake()
  const { container } = render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up orch in project' })
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  expect(page(container)).not.toMatch(/spawn_agent|pty/)
  expect(screen.getByText('Quick tasks').closest('.toggle')?.querySelector('input')).toBeNull()
  expect(screen.getByText('Always on')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  expect(screen.getByRole('button', { name: 'No agent access' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.queryByLabelText(/agent runner/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '1 day' }))
  expect(screen.getByRole('button', { name: 'No agent access' })).toHaveAttribute('aria-pressed', 'false')
  expect(screen.getByLabelText(/Let this Mac’s agent runner/)).not.toBeChecked()
  fireEvent.click(screen.getByRole('button', { name: 'No agent access' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
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

test('a wrong attempt keeps the right entries and clears only the wrong ones', async () => {
  const f = fake({ attempts: 3 })
  render(<Setup api={f.api} />)
  await reveal()
  fireEvent.click(screen.getByLabelText('I saved my secret words and recovery code'))
  fireEvent.change(screen.getByLabelText('Word 8'), { target: { value: 'demo-word-8' } })
  fireEvent.change(screen.getByLabelText('Word 17'), { target: { value: 'wrong' } })
  fireEvent.change(screen.getByLabelText('Word 23'), { target: { value: 'demo-word-23' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm saved words →' }))
  await screen.findByText(/did not match. 3 attempts left/)
  expect(screen.getByLabelText('Word 8')).toHaveValue('demo-word-8')
  expect(screen.getByLabelText('Word 17')).toHaveValue('')
  expect(screen.getByLabelText('Word 23')).toHaveValue('demo-word-23')
})

test('the progress words are shown, and the unfinished items are named in words', async () => {
  const f = fake({ failure: true, finish: true })
  const fetcher: typeof fetch = async (url, init) => {
    const res = await f.fetcher(url, init)
    if (String(url).includes('/progress')) {
      const body = await res.json()
      return new Response(JSON.stringify({ ...body, retry: ['runner_grant', 'custody'], error: { ...body.error, message: 'one-click could not be set up because this Mac refused; your workspace uses the terminal for now.' } }))
    }
    return res
  }
  const { container } = render(<Setup api={new SetupAPI('tok', fetcher)} />)
  await screen.findByRole('heading', { name: 'Finish setup in project' })
  fireEvent.click(screen.getByLabelText(/Worktrees/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
  await screen.findByRole('heading', { name: 'Setup needs your attention' })
  expect(page(container)).toContain('one-click could not be set up because')
  expect(page(container)).toContain("Still to finish: Agent access for this Mac's runner, One-click approval on this Mac")
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
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  const summary = document.querySelector('.review-summary')!
  expect(summary).toHaveTextContent('Agents may work for you for up to 1 day')
  expect(summary).not.toHaveTextContent('3 days')
  expect(summary).toHaveTextContent('Every approval asks for your passphrase in a terminal.')
})

test('nothing about agents or approval is claimed when the review has none of it', async () => {
  const { api } = withReview({ items: [], flags: [] })
  render(<Setup api={api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '7 days' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  const summary = document.querySelector('.review-summary')!
  expect(summary).not.toHaveTextContent(/7 days|terminal|one click|Agents may/)
})

test('a review item of a kind this page does not know is named in the summary and opens the details', async () => {
  const { api } = withReview({
    items: [
      { id: 'mystery', title: 'Something new to sign', lines: ['Do the new thing'], details: [], volatile: [], type: 'thing.done', fields: {}, placeholders: [], placeholder_details: [] },
    ],
  })
  render(<Setup api={api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(document.querySelector('.review-summary')).toHaveTextContent('Something new to sign')
  expect(screen.getByText('Show technical details').closest('details')).toHaveAttribute('open')
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
  await screen.findByRole('heading', { name: 'Your secret words' })
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
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  const details = document.querySelector('.review-technical')!
  expect(details).toHaveTextContent('label: set at signing')
  expect(details).toHaveTextContent('note: set at signing')
  expect(details).not.toHaveTextContent('from: set at signing')
  expect(details).not.toHaveTextContent('grant id: set at signing')
  expect(details).not.toHaveTextContent('expires_at: set at signing')
})

test('an unrecognised flag is shown in the summary and opens the details', async () => {
  const { api } = withReview({ flags: [{ id: 'surprise', text: 'Something else happens too.' }] })
  render(<Setup api={api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(document.querySelector('.review-summary')).toHaveTextContent('Something else happens too.')
  expect(screen.getByText('Show technical details').closest('details')).toHaveAttribute('open')
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
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  const summary = document.querySelector('.review-summary')!
  expect(summary).toHaveTextContent('may work on any ticket you can see, as you, for 1 day')
  expect(summary).not.toHaveTextContent(/7 days|everything/)
})

test.each([
  ['a grant item with another id', { id: 'other', type: 'grant.issued', fields: { type: 'grant.issued', hours: 24, scope: 'a', verbs: 'b' } }],
  ['a grant item without hours', { id: 'runner_grant', type: 'grant.issued', fields: { type: 'grant.issued' } }],
  ['a settings payload with extra keys', { id: 'settings', type: 'settings.changed', fields: { type: 'settings.changed', set: { grant_hours: 24 }, extra: 1 } }],
  ['a non-number grant_hours', { id: 'settings', type: 'settings.changed', fields: { type: 'settings.changed', set: { grant_hours: '24' } } }],
])('%s is named and opens the details', async (_name, patch) => {
  const { api } = withReview({
    items: [{ title: 'Odd item', lines: ['x'], details: [], volatile: [], placeholders: [], placeholder_details: [], ...patch } as Review['items'][number]],
  })
  render(<Setup api={api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(document.querySelector('.review-summary')).toHaveTextContent('Odd item')
  expect(screen.getByText('Show technical details').closest('details')).toHaveAttribute('open')
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
  await screen.findByRole('heading', { name: 'Finish setup in project' })
  expect(screen.queryByLabelText(/Agent access for this Mac.s runner/)).toBeNull()
})

test('N7: the review names the workspace from what core returned, not from the typed values', async () => {
  const f = fake()
  const { container } = render(<Setup api={f.api} />)
  await reveal()
  // the page's own profile says Demo/TST; the review shows what core's review holds
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  const box = container.querySelector('.review-workspace')!
  expect(box).toHaveTextContent('Workspace RA for Severin')
  expect(box).toHaveTextContent('Short name')
  expect(box).toHaveTextContent('/demo/orch-run2-a')
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
  expect(summarize(grant('workable', 'agent')).lines).toEqual(['This Mac’s agent runner may work on any ticket you can see, as you, for 3 days'])
  expect(summarize(grant('all', 'agent')).lines).toEqual(['This Mac’s agent runner gets agent access for 3 days (scope all, verbs agent)'])
  expect(summarize(grant('workable', 'task.done')).lines[0]).toContain('verbs task.done')
})

test('U1: a new setup starts with the main add-ons and agent access ticked, all still visible and untickable', async () => {
  const f = fake({ defaults: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up orch in project' })
  for (let i = 0; i < 2; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  const sw = screen.getByRole('switch', { name: /Worktrees/ })
  expect(sw).toBeChecked()
  expect(sw).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  expect(screen.getByRole('button', { name: '3 days' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByLabelText(/Let this Mac’s agent runner/)).toBeChecked()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
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
  await screen.findByRole('heading', { name: 'Set up orch in project' })
  for (let i = 0; i < 3; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  expect(screen.getByRole('button', { name: 'No agent access' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByRole('button', { name: '3 days' })).toHaveAttribute('aria-pressed', 'false')
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(f.calls.find((c) => c.path === 'profile')?.body).toMatchObject({ agent_grant: false, agent_hours: 8 })
})

test('a folder without git offers to make one when Worktrees is on, and never commits anything', async () => {
  const f = fake({ defaults: true, noGit: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up orch in project' })
  for (let i = 0; i < 2; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  expect(screen.getByText(/This folder is not a git repository/)).toBeInTheDocument()
  expect(screen.getByText(/None of your files is committed/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /Choose above to continue/ })).toBeDisabled()
  fireEvent.click(screen.getByLabelText(/Make it a git repository/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  expect(f.calls.find((c) => c.path === 'profile')?.body).toMatchObject({ git_init: true })
})

test('choosing to continue without worktrees drops them and asks no git question', async () => {
  const f = fake({ defaults: true, noGit: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up orch in project' })
  for (let i = 0; i < 2; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByLabelText(/Continue without worktrees/))
  expect(screen.queryByText(/This folder is not a git repository/)).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  const sent = f.calls.find((c) => c.path === 'profile')?.body as { addons: string[]; git_init: boolean }
  expect(sent.addons).not.toContain('worktrees')
  expect(sent.git_init).toBe(false)
})

test('after the terminal confirms, the words are replaced by a matched notice', async () => {
  const f = fake({ typed: true, running: true })
  render(<Setup api={f.api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '3 days' }))
  fireEvent.click(screen.getByLabelText(/Let this Mac’s agent runner/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.change(screen.getByLabelText(/Type.*GRANT/), { target: { value: 'GRANT' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  expect(await screen.findByText('The three words matched. Your choices are confirmed.')).toBeInTheDocument()
  expect(screen.queryByRole('list', { name: 'Your three words' })).toBeNull()
})
