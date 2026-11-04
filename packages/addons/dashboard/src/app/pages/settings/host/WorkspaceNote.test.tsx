import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { Workspace } from '@/api/types'
import { WorkspaceNote } from './WorkspaceNote'

const viewer = vi.hoisted(() => ({ role: 'owner' as string, remote: false }))
vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), showsLogout: () => viewer.remote }))
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
  viewer.remote = false
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
  it('never says trusted while Claude Code has no settings file yet', async () => {
    fns.getClaudeTrust.mockResolvedValue({ available: true, on: false, config: 'missing', offered: true, why_not: '', parent_config: '' })
    show()
    expect(await screen.findByText(/Claude Code not set up yet/)).toBeTruthy()
    expect(screen.queryByText(/trusts this folder/)).toBeNull()
  })
  it('says when the history is off', async () => {
    fns.getRecords.mockResolvedValue(records(false))
    show()
    expect(await screen.findByText(/History off/)).toBeTruthy()
  })
  it('says when Claude Code asks about each folder, and the i says the same state', async () => {
    fns.getClaudeTrust.mockResolvedValue({ available: true, on: false, offered: true, why_not: '', parent_config: '' })
    fns.getRecords.mockResolvedValue(records(false))
    show()
    expect(await screen.findByText('Claude Code asks about each folder · History off')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'More about this' }))
    expect(await screen.findByText(/Setup turns this off for the folders orch makes/)).toBeTruthy()
    expect(screen.getByText(/not being saved for you/)).toBeTruthy()
  })
  it('leaves out the trust part when the host cannot say', async () => {
    fns.getClaudeTrust.mockResolvedValue({ available: false })
    show()
    expect(await screen.findByText('History on')).toBeTruthy()
  })
  it('leaves out the history part when records are not configured or have no repo', async () => {
    fns.getRecords.mockResolvedValue({ ...records(true), configured: false })
    show()
    expect(await screen.findByText('Claude Code trusts this folder')).toBeTruthy()
    cleanup()
    fns.getRecords.mockResolvedValue({ ...records(true), repo: null })
    show()
    expect(await screen.findByText('Claude Code trusts this folder')).toBeTruthy()
  })
  it('draws nothing when both parts are absent', async () => {
    fns.getClaudeTrust.mockResolvedValue({ available: false })
    fns.getRecords.mockResolvedValue({ available: false })
    const { container } = show()
    await waitFor(() => expect(fns.getClaudeTrust).toHaveBeenCalled())
    await waitFor(() => expect(container.textContent).toBe(''))
  })
  it('in a hub or remote session it makes no Claude-trust call (the host refuses it there)', async () => {
    viewer.remote = true
    show()
    expect(await screen.findByText('History on')).toBeTruthy()
    expect(fns.getClaudeTrust).not.toHaveBeenCalled()
  })
  it('draws nothing for a non-owner and asks the host nothing', async () => {
    viewer.role = 'member'
    const { container } = show()
    await new Promise((r) => setTimeout(r, 20))
    expect(container.textContent).toBe('')
    expect(fns.getClaudeTrust).not.toHaveBeenCalled()
    expect(fns.getRecords).not.toHaveBeenCalled()
  })
})
