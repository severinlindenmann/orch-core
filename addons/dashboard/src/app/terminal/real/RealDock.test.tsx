// The real-host dock (M7a): the preview before opening, the idem kept on a retry, Stop with incomplete cleanup, and
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
vi.mock('../dock/context', async (orig) => ({ ...(await orig<typeof import('../dock/context')>()), useDockTicket: () => undefined }))
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
afterEach(() => { cleanup(); vi.restoreAllMocks(); setFeatures(null) })

describe('the real dock', () => {
  it('New terminal shows the host preview as untrusted text, then opens with one idem and attaches with the lease', async () => {
    vi.spyOn(runsApi, 'preview').mockResolvedValue(preview())
    const opened = run('AA')
    const open = vi.spyOn(runsApi, 'open')
      .mockRejectedValueOnce(new ApiError(503, { code: 'outcome.unknown', message: 'Try again', retryable: true }))
      .mockImplementation(async () => { list = { ...list, runs: [opened] }; return { run: opened } })
    mount()
    await screen.findByText('No terminal is open. Start one with New terminal.')
    fireEvent.pointerDown(screen.getByRole('button', { name: 'New terminal' }), { button: 0, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Shell in the workspace' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('Dashboard execution: agents and terminals started here run as you.')
    expect(dialog).toHaveTextContent('$SHELL -l')
    expect(dialog).toHaveTextContent('acts as you, not as an agent')
    expect(dialog.textContent).not.toContain('‮') // the bidi override is shown, never applied
    fireEvent.click(await screen.findByRole('button', { name: 'Open terminal' }))
    await screen.findByText('Try again')
    fireEvent.click(screen.getByRole('button', { name: 'Open terminal' }))
    await waitFor(() => expect(open).toHaveBeenCalledTimes(2))
    expect(open.mock.calls[0][3]).toMatch(/^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$/)
    expect(open.mock.calls[1][3]).toBe(open.mock.calls[0][3]) // the retry keeps the idem: one run, never two
    const term = await screen.findByTestId('term')
    expect(term.dataset.run).toBe(opened.run)
    expect(term.dataset.initial).toBe('write') // the shell this person just opened; take is never set by itself
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
    fireEvent.click(await screen.findByRole('button', { name: 'Open terminal' }))
    await waitFor(() => expect(screen.getByTestId('term').dataset.run).toBe(opened.run))
    expect(screen.getByTestId('term').dataset.initial).toBe('write')
    fireEvent.click(screen.getAllByRole('tab')[1])
    await waitFor(() => expect(screen.getByTestId('term').dataset.run).toBe(other.run))
    fireEvent.click(screen.getAllByRole('tab')[0])
    await waitFor(() => expect(screen.getByTestId('term').dataset.run).toBe(opened.run))
    expect(screen.getByTestId('term').dataset.initial).toBe('read')
  })

  it('a blocked preview cannot be confirmed', async () => {
    vi.spyOn(runsApi, 'preview').mockResolvedValue(preview({ blocked: { code: 'runner.unavailable', message: 'The runner is not running.' } }))
    mount()
    await screen.findByText(/No terminal is open/)
    fireEvent.pointerDown(screen.getByRole('button', { name: 'New terminal' }), { button: 0, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Shell in the workspace' }))
    await screen.findByText(/The runner is not running\./)
    expect(screen.getByRole('button', { name: 'Open terminal' })).toBeDisabled()
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

  it('Start agent stays on the not-available path (the server never lists agent_start)', async () => {
    setFeatures({ features: ['terminals'], workspaces: { [WS]: ['terminals'] } })
    mount()
    await screen.findByText(/No terminal is open/)
    fireEvent.pointerDown(screen.getByRole('button', { name: 'New terminal' }), { button: 0, pointerType: 'mouse' })
    expect(await screen.findByRole('menuitem', { name: /Start agent/ })).toHaveAttribute('aria-disabled', 'true')
  })
})
