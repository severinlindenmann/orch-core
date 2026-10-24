import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { READS, TOM, settingsOf, show, useFakeHost, workspace } from '@/test/hostHarness'

// The production app on a fake server: Settings, Add-ons, Model routing (a signed settings change, the typed word).
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const CHOICES = {
  harnesses: [
    { id: 'claude-code', label: 'Claude', models: [{ id: 'opus', label: 'Opus' }, { id: 'sonnet', label: 'Sonnet' }, { id: 'haiku', label: 'Haiku' }] },
    { id: 'codex', label: 'Codex', models: [{ id: 'gpt-5.4', label: 'GPT-5.4' }, { id: 'o3', label: 'o3' }] },
  ],
  modes: [
    { id: 'work', label: 'Do the work' },
    { id: 'refine', label: 'Write requirements and plan' },
    { id: 'context', label: 'Read and answer' },
  ],
}
const addOns = (on: boolean) => ({ available: true, add_ons: [{ id: 'model-routing', title: 'Model routing', desc: 'Choose models.', state: on ? 'on' : 'off', locked: null, blocked: null }] })
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, 'model_routing'], addOns: addOns(true) } as never)])
const route = (models: unknown = null, on = true) => {
  const ws = state.host.workspaces[0]
  ws.addOns = addOns(on) as never
  ws.settings = settingsOf({ models } as never)
  state.host.override = (path, method) => (path.endsWith('/model-routing') && method === 'GET' ? Response.json(CHOICES) : undefined)
}
/** Settings opens in place under its row; the eight choices sit behind Customize models. */
async function openBox(custom = true) {
  const u = userEvent.setup()
  await u.click(await screen.findByTestId('addon-model-routing-expand', {}, T))
  if (custom) await u.click(await screen.findByRole('button', { name: 'Customize models' }, T))
  return await screen.findByTestId('model-routing', {}, T)
}
const calls = (p: string) => state.host.calls.filter((c) => c.path.includes(p))
const posted = () => calls('/handoff').filter((c) => c.method === 'POST')

it('shows a row per kind of work and agent, all on the agent default, only while the add-on is on', async () => {
  route()
  show('/w/DEMO/settings/addons')
  const box = await openBox()
  expect(box).toHaveTextContent('Do the work')
  expect(box).toHaveTextContent('Write requirements and plan')
  expect(box).toHaveTextContent('Read and answer')
  for (const sel of within(box).getAllByRole('combobox')) expect(sel).toHaveValue('')
  expect(within(box).getByRole('button', { name: 'Review and sign' })).toBeDisabled() // nothing changed
})

it('defaults stay behind Customize models and the summary names the available agents', async () => {
  route()
  show('/w/DEMO/settings/addons')
  const box = await openBox(false)
  expect(within(box).queryByRole('combobox')).toBeNull()
  expect(within(box).getByTestId('routing-summary')).toHaveTextContent('Every agent uses its own default model. Available: Claude, Codex.')
})

it('is not drawn while the add-on is off', async () => {
  route(null, false)
  show('/w/DEMO/settings/addons')
  await screen.findByRole('list', { name: 'Add-ons' }, T)
  expect(screen.queryByTestId('model-routing')).toBeNull()
  expect(screen.queryByTestId('addon-model-routing-expand')).toBeNull()
})

it('Review and sign sends exactly the chosen table, from the closed list, as one signed change', async () => {
  route()
  const user = userEvent.setup()
  show('/w/DEMO/settings/addons')
  const box = await openBox()
  const claude = within(within(box).getByTestId('routing-claude-code'))
  expect(claude.getAllByRole('option').map((o) => (o as HTMLOptionElement).value)).toEqual(['', 'opus', 'sonnet', 'haiku', '', 'opus', 'sonnet', 'haiku', '', 'opus', 'sonnet', 'haiku'])
  await user.selectOptions(within(box).getByLabelText('Do the work', { selector: '#route-claude-code-work' }), 'sonnet')
  await user.selectOptions(within(box).getByLabelText('Read and answer', { selector: '#route-codex-context' }), 'o3')
  await user.click(within(box).getByRole('button', { name: 'Review and sign' }))
  await waitFor(() => expect(calls('/handoff').filter((c) => c.method === 'POST')).toHaveLength(1), T)
  expect(posted()[0].body).toEqual({ action: 'models_set', models: { 'claude-code': { work: 'sonnet' }, codex: { context: 'o3' } } })
})

it('choosing the agent default for every row turns Model routing off', async () => {
  route({ 'claude-code': { work: 'sonnet' } })
  const user = userEvent.setup()
  show('/w/DEMO/settings/addons')
  const box = await openBox()
  const pick = within(box).getByLabelText('Do the work', { selector: '#route-claude-code-work' })
  await waitFor(() => expect(pick).toHaveValue('sonnet'), T)
  await user.selectOptions(pick, '')
  await user.click(within(box).getByRole('button', { name: 'Turn off Model routing' }))
  await waitFor(() => expect(calls('/handoff').filter((c) => c.method === 'POST')).toHaveLength(1), T)
  expect(posted()[0].body).toEqual({ action: 'addon_disable', name: 'model-routing' })
})

it('a member can read it but not change it', async () => {
  route()
  state.host.workspaces[0].me = TOM
  state.host.workspaces[0].role = 'member'
  show('/w/DEMO/settings/addons')
  const box = await openBox()
  for (const sel of within(box).getAllByRole('combobox')) expect(sel).toBeDisabled()
  expect(within(box).queryByRole('button', { name: 'Review and sign' })).toBeNull()
  expect(box).toHaveTextContent('Only an owner can change this')
})

it('MR1: a failed choices read shows Retry instead of nothing, and editing waits for the signed table', async () => {
  route()
  let fail = true
  state.host.override = (path, method) => (path.endsWith('/model-routing') && method === 'GET' ? (fail ? Response.json({ ok: false, error: { code: 'x', message: 'x', retryable: false } }, { status: 500 }) : Response.json(CHOICES)) : undefined)
  const user = userEvent.setup()
  show('/w/DEMO/settings/addons')
  await user.click(await screen.findByTestId('addon-model-routing-expand', {}, T))
  await user.click(await screen.findByRole('button', { name: 'Retry' }, T))
  fail = false
  await user.click(await screen.findByRole('button', { name: 'Retry' }, T))
  await user.click(await screen.findByRole('button', { name: 'Customize models' }, T))
  const box = await screen.findByTestId('model-routing', {}, T)
  for (const sel of within(box).getAllByRole('combobox')) expect(sel).toBeEnabled()
})

it('MR2/MR5/MR6: explains Agent default, shows the resolved name, one collapsible section per agent, and says when there is nothing to save', async () => {
  route({ 'claude-code': { work: 'sonnet' } })
  show('/w/DEMO/settings/addons')
  const box = await openBox()
    expect(within(box).getByTestId('agent-default-help')).toHaveTextContent('orch does not choose one')
  expect(within(box).getAllByText('Chosen by the agent app').length).toBeGreaterThan(0)
  expect(within(box).getByText('Claude Code').closest('summary')).not.toBeNull()
  expect(within(box).getByText('Codex').closest('summary')).not.toBeNull()
  expect(within(box).getByTestId('routing-nochange')).toHaveTextContent('No changes to save')
})
