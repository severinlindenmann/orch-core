import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { CLICK_RISK } from '@/api/click'
import { ApiError } from '@/api/types'
import { useClickSign } from './useClickSign'

const REQ = 'hr_' + '0'.repeat(25) + '1'
const view = { view_digest: 'sha256:' + 'c'.repeat(64), view: { risk: CLICK_RISK, op: 'approve', workspace: { id: 'a', name: 'n', prefix: 'D' }, fingerprint: '0'.repeat(12), lines: ['x'], checks: [], confirm: { kind: 'click' as const }, expires_at: '', auth: 'convenience', device: 'd', artifacts: null } }

function Probe({ onRun }: { onRun: (r: ReturnType<typeof useClickSign>) => void }) {
  const run = useClickSign('a'.repeat(32))
  onRun(run)
  return <p>{run.phase}</p>
}

afterEach(() => vi.restoreAllMocks())

describe('the too-soon guard is invisible', () => {
  it('a first press refused as too soon waits and signs once more, with no problem shown', async () => {
    vi.spyOn(api, 'prepareClick').mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'approve', confirm: 'click' })
    vi.spyOn(api, 'viewClick').mockResolvedValue(view)
    const sign = vi
      .spyOn(api, 'signClick')
      .mockRejectedValueOnce(new ApiError(409, { code: 'human.click_refused', message: 'x', retryable: false, reason: 'too_soon' }))
      .mockResolvedValue({ state: 'submitted', duplicate: false } as never)
    let run!: ReturnType<typeof useClickSign>
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Probe onRun={(r) => (run = r)} />
      </QueryClientProvider>,
    )
    await act(async () => void (await run.start({ action: 'approve', key: 'DEMO-1', gate: 'plan' })))
    await waitFor(() => expect(run.phase).toBe('ready'))
    await act(async () => void (await run.sign()))
    expect(sign).toHaveBeenCalledTimes(2)
    expect(run.phase).toBe('done')
    expect(run.problem).toBeNull()
  })
})
