// The lease UX of section 12.6.5 and the reconnect policy of 12.6.6, against RealTerminal with a fake socket.

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { runsApi, type Run } from '@/api/runs'
import { createRef } from 'react'
import { RealTerminal, type RealTerminalHandle } from './RealTerminal'
import type { TermConnection, TermHandlers } from './termProtocol'

const WS = 'a'.repeat(32)
export const run = (over: Partial<Run> = {}): Run => ({
  run: 'r_01M4Y46B485AXD4CBEJZ15Z1TV', rev: 1, kind: 'shell', ticket: null, harness: 'shell', mode: null, model: null, as_person: true,
  state: 'running', started_at: '2026-10-14T10:00:00Z', viewers: 0, writer: false, cols: 120, rows: 40, replay_until: null,
  label: null, label_hidden: false, cwd: { kind: 'workspace' }, exit: null, ...over,
})

let sockets: { ticket: string; h: TermHandlers; c: TermConnection & Record<string, ReturnType<typeof vi.fn>> }[]
let tickets: { mode: string; take: boolean }[]
const connect = (ticket: string, h: TermHandlers) => {
  const c = { sendInput: vi.fn(), resize: vi.fn(), close: vi.fn() }
  sockets.push({ ticket, h, c: c as never })
  return c as TermConnection
}

beforeEach(() => {
  sockets = []
  tickets = []
  vi.spyOn(runsApi, 'ticket').mockImplementation(async (_ws, _run, mode, take = false) => {
    tickets.push({ mode, take })
    return { ticket: `t${tickets.length}`, expires_in_ms: 30000 }
  })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)) })
const ready = (writer: boolean) => ({ start: 0, offset: 0, cols: 80, rows: 24, writer, state: 'running' as const })

