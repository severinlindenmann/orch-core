// The real-host dock (M7a): New terminal in one press, the idem kept on Try again, Stop with incomplete cleanup, and
// "Output no longer kept" without attaching.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setFeatures } from '@/api/features'
import { runsApi, type Run, type RunList, type TermPreview } from '@/api/runs'
import { ApiError } from '@/api/types'
import { TooltipProvider } from '@/components/ui/tooltip'
import { RealDock } from './RealDock'

const WS = 'a'.repeat(32)
vi.mock('../../workspace', () => ({ useWorkspace: () => ({ workspace: { id: WS } }) }))
const dock = vi.hoisted(() => ({ ticket: undefined as string | undefined }))
vi.mock('../dock/context', async (orig) => ({ ...(await orig<typeof import('../dock/context')>()), useDockTicket: () => dock.ticket }))
// The terminal itself is covered in RealTerminal.test.tsx: here only which run it is mounted for, and how.
// Like the real one, it reads `initial` once, when it mounts.
vi.mock('./RealTerminal', async () => {
  const { useState } = await vi.importActual<typeof import('react')>('react')
  return { RealTerminal: function Term({ run, initial }: { run: Run; initial: string }) { const [first] = useState(initial); return <div data-testid="term" data-run={run.run} data-initial={first} /> } }
})

const run = (id: string, over: Partial<Run> = {}): Run => ({
  run: `r_01M4Y46B485AXD4CBEJZ15Z1${id}`, rev: 1, kind: 'shell', ticket: null, harness: 'shell', mode: null, model: null, as_person: true,
  state: 'running', started_at: '2026-10-14T10:00:00Z', viewers: 0, writer: false, cols: 120, rows: 40, replay_until: null,
  label: null, label_hidden: false, cwd: { kind: 'workspace' }, exit: null, ...over,
})
const preview = (over: Partial<TermPreview> = {}): TermPreview => ({
  spec: 'f'.repeat(64), display: '$SHELL -l', display_hidden: false, cwd_path: '/home/demo/billing‮', cwd_path_hidden: false,
  notice: 'acts as you, not as an agent', as_person: true, login_shell: false, env: ['PATH', 'ORCH_TICKET'], blocked: null, warnings: [], ...over,
})

let list: RunList
const props = {
  prefs: { open: true, side: 'bottom', bottom: 320, right: 480, harness: 'shell' } as never,
  side: 'bottom' as const, size: 320, view: { width: 1440, height: 900 }, area: 1200, rightFits: true,
  setPrefs: vi.fn(), focus: { current: null }, memory: new Map(), collapse: vi.fn(),
}
function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><TooltipProvider><RealDock {...props} /></TooltipProvider></QueryClientProvider>)
}

beforeEach(() => {
  list = { runs: [], runner: { connected: true, enabled: true, terminals: true }, truncated: false }
  vi.spyOn(runsApi, 'list').mockImplementation(async () => list)
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); setFeatures(null); dock.ticket = undefined })

