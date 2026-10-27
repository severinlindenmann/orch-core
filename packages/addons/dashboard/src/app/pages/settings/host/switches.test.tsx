import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { ClaudeTrust, RecordsStatus } from '@/api/quickRecords'
import type { Workspace } from '@/api/types'
import { WorkspaceSwitches } from './Switches'

const viewer = vi.hoisted(() => ({ role: 'owner' as string }))
vi.mock('./useViewerRole', () => ({ useViewerRole: () => viewer.role }))

const ws = { id: 'w1', prefix: 'T', name: 'Test' } as unknown as Workspace
const TRUST = /without asking/
const HISTORY = /Keep a history of your work/

const fns = {
  getClaudeTrust: vi.fn(),
  grantClaudeTrust: vi.fn(),
  getRecords: vi.fn(),
  recordsAutoOn: vi.fn(),
  recordsAutoOff: vi.fn(),
}

const trust = (over: Partial<ClaudeTrust> = {}): ClaudeTrust => ({ available: true, on: false, offered: true, why_not: '', parent_config: '', ...over })
const records = (over: Partial<RecordsStatus> = {}): RecordsStatus => ({
  available: true,
  configured: true,
  repo: { state: 'ok', problem: null, problem_hidden: false },
  auto: { on: false, every_min: 15, push: false },
  ...over,
})

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <WorkspaceSwitches workspace={ws} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  viewer.role = 'owner'
  for (const f of Object.values(fns)) f.mockReset()
  fns.getClaudeTrust.mockResolvedValue(trust())
  fns.grantClaudeTrust.mockResolvedValue({ on: true })
  fns.getRecords.mockResolvedValue(records())
  fns.recordsAutoOn.mockResolvedValue({ ok: true })
  fns.recordsAutoOff.mockResolvedValue({ ok: true })
  Object.assign(api as unknown as Record<string, unknown>, fns)
})
afterEach(() => cleanup())

describe('WorkspaceSwitches', () => {
  it('turns the folder question off with a plain grant', async () => {
    show()
    fireEvent.click(await screen.findByRole('switch', { name: TRUST }))
    await waitFor(() => expect(fns.grantClaudeTrust).toHaveBeenCalledWith('w1', false))
  })

  it('shows the reason and "Trust anyway" when another parent folder has settings', async () => {
    fns.getClaudeTrust.mockResolvedValue(trust({ offered: false, why_not: 'A folder above has settings of someone else.', parent_config: '/home/x/.claude' }))
    show()
    expect(await screen.findByText('A folder above has settings of someone else.')).toBeTruthy()
    fireEvent.click(screen.getByRole('switch', { name: TRUST }))
    expect(fns.grantClaudeTrust).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Trust anyway' }))
    await waitFor(() => expect(fns.grantClaudeTrust).toHaveBeenCalledWith('w1', true))
  })

  it('turns the history on and off', async () => {
    show()
    fireEvent.click(await screen.findByRole('switch', { name: HISTORY }))
    await waitFor(() => expect(fns.recordsAutoOn).toHaveBeenCalledWith('w1'))
    cleanup()
    fns.getRecords.mockResolvedValue(records({ auto: { on: true, every_min: 15, push: false } }))
    show()
    fireEvent.click(await screen.findByRole('switch', { name: HISTORY }))
    await waitFor(() => expect(fns.recordsAutoOff).toHaveBeenCalledWith('w1'))
  })

  it('hides both switches from anyone but an owner', async () => {
    viewer.role = 'maintainer'
    show()
    await waitFor(() => expect(screen.queryByRole('switch')).toBeNull())
    expect(fns.getRecords).not.toHaveBeenCalled()
    expect(fns.getClaudeTrust).not.toHaveBeenCalled()
  })

  it('hides the history when records are not set up', async () => {
    fns.getRecords.mockResolvedValue(records({ configured: false, repo: null }))
    show()
    await screen.findByRole('switch', { name: TRUST })
    await waitFor(() => expect(fns.getRecords).toHaveBeenCalled())
    expect(screen.queryByRole('switch', { name: HISTORY })).toBeNull()
  })
})
