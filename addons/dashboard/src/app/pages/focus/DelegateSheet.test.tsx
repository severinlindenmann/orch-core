import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { delegateApi } from '@/api/focus'
import type { AgentPreview } from '@/api/agents'
import type { Run } from '@/api/runs'
import { DelegateSheet } from './DelegateSheet'

const parent = { run: 'r_01J0000000000000000000000A', harness: 'claude-code', ticket: 'DEMO-1', state: 'running' } as Run
const SPEC = 'a'.repeat(64)
const preview = (over: Partial<AgentPreview> = {}): AgentPreview => ({ spec: SPEC, ticket: 'DEMO-1', display: 'x', display_hidden: false, cwd: { kind: 'worktree' }, cwd_path: '/w', cwd_path_hidden: false, will_create_worktree: false, login_shell: false, env: [], grant: null, blocked: null, warnings: [], ...over })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

function mount(props: Partial<React.ComponentProps<typeof DelegateSheet>> = {}) {
  const onClose = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><DelegateSheet ws="w1" ticket="DEMO-1" parent={parent} used={1} max={3} onClose={onClose} {...props} /></QueryClientProvider>)
  return { onClose, user: userEvent.setup() }
}

it('previews then starts exactly the previewed task as a child of the main run, with one idem', async () => {
  const pre = vi.spyOn(delegateApi, 'preview').mockResolvedValue(preview())
  const start = vi.spyOn(delegateApi, 'start').mockResolvedValue({ run: parent })
  const { user, onClose } = mount()
  const sheet = screen.getByTestId('focus-delegate-sheet')
  expect(within(sheet).getByRole('button', { name: 'Delegate' })).toBeDisabled() // a title is required
  await user.type(within(sheet).getByLabelText('Task title'), 'Review tenant isolation')
  await user.type(within(sheet).getByLabelText('What to do'), 'Check the counters.')
  await user.type(within(sheet).getByLabelText('Expected output'), 'A short report')
  expect(screen.getByTestId('delegate-scope')).toHaveTextContent('same folder as the main session')
  expect(screen.getByTestId('delegate-budget')).toHaveTextContent('1 of 3 delegated tasks used')
  await user.click(within(sheet).getByRole('button', { name: 'Delegate' }))
  const args = { ticket: 'DEMO-1', harness: 'claude-code', mode: 'task', parent_run: parent.run, label: 'Review tenant isolation', brief: 'Check the counters.', expected: 'A short report', worktree: true }
  await waitFor(() => expect(start).toHaveBeenCalledWith('w1', args, SPEC, expect.stringMatching(/^a_/)))
  expect(pre).toHaveBeenCalledWith('w1', args)
  await waitFor(() => expect(onClose).toHaveBeenCalled())
})

it('shows why the host refused and starts nothing', async () => {
  vi.spyOn(delegateApi, 'preview').mockResolvedValue(preview({ blocked: { code: 'launch.invalid', message: 'the parent is not a live agent run', detail: {} } }))
  const start = vi.spyOn(delegateApi, 'start')
  const { user } = mount()
  await user.type(screen.getByLabelText('Task title'), 'Task')
  await user.click(screen.getByRole('button', { name: 'Delegate' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('the parent is not a live agent run')
  expect(start).not.toHaveBeenCalled()
})

it('disables the button at the delegation limit', async () => {
  mount({ used: 3, max: 3 })
  expect(screen.getByRole('button', { name: 'Delegate' })).toBeDisabled()
})
