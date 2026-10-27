vi.mock('@/components/sign/trustedPress', () => ({ trustedPress: () => true }))
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { folderSummary, sizeText, workdirError } from '@/api/workdir'
import { ApiError } from '@/api/types'
import type { TicketDocument } from '@/api/types'
import { WorkFolder } from './WorkFolder'

const get = vi.fn()
const remove = vi.fn()
vi.mock('@/api/features', () => ({ isRealHost: () => true, hasFeature: () => true }))
vi.mock('@/app/workspace', () => ({ useWorkspace: () => ({ workspace: { id: 'w1' } }) }))
vi.mock('@/app/useRole', () => ({ useRole: () => 'owner' }))
vi.mock('@/api/workdir', async (orig) => ({
  ...(await orig<typeof import('@/api/workdir')>()),
  workdirApi: { get: (...a: unknown[]) => get(...a), remove: (...a: unknown[]) => remove(...a) },
  workdirQueries: {
    ofTicket: (ws: string, key: string) => ({ queryKey: ['workdir', ws, key], queryFn: () => get(ws, key), retry: false }),
  },
}))

const ticket = { key: 'DEMO-0001', links: { repos: [], branches: {} } } as unknown as TicketDocument

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <WorkFolder ticket={ticket} />
    </QueryClientProvider>,
  )
}

describe('work folder words', () => {
  it('counts files and size plainly, and never writes an unknown size as zero', () => {
    expect(folderSummary({ files: 14, bytes: 2_200_000 })).toBe('14 files, 2.1 MB')
    expect(folderSummary({ files: 1, bytes: 12 })).toBe('1 file, 12 bytes')
    expect(folderSummary({ files: 20000, bytes: 5, truncated: true })).toBe('more than 20000 files, 5 bytes')
    expect(folderSummary({ files: null, bytes: null })).toBe('size not known')
    expect(sizeText(2048)).toBe('2.0 KB')
  })
  it('explains a run that is still working in the folder', () => {
    expect(workdirError(new ApiError(409, { code: 'worktree.in_use', message: 'x', retryable: false }))).toMatch(/Stop on that run/)
  })
})

describe('the work folder line on the ticket page', () => {
  beforeEach(() => {
    get.mockReset()
    remove.mockReset()
  })
  it('shows nothing for a ticket with a codebase or without a folder', async () => {
    get.mockResolvedValue({ available: true, exists: false, codebase: true })
    const { container } = show()
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
  it('shows what is there, keeps the path under technical details, and deletes only after an inline second press', async () => {
    get.mockResolvedValue({ available: true, exists: true, codebase: false, folder: '.orch-work/DEMO-0001', files: 14, bytes: 2_200_000, truncated: false })
    remove.mockResolvedValue({ deleted: true })
    const user = userEvent.setup()
    show()
    expect(await screen.findByText(/Work folder: 14 files, 2.1 MB, not artifacts/)).toBeTruthy()
    expect(screen.getByText('Technical details')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Delete work folder' }))
    expect(remove).not.toHaveBeenCalled() // the question comes first
    expect(screen.queryByRole('dialog')).toBeNull()
    get.mockResolvedValue({ available: true, exists: false, codebase: false })
    await user.click(screen.getByRole('button', { name: 'Press again to delete' }))
    await waitFor(() => expect(remove).toHaveBeenCalledWith('w1', 'DEMO-0001', expect.stringMatching(/^a_/)))
  })
  it('says why a delete was refused', async () => {
    get.mockResolvedValue({ available: true, exists: true, codebase: false, folder: '.orch-work/DEMO-0001', files: 1, bytes: 1 })
    remove.mockRejectedValue(new ApiError(409, { code: 'worktree.in_use', message: 'x', retryable: false }))
    const user = userEvent.setup()
    show()
    await user.click(await screen.findByRole('button', { name: 'Delete work folder' }))
    await user.click(screen.getByRole('button', { name: 'Press again to delete' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
  })
})
