import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { Setup } from './Setup'
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
  ],
  methods: ['terminal', 'click'],
  click_available: true,
  click_reason: 'click_relay_unsupported: phone approvals need terminal signing',
  agent_hours: [8, 24, 72, 168],
  relay: { available: true, enabled: true },
  missing: [],
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
    stateError?: boolean
    failure?: boolean
    done?: boolean
    click?: boolean
    relayUnavailable?: boolean
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
        mode: options.finish ? 'finish' : 'new',
        missing: options.finish ? ['runner_grant'] : [],
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
        tty_confirm: !!options.finish,
        ...(options.agents ? { typed_word: 'AGENTS' } : {}),
        ...(options.typed
          ? {
              typed_word: 'GRANT',
              tty_confirm: true,
              runner_grant: { hours: 72, scope: 'tickets', holder: "this machine's runner, never shown" },
            }
          : {}),
      }
    if (path === 'finish') result = { url: '/' }
    if (path === 'create') result = { run_id: 'demo-run' }
    if (path.startsWith('progress'))
      result = {
        steps: [{ id: 'genesis', label: 'Create workspace', status: options.failure ? 'failed' : 'todo' }],
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

test('typed word gates long runner access; digest and fingerprint stay bound while waiting for terminal', async () => {
  const f = fake({ typed: true })
  render(<Setup api={f.api} />)
  await approval()
  fireEvent.click(screen.getByRole('button', { name: '3 days' }))
  fireEvent.click(screen.getByLabelText(/Give this computer/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your secret words' })
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  expect(screen.getByText('Agents may work for 3 days.')).toBeInTheDocument()
  expect(screen.getByText("this machine's runner, never shown")).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Create workspace' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText(/Type.*GRANT/), { target: { value: 'GRANT' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await screen.findByRole('heading', { name: 'Confirm in your terminal' })
  expect(screen.getByText('demo not real')).toBeInTheDocument()
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
  for (let i = 0; i < 4; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
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
  for (let i = 0; i < 4; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
  await screen.findByRole('heading', { name: 'Your workspace is ready' })
  expect(screen.getByRole('button', { name: 'Open your workspace →' })).toBeEnabled()
  const assign = vi.fn()
  vi.stubGlobal('location', { origin: location.origin, assign })
  fireEvent.click(screen.getByRole('button', { name: 'Open your workspace →' }))
  await waitFor(() => expect(assign).toHaveBeenCalledWith('/'))
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
    waiting === 'waiting for terminal' ? 'Confirm in your terminal' : 'Waiting for the agent runner to connect…')
})

function failing(status: number, code?: string) {
  const fetcher = vi.fn(
    async () => new Response(JSON.stringify(code ? { error: { code } } : {}), { status }),
  ) as unknown as typeof fetch
  return new SetupAPI('tok', fetcher)
}

test.each([
  [410, 'setup.ended'],
  [401, 'setup.token'],
])('initial state %i shows Reason: %s', async (status, code) => {
  const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
  render(<Setup api={failing(status, code)} />)
  expect(await screen.findByText(`Reason: ${code}`)).toBeInTheDocument()
  expect(screen.getByText(/This session has ended/)).toBeInTheDocument()
  expect(spy).toHaveBeenCalledWith('[orch setup]', code)
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
