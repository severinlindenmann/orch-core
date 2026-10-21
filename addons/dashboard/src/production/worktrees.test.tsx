import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { runsApi, type Run } from '@/api/runs'
import { onDockRequest } from '@/app/terminal/dock/request'
import { expect, it, vi } from 'vitest'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'

// The production app on a fake server that speaks the worktree routes (H11d, section 12.7).
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const row = (over: Record<string, unknown> = {}) => ({
  ticket: 'DEMO-0001', title: 'First ticket', repo: 'code', branch: 'feat/one', path: '/work/code.worktrees/aaaaaaaa-DEMO-0001',
  state: 'ok', dirty: 0, checked: true, created_at: '2026-10-18T08:00:00Z', by: 'agent', mine: false, ...over,
})
let rows: unknown[] = []
let refuse: string | null = null
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, 'worktrees', 'terminals_page'] })])
const route = () => {
  rows = [row(), row({ ticket: 'DEMO-0002', title: 'Second', state: 'missing', dirty: null, mine: true, by: 'person' })]
  refuse = null
  state.host.override = (path, method) => {
    if (!path.endsWith('/worktrees') && !path.includes('/worktrees/')) return undefined
    if (method === 'GET') return Response.json({ available: true, rows })
    if (refuse) return Response.json({ ok: false, error: { code: refuse, message: refuse, retryable: false } }, { status: 409 })
    if (path.endsWith('/remove')) {
      rows = rows.slice(1)
      return Response.json({ removed: true })
    }
    return undefined
  }
}
const calls = (p: string) => state.host.calls.filter((c) => c.path.includes(p))

it('lists each ticket’s worktree in plain words and keeps folder and branch behind Technical details', async () => {
  route()
  show('/w/DEMO/worktrees')
  const list = await screen.findByRole('list', { name: 'Worktrees' }, T)
  const [first, second] = within(list).getAllByRole('listitem')
  expect(first).toHaveTextContent('Ready')
  expect(first).toHaveTextContent('no unsaved changes')
  expect(first).toHaveTextContent('by an agent')
  expect(second).toHaveTextContent('Folder is gone')
  expect(second).toHaveTextContent('not checked') // unknown is never zero
  expect(second).toHaveTextContent('by you')
  expect(second).not.toHaveTextContent(/0 changed|no unsaved/)
  const details = within(first).getByText('Technical details').closest('details')!
  expect(details).not.toHaveAttribute('open')
  expect(details).toHaveTextContent('/work/code.worktrees/aaaaaaaa-DEMO-0001')
  expect(calls('/worktrees')[0].method).toBe('GET')
})

it('Remove asks first, posts once with an idem, and the row goes', async () => {
  route()
  const user = userEvent.setup()
  show('/w/DEMO/worktrees')
  await user.click(await screen.findByRole('button', { name: 'Remove the worktree of DEMO-0001' }, T))
  expect(await screen.findByText('Remove this worktree?')).toBeInTheDocument()
  expect(calls('/worktrees/remove')).toEqual([])
  await user.click(screen.getByRole('button', { name: 'Remove worktree' }))
  await waitFor(() => expect(calls('/worktrees/remove')).toHaveLength(1), T)
  const body = calls('/worktrees/remove')[0].body as Record<string, unknown>
  expect(body).toMatchObject({ ticket: 'DEMO-0001', repo: 'code' })
  expect(body.idem).toMatch(/^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$/)
  expect(body).not.toHaveProperty('force')
  await waitFor(() => expect(screen.queryByText('Remove this worktree?')).not.toBeInTheDocument(), T)
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Remove the worktree of DEMO-0001' })).not.toBeInTheDocument(), T)
})

it('a refusal is said in words, with no code on screen', async () => {
  route()
  refuse = 'worktree.dirty'
  const user = userEvent.setup()
  show('/w/DEMO/worktrees')
  await user.click(await screen.findByRole('button', { name: 'Remove the worktree of DEMO-0001' }, T))
  await user.click(screen.getByRole('button', { name: 'Remove worktree' }))
  const alert = await screen.findByRole('alert', {}, T)
  expect(alert).toHaveTextContent('changes that are not saved in git')
  expect(alert).not.toHaveTextContent('worktree.dirty')
})

it('says so when there are none, and when the host does not serve worktrees', async () => {
  route()
  rows = []
  show('/w/DEMO/worktrees')
  expect(await screen.findByTestId('worktrees-empty', {}, T)).toHaveTextContent('No ticket has its own worktree yet')
})

it('the ticket page shows its worktree per repository and the shared copy when there is none', async () => {
  route()
  rows = []
  window.innerWidth = 1400 // the rail is a column from 1280 px
  show('/w/DEMO/ticket/DEMO-0001')
  const section = await screen.findByTestId('ticket-worktree', {}, T)
  expect(section).toHaveTextContent('Shared working copy')
  expect(within(section).getByRole('button', { name: 'Add worktree' })).toBeInTheDocument()
  expect(calls('/worktrees?ticket=DEMO-0001').length).toBeGreaterThan(0)
})

it('previews the worktree cwd and opens its confirmed terminal in the dock', async () => {
  route()
  const preview = vi.spyOn(runsApi, 'preview').mockResolvedValue({ spec: 'demo-spec', display: 'shell', display_hidden: false, cwd_path: '/work/code.worktrees/demo', cwd_path_hidden: false, notice: null, as_person: true, login_shell: true, env: [], blocked: null, warnings: [] })
  const open = vi.spyOn(runsApi, 'open').mockResolvedValue({ run: { run: 'demo-new-run' } as Run })
  const dock = vi.fn()
  const unsubscribe = onDockRequest(dock)
  try {
    const user = userEvent.setup()
    show('/w/DEMO/worktrees')
    await user.click(await screen.findByRole('button', { name: 'Open a terminal in the worktree of DEMO-0001' }, T))
    expect(screen.queryByRole('button', { name: 'Open a terminal in the worktree of DEMO-0002' })).not.toBeInTheDocument()
    const args = { ticket: 'DEMO-0001', cwd: { kind: 'worktree', repo: 'code', ticket: 'DEMO-0001' } }
    expect(preview).toHaveBeenCalledWith(A, args)
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('/work/code.worktrees/demo')
    expect(open).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: 'Open terminal' }))
    await waitFor(() => expect(open).toHaveBeenCalledWith(A, args, 'demo-spec', expect.stringMatching(/^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$/)))
    expect(dock).toHaveBeenCalledWith('demo-new-run')
  } finally { unsubscribe(); preview.mockRestore(); open.mockRestore() }
})