describe('the real dock', () => {
  it('New terminal opens in one press (no confirm dialog), keeps its idem and spec on Try again, and attaches with the lease', async () => {
    const asked = vi.spyOn(runsApi, 'preview').mockResolvedValue(preview())
    const opened = run('AA')
    const open = vi.spyOn(runsApi, 'open')
      .mockRejectedValueOnce(new ApiError(503, { code: 'outcome.unknown', message: 'The host did not say whether it opened.', retryable: true }))
      .mockImplementation(async () => { list = { ...list, runs: [opened] }; return { run: opened } })
    mount()
    await screen.findByText('No terminal is open. Start one with New terminal.')
    fireEvent.pointerDown(screen.getByRole('button', { name: 'New terminal' }), { button: 0, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Shell in the workspace' }))
    await waitFor(() => expect(open).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('dialog')).toBeNull() // no "Open a terminal?" confirm
    expect(open.mock.calls[0][1]).toEqual({})
    expect(open.mock.calls[0][2]).toBe('f'.repeat(64)) // the spec of the host's preview
    expect(await screen.findByRole('alert')).toHaveTextContent('The host did not say whether it opened.')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(open).toHaveBeenCalledTimes(2))
    expect(open.mock.calls[0][3]).toMatch(/^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$/)
    expect(open.mock.calls[1][3]).toBe(open.mock.calls[0][3]) // the retry keeps the idem: one run, never two
    expect(open.mock.calls[1][2]).toBe(open.mock.calls[0][2]) // and the spec it was shown
    expect(asked).toHaveBeenCalledTimes(1)
    const term = await screen.findByTestId('term')
    expect(term.dataset.run).toBe(opened.run)
    expect(term.dataset.initial).toBe('write') // the shell this tab just opened: its first attach takes control (B12A N5)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('a second New terminal is a new idem: only Try again reuses one', async () => {
    vi.spyOn(runsApi, 'preview').mockResolvedValue(preview())
    const open = vi.spyOn(runsApi, 'open').mockImplementation(async () => { const r = run(`A${open.mock.calls.length}`); list = { ...list, runs: [...list.runs, r] }; return { run: r } })
    mount()
    await screen.findByText(/No terminal is open/)
    for (let i = 1; i <= 2; i++) {
      fireEvent.pointerDown(screen.getByRole('button', { name: 'New terminal' }), { button: 0, pointerType: 'mouse' })
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Shell in the workspace' }))
      await waitFor(() => expect(open).toHaveBeenCalledTimes(i))
    }
    expect(open.mock.calls[1][3]).not.toBe(open.mock.calls[0][3])
  })

  it('the write attach after Open is one-shot: switching away and back attaches read (review M1)', async () => {
    vi.spyOn(runsApi, 'preview').mockResolvedValue(preview())
    const other = run('BB')
    const opened = run('AA')
    list = { ...list, runs: [other] }
    vi.spyOn(runsApi, 'open').mockImplementation(async () => { list = { ...list, runs: [opened, other] }; return { run: opened } })
    mount()
    await screen.findByTestId('term')
    fireEvent.pointerDown(screen.getByRole('button', { name: 'New terminal' }), { button: 0, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Shell in the workspace' }))
    await waitFor(() => expect(screen.getByTestId('term').dataset.run).toBe(opened.run))
    expect(screen.getByTestId('term').dataset.initial).toBe('write')
    fireEvent.click(screen.getAllByRole('tab')[1])
    await waitFor(() => expect(screen.getByTestId('term').dataset.run).toBe(other.run))
    fireEvent.click(screen.getAllByRole('tab')[0])
    await waitFor(() => expect(screen.getByTestId('term').dataset.run).toBe(opened.run))
    expect(screen.getByTestId('term').dataset.initial).toBe('read')
  })

  it('a blocked preview opens nothing, says why, and offers no Try again', async () => {
    vi.spyOn(runsApi, 'preview').mockResolvedValue(preview({ blocked: { code: 'runner.unavailable', message: 'The runner is not running.' } }))
    const open = vi.spyOn(runsApi, 'open')
    mount()
    await screen.findByText(/No terminal is open/)
    fireEvent.pointerDown(screen.getByRole('button', { name: 'New terminal' }), { button: 0, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Shell in the workspace' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The runner is not running.')
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(open).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('an existing run attaches read-only; Stop with incomplete cleanup says processes may still run', async () => {
    list = { ...list, runs: [run('BB')] }
    vi.spyOn(runsApi, 'stop').mockResolvedValue({ state: 'exited', cleanup: 'incomplete' })
    mount()
    expect((await screen.findByTestId('term')).dataset.initial).toBe('read')
    fireEvent.click(screen.getByRole('button', { name: 'Stop Shell' }))
    expect(await screen.findByText('Some processes may still be running.')).toBeInTheDocument()
  })

  it('an ended run whose output is gone is not attached', async () => {
    list = { ...list, runs: [run('CC', { state: 'exited', replay_until: '2026-01-01T00:00:00Z', exit: { code: 0, signal: null, at: '2026-01-01T00:00:00Z', reason: 'exited', cleanup: 'complete' } })] }
    mount()
    expect(await screen.findByText('Output no longer kept')).toBeInTheDocument()
    expect(screen.queryByTestId('term')).toBeNull()
  })

  it('ended terminals can be cleared one by one or all at once; running ones stay', async () => {
    const ended = { state: 'exited' as const, replay_until: '2099-01-01T00:00:00Z', exit: { code: 0, signal: null, at: '2026-01-01T00:00:00Z', reason: 'exited' as const, cleanup: 'complete' as const } }
    list = { ...list, runs: [run('E1', { ...ended, ticket: 'RA-0001' }), run('E2', { ...ended, ticket: 'RA-0002' }), run('R1', { label: 'Live one' })] }
    mount()
    await screen.findByRole('tab', { name: /Live one/ })
    fireEvent.click(screen.getByRole('button', { name: 'Clear RA-0001 Shell' }))
    expect(screen.queryByRole('tab', { name: /RA-0001/ })).toBeNull()
    expect(screen.getByRole('tab', { name: /RA-0002/ })).toBeInTheDocument()
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Dock menu' }), { button: 0, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Clear ended terminals' }))
    expect(screen.queryByRole('tab', { name: /RA-0002/ })).toBeNull()
    expect(screen.getByRole('tab', { name: /Live one/ })).toBeInTheDocument()
  })

  it('U6: a start that never spawned leaves no tab behind (the dialog already showed the error)', async () => {
    const failed = { state: 'exited' as const, replay_until: null, exit: { code: null, signal: null, at: '2026-01-01T00:00:00Z', reason: 'spawn_failed' as const, cleanup: null } }
    list = { ...list, runs: [run('F1', { ...failed, ticket: 'RA-0009', kind: 'agent' }), run('R1', { label: 'Live one' })] }
    mount()
    await screen.findByRole('tab', { name: /Live one/ })
    expect(screen.queryByRole('tab', { name: /RA-0009/ })).toBeNull()
  })

  it('MED-1: a spawn_failed run whose cleanup is in doubt keeps its tab and the warning', async () => {
    const doubtful = { state: 'exited' as const, replay_until: '2099-01-01T00:00:00Z', exit: { code: null, signal: null, at: '2026-01-01T00:00:00Z', reason: 'spawn_failed' as const, cleanup: 'incomplete' as const } }
    list = { ...list, runs: [run('F2', { ...doubtful, ticket: 'RA-0010', kind: 'agent' })] }
    mount()
    expect(await screen.findByRole('tab', { name: /RA-0010/ })).toBeInTheDocument()
    expect(await screen.findByText(/Some processes may still be running/)).toBeInTheDocument()
  })

  it('Start agent is not available when the host does not serve agent_start', async () => {
    setFeatures({ features: ['terminals', 'terminals_page'], workspaces: { [WS]: ['terminals', 'terminals_page'] } })
    mount()
    await screen.findByText(/No terminal is open/)
    fireEvent.pointerDown(screen.getByRole('button', { name: 'New terminal' }), { button: 0, pointerType: 'mouse' })
    expect(await screen.findByRole('menuitem', { name: /Start agent/ })).toHaveAttribute('aria-disabled', 'true')
  })

  it('Start agent needs a ticket: outside a ticket page it is disabled and says so', async () => {
    setFeatures({ features: ['terminals', 'terminals_page', 'agent_start'], workspaces: { [WS]: ['terminals', 'terminals_page', 'agent_start'] } })
    mount()
    await screen.findByText(/No terminal is open/)
    fireEvent.pointerDown(screen.getByRole('button', { name: 'New terminal' }), { button: 0, pointerType: 'mouse' })
    const item = await screen.findByRole('menuitem', { name: /Start agent/ })
    expect(item).toHaveAttribute('aria-disabled', 'true')
    expect(item).toHaveTextContent('open a ticket first')
  })

  it('Start agent opens core\'s dialog on a ticket page when the host serves agent_start', async () => {
    dock.ticket = 'DEMO-1'
    const { agentsApi } = await import('@/api/agents')
    setFeatures({ features: ['terminals', 'terminals_page', 'agent_start'], workspaces: { [WS]: ['terminals', 'terminals_page', 'agent_start'] } })
    const pre = vi.spyOn(agentsApi, 'preview').mockResolvedValue({ spec: 'f'.repeat(64), ticket: null, display: 'codex', display_hidden: false, cwd: { kind: 'workspace' },
      cwd_path: '/w', cwd_path_hidden: false, will_create_worktree: false, login_shell: false, env: [], grant: null, blocked: null, warnings: [] })
    vi.spyOn(agentsApi, 'runner').mockResolvedValue({ runner: { connected: true, enabled: true, terminals: true, grant: null } })
    mount()
    await screen.findByText(/No terminal is open/)
    fireEvent.pointerDown(screen.getByRole('button', { name: 'New terminal' }), { button: 0, pointerType: 'mouse' })
    const item = await screen.findByRole('menuitem', { name: 'Start agent on DEMO-1' })
    expect(item).not.toHaveAttribute('aria-disabled')
    fireEvent.click(item)
    expect(await screen.findByTestId('agent-preview')).toBeInTheDocument()
    expect(pre).toHaveBeenCalledWith(WS, expect.objectContaining({ ticket: 'DEMO-1' }))
  })

  it('the dock menu also offers "Change agent or model…" next to Start agent, on a ticket page only', async () => {
    dock.ticket = 'DEMO-1'
    const { agentsApi } = await import('@/api/agents')
    setFeatures({ features: ['terminals', 'terminals_page', 'agent_start'], workspaces: { [WS]: ['terminals', 'terminals_page', 'agent_start'] } })
    vi.spyOn(agentsApi, 'preview').mockResolvedValue({ spec: 'f'.repeat(64), ticket: null, display: 'codex', display_hidden: false, cwd: { kind: 'workspace' },
      cwd_path: '/w', cwd_path_hidden: false, will_create_worktree: false, login_shell: false, env: [], grant: null, blocked: null, warnings: [] })
    vi.spyOn(agentsApi, 'runner').mockResolvedValue({ runner: { connected: true, enabled: true, terminals: true, grant: null } })
    mount()
    await screen.findByText(/No terminal is open/)
    fireEvent.pointerDown(screen.getByRole('button', { name: 'New terminal' }), { button: 0, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Change agent or model…' }))
    expect(await screen.findByTestId('agent-preview')).toBeInTheDocument()
  })

  it('M-6: with the Terminals add-on off the dock lists no shells and does not offer a new one', async () => {
    setFeatures({ features: ['terminals'], workspaces: { [WS]: ['terminals'] } })
    list = { ...list, runs: [run('SH', { state: 'exited', exit: { code: 0, reason: 'exited', cleanup: 'complete' } as never })] }
    mount()
    expect(await screen.findByText(/Terminals are switched off in this workspace/)).toBeInTheDocument()
    expect(screen.queryByRole('tab')).toBeNull()
    fireEvent.pointerDown(screen.getByRole('button', { name: 'New terminal' }), { button: 0, pointerType: 'mouse' })
    expect(await screen.findByRole('menuitem', { name: /Shell in the workspace/ })).toHaveAttribute('aria-disabled', 'true')
  })

  it('a very long tab name stays on one line, with the full name as its tooltip', async () => {
    const long = 'a-very-long-terminal-name '.repeat(8).trim()
    list = { ...list, runs: [run('LN', { label: long })] }
    mount()
    const tab = await screen.findByRole('tab')
    const name = tab.querySelector('span.truncate') as HTMLElement
    expect(name.title).toBe(long)
    expect(name.className).toContain('[&>bdi]:whitespace-nowrap')
  })

  it('an agent whose runner grant ended says so and keeps Stop', async () => {
    list = { ...list, runs: [run('AG', { kind: 'agent', harness: 'codex', as_person: false, grant_state: 'ended' })] }
    mount()
    expect(await screen.findByText(/runner grant ended/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Stop/ })).toBeInTheDocument()
  })
  it('r17 P04: a finished agent (ticket done) offers Close with a grey dot, not Stop', async () => {
    const { api } = await import('@/api/client')
    vi.spyOn(api, 'listTickets').mockResolvedValue([{ key: 'DEMO-0001', status: 'done' }] as never)
    const stop = vi.spyOn(runsApi, 'stop').mockResolvedValue({ state: 'exited', cleanup: 'complete' })
    list = { ...list, runs: [run('AG', { kind: 'agent', harness: 'claude-code', ticket: 'DEMO-0001' })] }
    mount()
    const close = await screen.findByRole('button', { name: /^Close/ })
    expect(screen.queryByRole('button', { name: /^Stop/ })).toBeNull()
    expect(screen.getByRole('tab')).toHaveTextContent('Finished')
    expect(screen.getByRole('tab').querySelector('span.bg-success')).toBeNull()
    fireEvent.click(close)
    await waitFor(() => expect(stop).toHaveBeenCalled())
  })
  it('P15: an agent run has a one-line summary above its terminal; a shell does not', async () => {
    list = { ...list, runs: [run('AG', { kind: 'agent', harness: 'claude-code', ticket: 'DEMO-0001' })] }
    mount()
    expect(await screen.findByTestId('dock-summary')).toHaveTextContent(/^Agent is working · started \d{2}:\d{2}$/)
    expect(screen.getByTestId('term')).toBeInTheDocument()
    cleanup()
    list = { ...list, runs: [run('SH')] }
    mount()
    await screen.findByTestId('term')
    expect(screen.queryByTestId('dock-summary')).toBeNull()
  })
})