describe('the chat composer handle', () => {
  it('writes a bracketed paste and Enter only while this tab holds the lease', async () => {
    const handle = createRef<RealTerminalHandle>()
    const leases: string[] = []
    render(<RealTerminal ws={WS} run={run()} initial="write" connect={connect} handle={handle} onLease={(l) => leases.push(l)} />)
    await flush()
    expect(handle.current?.sendInput('hi')).toBe(false)
    act(() => sockets[0].h.ready(ready(false)))
    expect(handle.current?.sendInput('hi')).toBe(false)
    expect(sockets[0].c.sendInput).not.toHaveBeenCalled()
    act(() => sockets[0].h.ready(ready(true)))
    expect(leases.at(-1)).toBe('writing')
    expect(handle.current?.sendInput('two\nlines')).toBe(true)
    const [tag, bytes] = (sockets[0].c.sendInput as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(tag).toBe(0)
    expect(new TextDecoder().decode(bytes)).toBe('\x1b[200~two\nlines\x1b[201~\r')
  })
  it('take() asks for the lease with take:true', async () => {
    const handle = createRef<RealTerminalHandle>()
    render(<RealTerminal ws={WS} run={run()} initial="read" connect={connect} handle={handle} />)
    await flush()
    act(() => handle.current?.take())
    await flush()
    expect(tickets[1]).toEqual({ mode: 'write', take: true })
  })
})

describe('lease and reconnect', () => {
  it('K7: the tab that just opened the terminal takes keyboard focus; a read-only one does not', async () => {
    const { unmount } = render(<RealTerminal ws={WS} run={run()} initial="write" connect={connect} />)
    await flush()
    act(() => sockets[0].h.ready(ready(true)))
    expect(document.activeElement?.getAttribute('aria-label')).toBe('Terminal input')
    unmount()
    sockets = []
    render(<RealTerminal ws={WS} run={run()} initial="read" connect={connect} />)
    await flush()
    act(() => sockets[0].h.ready(ready(false)))
    expect(document.activeElement?.getAttribute('aria-label')).not.toBe('Terminal input')
  })
  it('attaches read-only by default and offers Take control (take:true only after the click)', async () => {
    render(<RealTerminal ws={WS} run={run()} initial="read" connect={connect} />)
    await flush()
    expect(tickets).toEqual([{ mode: 'read', take: false }])
    act(() => sockets[0].h.ready(ready(false)))
    expect(screen.getByText('Read only.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Take control' }))
    await flush()
    expect(tickets[1]).toEqual({ mode: 'write', take: true })
    expect(sockets[0].c.close).toHaveBeenCalled()
  })

  it('someone else writing: "Another tab or device is controlling this terminal"', async () => {
    render(<RealTerminal ws={WS} run={run({ writer: true })} initial="read" connect={connect} />)
    await flush()
    act(() => sockets[0].h.ready(ready(false)))
    expect(screen.getByText('Another tab or device is controlling this terminal.')).toBeInTheDocument()
  })

  it('losing the lease shows Take back; a 4409 offers Take control; neither takes by itself', async () => {
    render(<RealTerminal ws={WS} run={run()} initial="write" connect={connect} />)
    await flush()
    expect(tickets).toEqual([{ mode: 'write', take: true }]) // N5: the tab that started the shell gets control
    act(() => sockets[0].h.ready(ready(true)))
    expect(screen.queryByText('Read only.')).toBeNull()
    act(() => sockets[0].h.writer(false))
    expect(screen.getByText('Another tab or device took control of this terminal.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Take back' })).toBeInTheDocument()
    await flush()
    expect(tickets.length).toBe(1)
    act(() => sockets[0].h.closed(4409, { reason: 'writer_busy' }))
    await flush()
    expect(tickets.length).toBe(1)
    expect(screen.getByRole('button', { name: 'Take control' })).toBeInTheDocument()
  })

  it('an ended run is attached once and never retried when the host answers 503', async () => {
    vi.useFakeTimers()
    vi.mocked(runsApi.ticket).mockRejectedValue(new (await import('@/api/types')).ApiError(503, { code: 'host.unavailable', message: 'x', retryable: true }))
    render(<RealTerminal ws={WS} run={run({ state: 'exited', replay_until: '2099-01-01T00:00:00Z' })} initial="read" connect={connect} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    expect(runsApi.ticket).not.toHaveBeenCalled() // nothing is asked for an ended run until the person wants its output
    fireEvent.click(screen.getByRole('button', { name: 'Show output' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
    expect(runsApi.ticket).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/This terminal has ended/)).toBeInTheDocument()
  })
  it('a reconnect (4503, 4401, 4429) asks for read again, never for the lease', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<RealTerminal ws={WS} run={run()} initial="write" connect={connect} />)
    await flush()
    act(() => sockets[0].h.ready(ready(true)))
    act(() => sockets[0].h.closed(4503, { reason: 'runner.changed' }))
    expect(screen.getByText('The runner changed; try again.')).toBeInTheDocument()
    await act(async () => { vi.advanceTimersByTime(1100) })
    await flush()
    expect(tickets.at(-1)).toEqual({ mode: 'read', take: false })
    act(() => sockets.at(-1)!.h.closed(4401, null))
    await act(async () => { vi.advanceTimersByTime(300) })
    await flush()
    expect(tickets.at(-1)).toEqual({ mode: 'read', take: false })
    act(() => sockets.at(-1)!.h.closed(4429, { reason: 'overflow', retry_after_ms: 500 }))
    await act(async () => { vi.advanceTimersByTime(600) })
    await flush()
    expect(tickets.at(-1)).toEqual({ mode: 'read', take: false })
    expect(tickets.filter((t) => t.mode === 'write').length).toBe(1)
  })

  it.each([1000, 4400, 4403, 4422, 4500])('%i does not retry', async (code) => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<RealTerminal ws={WS} run={run()} initial="read" connect={connect} />)
    await flush()
    act(() => sockets[0].h.ready(ready(false)))
    act(() => sockets[0].h.closed(code, code === 1000 ? { reason: 'exited' } : { reason: 'protocol' }))
    await act(async () => { vi.advanceTimersByTime(40_000) })
    await flush()
    expect(tickets.length).toBe(1)
  })

  it('exit, input paused and input lost are shown', async () => {
    render(<RealTerminal ws={WS} run={run()} initial="write" connect={connect} />)
    await flush()
    act(() => sockets[0].h.ready(ready(true)))
    act(() => { sockets[0].h.inputPaused(5000); sockets[0].h.inputLost(); sockets[0].h.exit(3, null) })
    expect(screen.getByText('Input paused')).toBeInTheDocument()
    expect(screen.getByText(/Some input was not delivered/)).toBeInTheDocument()
    expect(screen.getByText('Process exited with code 3.')).toBeInTheDocument()
  })

  it('a remount attaches read again', async () => {
    const view = render(<RealTerminal ws={WS} run={run()} initial="read" connect={connect} />)
    await flush()
    act(() => sockets[0].h.ready(ready(false)))
    fireEvent.click(screen.getByRole('button', { name: 'Take control' }))
    await flush()
    view.unmount()
    render(<RealTerminal ws={WS} run={run({ writer: true })} initial="read" connect={connect} />)
    await flush()
    expect(tickets.at(-1)).toEqual({ mode: 'read', take: false })
  })
})
