import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { choicesFor, groupProgress, plainReason, Setup, reviewFacts, summarize } from './Setup'
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
      title: 'Work copies',
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
    attempts?: number
    typed?: boolean
    noTty?: boolean
    waiting?: string
    running?: boolean
    stateError?: boolean
    failure?: boolean
    done?: boolean
    click?: boolean
    clickOff?: string
    relayUnavailable?: boolean
    noGit?: boolean
    complete?: boolean
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
        check_positions: [17],
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
  await screen.findByRole('button', { name: 'Continue →' })
  for (let i = 0; i < 3; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
}
async function reveal() {
  await approval()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
}
async function check() {
  fireEvent.click(screen.getByLabelText('I saved my recovery words'))
  for (const n of [17]) fireEvent.click(within(screen.getByRole('group', { name: `Word ${n}` })).getByRole('button', { name: `demo-word-${n}` }))
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

test('shows secrets only as text, asks for one word, drops secrets after confirm', async () => {
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
  expect(f.calls.find((c) => c.path === 'secrets/confirm')?.body).toEqual({ words: { 17: 'demo-word-17' } })
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
  expect(screen.getByRole('alert')).toHaveTextContent('new session with new recovery words')
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
  expect(screen.getByText(/Phone approvals are not available with One click yet/)).toBeInTheDocument()
  expect(screen.queryByText(/click_relay_unsupported/)).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  expect(screen.queryByRole('heading', { name: 'Your passphrase' })).not.toBeInTheDocument()
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
  expect(screen.getByText(/Safest if others use this computer/)).toBeVisible()
  const risk = screen.getByTestId('click-risk') as HTMLDetailsElement
  expect(risk.open).toBe(false) // the longer meaning is one click away, not in the main view
  expect(risk).toHaveTextContent('Anything running as you on this computer, including your agents, could approve for you. Choose a passphrase if the computer is shared.')
  fireEvent.click(screen.getByLabelText(/Passphrase/))
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
  expect(screen.getByText(/^Everything you approve \(/).closest('details')).not.toHaveAttribute('open')
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
  fireEvent.click(screen.getByLabelText(/Work copies/))
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
  fireEvent.click(screen.getByRole('button', { name: 'Copy recovery words' }))
  await Promise.resolve()
  expect(writeText.mock.calls[0][0]).toContain('demo-word-24')
  expect(writeText.mock.calls[0][0]).not.toContain('passphrase')
  await vi.advanceTimersByTimeAsync(60_000)
  expect(writeText).toHaveBeenLastCalledWith('')
  fireEvent.click(screen.getByRole('button', { name: 'Print recovery words' }))
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

test('successful progress offers the workspace, while finish mode never reveals secrets', async () => {
  const f = fake({ finish: true, done: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Finish setting up your workspace' })
  fireEvent.click(screen.getByLabelText(/Work copies/))
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
  // the name and the short name are not inputs, the add-on that is on is not a switch
  expect(screen.queryByLabelText('Your name')).toBeNull()
  expect(screen.getAllByRole('switch').map((el) => el.closest('label')?.textContent ?? '')).toEqual([
    expect.stringContaining('Work copies'),
    expect.stringContaining('Let agents start on this computer now'),
    expect.stringContaining('Approve with one click'),
  ])
  expect(screen.getByRole('button', { name: 'Continue →' })).toBeDisabled()
  fireEvent.click(screen.getByLabelText(/Work copies/))
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
  expect(page(container)).toContain('Agent access for this computer’s runner')
  expect(page(container)).not.toMatch(/runner_grant|custody|addon:/)
})

test('the steps have visible names next to the dots', async () => {
  render(<Setup api={fake().api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  const nav = screen.getByRole('navigation', { name: 'Setup progress' })
  expect(nav).toHaveTextContent('WelcomeYouWhat agents can doApprovalCheck wordsReview')
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
  fireEvent.click(screen.getByLabelText(/Work copies/))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
  const running = await screen.findByLabelText('Your key: Running')
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

test('a wrong attempt clears the wrong word and shows the attempts left', async () => {
  const f = fake({ attempts: 3 })
  render(<Setup api={f.api} />)
  await reveal()
  fireEvent.click(screen.getByLabelText('I saved my recovery words'))
  const group = within(screen.getByRole('group', { name: 'Word 17' }))
  expect(group.getAllByRole('button')).toHaveLength(4)
  expect(group.getByRole('button', { name: 'demo-word-17' })).toBeInTheDocument()
  expect(document.querySelectorAll('.recovery li.check')).toHaveLength(1)
  expect(document.querySelector('.recovery li.check')).toHaveTextContent('17')
  expect(screen.getByRole('heading', { name: 'Tap word 17 to confirm' })).toBeInTheDocument()
  const decoy = group.getAllByRole('button').find((b) => b.textContent !== 'demo-word-17')!
  fireEvent.click(decoy)
  expect(decoy).toHaveAttribute('aria-pressed', 'true')
  fireEvent.click(screen.getByRole('button', { name: 'Confirm saved words →' }))
  await screen.findByText(/not the right word.*highlighted number.*3 tries left/)
  expect(group.getByRole('button', { name: decoy.textContent! })).toHaveAttribute('aria-pressed', 'false')
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
  fireEvent.click(screen.getByLabelText(/Work copies/))
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
  expect(summary).toHaveTextContent('In a terminal, with your passphrase')
})

test('the git flag is the Safe copies row, no Also row; its signed text and the git words are in Everything you approve', async () => {
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
  expect(summary).toHaveTextContent('Safe copiesEach ticket gets its own safe copy of this folder. None of your files are changed, except .gitignore.')
  expect(summary).not.toHaveTextContent(/Also|git init|\.orch/)
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
  expect(screen.getByText(/^Everything you approve \(/).closest('details')).not.toHaveAttribute('open')
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
  expect(screen.getByText(/^Everything you approve \(/).closest('details')).not.toHaveAttribute('open')
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
  expect(screen.getByText(/^Everything you approve \(/).closest('details')).not.toHaveAttribute('open')
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
  expect(box).toHaveTextContent('RA-0001')
  expect(box).toHaveTextContent('orch-run2-a')
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
  const sw = screen.getByRole('switch', { name: /Work copies/ })
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

test('a folder without git offers to make one when Work copies is on, and never commits anything', async () => {
  const f = fake({ defaults: true, noGit: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  for (let i = 0; i < 2; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  expect(screen.getByText(/This folder does not track changes yet/)).toBeInTheDocument()
  expect(screen.getByText(/None of your files are changed, except .gitignore/)).toBeInTheDocument()
  expect(screen.queryByText(/git init/i)).not.toBeInTheDocument()
  expect(screen.getByLabelText(/Set it up for me/)).toBeChecked()
  expect(screen.getByRole('button', { name: 'Continue →' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  await screen.findByRole('heading', { name: 'Your recovery words' })
  expect(f.calls.find((c) => c.path === 'profile')?.body).toMatchObject({ git_init: true })
  expect(screen.getAllByLabelText(/^Word \d+$/)).toHaveLength(1)
  expect(screen.getByRole('heading', { name: 'Tap word 17 to confirm' })).toBeInTheDocument()
})

test('choosing to continue without worktrees drops them and asks no git question', async () => {
  const f = fake({ defaults: true, noGit: true })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  for (let i = 0; i < 2; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  fireEvent.click(screen.getByLabelText(/Skip this/))
  expect(screen.queryByText(/This folder does not track changes yet/)).not.toBeInTheDocument()
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
  expect(facts.map((f) => f.label)).toEqual(['Workspace', 'Owner', 'Project folder', 'How you approve', 'Agents', 'History'])
  expect(facts.find((f) => f.label === 'Agents')!.value).toBe('may work for 3 days')
  expect(facts.find((f) => f.label === 'Add-ons')).toBeUndefined()
  expect(facts.find((f) => f.label === 'Project folder')!.value).toBe('p')
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
    { id: 'key', label: 'Your key', status: 'done' },
    { id: 'workspace', label: 'Your workspace', status: 'done' },
    { id: 'services', label: 'Background services', status: 'failed' },
    { id: 'addons', label: 'Add-ons', status: 'done' },
    { id: 'approval', label: 'Approval', status: 'done' },
  ])
  expect(steps).toHaveLength(8)
  expect(steps[5].status).toBe('failed')
  expect(groupProgress([])).toEqual([])
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

test('the workspace name and prefix follow "Your name" until edited by hand', async () => {
  const f = fake({ suggested: { name: '', workspace_name: 'My workspace', prefix: 'MW' } })
  render(<Setup api={f.api} />)
  await screen.findByRole('heading', { name: 'Set up your workspace' })
  fireEvent.click(screen.getByRole('button', { name: 'Continue →' }))
  expect(screen.getByLabelText(/^Workspace name/)).toHaveValue('My workspace')
  fireEvent.change(screen.getByLabelText('Your name'), { target: { value: 'Anna Test' } })
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
  expect(screen.getByLabelText('I saved my recovery words')).toBeInTheDocument()
  const text = document.body.textContent ?? ''
  expect(text).not.toMatch(/secret words|recovery code/i)
  expect(screen.getByText(/these words will no longer work/)).not.toHaveTextContent('orch setup')
})

test.each([
  ['darwin', true],
  ['linux', false],
  [undefined, false],
])('the Background Items note shows only on a Mac (os %s)', async (os, shown) => {
  const f = fake({ done: true, os })
  render(<Setup api={f.api} />)
  await reveal()
  await check()
  await screen.findByRole('heading', { name: 'Review your setup' })
  fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  await screen.findByRole('heading', { name: 'Your workspace is ready' })
  expect(Boolean(screen.queryByText(/Background Items Added/))).toBe(shown)
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

test('a word check with fewer than four distinct words offers fewer buttons and still holds the right word once', () => {
  for (const words of [['red', 'blue', 'red', 'red'], ['red', 'red', 'red'], ['red', 'blue', 'green', 'blue', 'red']]) {
    for (let n = 1; n <= words.length; n++) {
      const c = choicesFor(n, words)
      expect(c).toContain(words[n - 1])
      expect(new Set(c).size).toBe(c.length)
      expect(c.length).toBe(Math.min(4, new Set(words).size))
    }
  }
  expect(choicesFor(1, ['red', 'red'])).toEqual(['red'])
})
