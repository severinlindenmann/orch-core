import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { Workspace } from '@/api/types'
import { WorkspaceNote } from './WorkspaceNote'

const viewer = vi.hoisted(() => ({ role: 'owner' as string }))
vi.mock('./useViewerRole', () => ({ useViewerRole: () => viewer.role }))

const ws = { id: 'w1', prefix: 'T', name: 'Test' } as unknown as Workspace
const fns = { getClaudeTrust: vi.fn(), getRecords: vi.fn() }
const records = (on: boolean) => ({ available: true, configured: true, repo: { state: 'ok', problem: null, problem_hidden: false }, auto: { on, every_min: 15, push: false } })

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><WorkspaceNote workspace={ws} /></QueryClientProvider>)
}

beforeEach(() => {
  viewer.role = 'owner'
  for (const f of Object.values(fns)) f.mockReset()
  fns.getClaudeTrust.mockResolvedValue({ available: true, on: true, offered: false, why_not: '', parent_config: '' })
  fns.getRecords.mockResolvedValue(records(true))
  Object.assign(api as unknown as Record<string, unknown>, fns)
})
afterEach(() => cleanup())

describe('WorkspaceNote', () => {
  it('is one quiet line with an i and no switch', async () => {
    show()
    expect(await screen.findByText('Claude Code trusts this folder · History on')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'More about this' })).toBeTruthy()
    expect(screen.queryByRole('switch')).toBeNull()
  })
  it('says when the history is off', async () => {
    fns.getRecords.mockResolvedValue(records(false))
    show()
    expect(await screen.findByText(/History off/)).toBeTruthy()
  })
  it('draws nothing for a non-owner', async () => {
    viewer.role = 'member'
    const { container } = show()
    await new Promise((r) => setTimeout(r, 20))
    expect(container.textContent).toBe('')
  })
})
