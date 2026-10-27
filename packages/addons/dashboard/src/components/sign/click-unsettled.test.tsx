import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { CLICK_RISK, unknownOutcome } from '@/api/click'
import { ApiError } from '@/api/types'
import { useClickSign } from './useClickSign'

const REQ = 'hr_' + '0'.repeat(25) + '1'
const view = { view_digest: 'sha256:' + 'c'.repeat(64), view: { risk: CLICK_RISK, op: 'approve', workspace: { id: 'a', name: 'n', prefix: 'D' }, fingerprint: '0'.repeat(12), lines: ['x'], checks: [], confirm: { kind: 'click' as const }, expires_at: '', auth: 'convenience', device: 'd', artifacts: null } }

function Probe({ ws, onRun }: { ws: string; onRun: (r: ReturnType<typeof useClickSign>) => void }) {
  const run = useClickSign(ws)
  onRun(run)
  return <p>{run.phase}{run.leftUnsettled ? ' left' : ''}{run.switched ? ' switched' : ''}</p>
}

afterEach(() => vi.restoreAllMocks())

describe('switching workspace', () => {
  const setup = async () => {
    vi.spyOn(api, 'prepareClick').mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'approve', confirm: 'click' })
    vi.spyOn(api, 'viewClick').mockResolvedValue(view)
    const cancel = vi.spyOn(api, 'cancelClick').mockResolvedValue({ id: REQ, state: 'cancelled' })
    vi.spyOn(api, 'stateClick').mockResolvedValue({ id: REQ, state: 'bound' })
    let run!: ReturnType<typeof useClickSign>
    const client = new QueryClient()
    const ui = (ws: string) => (
      <QueryClientProvider client={client}>
        <Probe ws={ws} onRun={(r) => (run = r)} />
      </QueryClientProvider>
    )
    const v = render(ui('a'.repeat(32)))
    await act(async () => void (await run.start({ action: 'approve', key: 'DEMO-1', gate: 'plan' })))
    await waitFor(() => expect(run.phase).toBe('ready'))
    return { run: () => run, rerender: (ws: string) => v.rerender(ui(ws)), cancel }
  }

  it('with an open request cancels it and says so', async () => {
    const t = await setup()
    t.rerender('b'.repeat(32))
    await waitFor(() => expect(screen.getByText(/switched/)).toBeInTheDocument())
    expect(t.cancel).toHaveBeenCalledTimes(1)
  })

  it('C2: cancelling tombstones the request before the answer; another press increments retry', async () => {
    const t = await setup()
    const sign = vi.spyOn(api, 'signClick')
    let finish!: () => void
    t.cancel.mockImplementationOnce(() => new Promise(resolve => { finish = () => resolve({ id: REQ, state: 'cancelled' }) }))
    let cancellation!: Promise<void>
    act(() => { cancellation = t.run().cancel() })
    await act(async () => { await t.run().sign() })
    expect(sign).not.toHaveBeenCalled()
    expect(t.run().phase).toBe('cancelled')
    await act(async () => { finish(); await cancellation })
    await act(async () => { await t.run().again({ action: 'approve', key: 'DEMO-1', gate: 'plan' }) })
    expect(api.prepareClick).toHaveBeenLastCalledWith('a'.repeat(32), { action: 'approve', key: 'DEMO-1', gate: 'plan' }, 1)
    expect(sign).not.toHaveBeenCalled()
  })

  it('while the outcome is unknown cancels nothing and claims nothing', async () => {
    const t = await setup()
    vi.spyOn(api, 'signClick').mockRejectedValue(new ApiError(409, { code: 'human.click_refused', message: 'x', retryable: false, reason: 'uncertain' }))
    await act(async () => void (await t.run().sign()))
    await waitFor(() => expect(t.run().phase).toBe('settling'))
    t.rerender('b'.repeat(32))
    await waitFor(() => expect(screen.getByText(/left/)).toBeInTheDocument())
    expect(screen.queryByText(/switched/)).toBeNull()
    expect(t.cancel).not.toHaveBeenCalled()
  })
})

describe('what is an unknown outcome', () => {
  it('a lost answer and a catalog-less server failure are; a refusal of the catalog is not', () => {
    expect(unknownOutcome(new TypeError('Failed to fetch'))).toBe(true)
    expect(unknownOutcome(new ApiError(502, { code: 'host.unavailable', message: 'x', retryable: true }))).toBe(true)
    expect(unknownOutcome(new ApiError(503, { code: 'host.unavailable', message: 'x', retryable: true }, 9))).toBe(false)
    expect(unknownOutcome(new ApiError(409, { code: 'human.click_refused', message: 'x', retryable: false, reason: 'view_changed' }, 3))).toBe(false)
  })
})
