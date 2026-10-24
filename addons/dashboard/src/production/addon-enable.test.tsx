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
const calls = (p: string) => state.host.calls.filter((c) => c.path.includes(p))
const posted = () => calls('/handoff').filter((c) => c.method === 'POST')


const setup = (name: 'publish' | 'model-routing', on = false, setUp: boolean | null = false) => {
  route(null, on)
  state.host.workspaces[0].addOns = { available: true, add_ons: [{ id: name, title: name === 'publish' ? 'Publish' : 'Model routing', desc: 'Settings for this workspace.', state: on ? 'on' : 'off', locked: null, blocked: null, needs: name === 'publish' ? 'publish' : 'models', set_up: setUp }] } as never
}

it.each(['model-routing', 'publish'] as const)('off %s opens settings and sends one enable action', async (name) => {
  setup(name)
  const user = userEvent.setup()
  show('/w/DEMO/settings/addons')
  const title = name === 'publish' ? 'Publish' : 'Model routing'
  await user.click(await screen.findByRole('switch', { name: title }, T))
  const form = within(await screen.findByRole('dialog', { name: `Turn on ${title}` }, T))
  expect(form.getByRole('button', { name: 'Turn on' })).toBeDisabled()
  if (name === 'publish') {
    expect(form.getByText('Publish is signed in your terminal.')).toBeInTheDocument()
    await user.type(form.getByLabelText('Files are sent to'), 'https://files.example.test')
    await user.type(form.getByLabelText('Links open at'), 'https://links.example.test')
  } else {
    await user.selectOptions(await form.findByLabelText('Do the work', { selector: '#route-codex-work' }), 'gpt-5.4')
  }
  await user.click(form.getByRole('button', { name: 'Turn on' }))
  await waitFor(() => expect(posted()).toHaveLength(1), T)
  expect(posted()[0].body).toEqual({ action: 'addon_enable', name, ...(name === 'publish' ? { publish: { server: 'https://files.example.test', share: 'https://links.example.test' } } : { models: { codex: { work: 'gpt-5.4' } } }) })
})

it('set up but off uses the plain grant', async () => {
  setup('model-routing', false, true)
  const user = userEvent.setup()
  show('/w/DEMO/settings/addons')
  await user.click(await screen.findByRole('switch', { name: 'Model routing' }, T))
  expect(screen.queryByTestId('addon-model-routing-state')).toBeNull() // no state chip: the switch says it
  await waitFor(() => expect(posted()).toHaveLength(1), T)
  expect(posted()[0].body).toEqual({ action: 'addon_grant', name: 'model-routing' })
})

it.each(['model-routing', 'publish'] as const)('on %s without settings signs settings only', async (name) => {
  setup(name, true, false)
  const user = userEvent.setup()
  show('/w/DEMO/settings/addons')
  expect(await screen.findByTestId(`addon-${name}-state`, {}, T)).toHaveTextContent('Needs setup')
  await user.click(screen.getByRole('button', { name: 'Set up' }))
  const form = within(screen.getByRole('dialog'))
  if (name === 'publish') {
    await user.type(form.getByLabelText('Files are sent to'), 'https://files.example.test')
    await user.type(form.getByLabelText('Links open at'), 'https://links.example.test')
  } else {
    await user.selectOptions(await form.findByLabelText('Do the work', { selector: '#route-codex-work' }), 'gpt-5.4')
  }
  await user.click(form.getByRole('button', { name: 'Review and sign' }))
  await waitFor(() => expect(posted()).toHaveLength(1), T)
  expect(posted()[0].body).toEqual(name === 'publish' ? { action: 'publish_setup', server: 'https://files.example.test', share: 'https://links.example.test' } : { action: 'models_set', models: { codex: { work: 'gpt-5.4' } } })
})

it('on and set up keeps the section and turns off with the switch', async () => {
  setup('model-routing', true, true)
  const user = userEvent.setup()
  show('/w/DEMO/settings/addons')
  await user.click(await screen.findByTestId('addon-model-routing-expand', {}, T))
  expect(await screen.findByTestId('model-routing', {}, T)).toBeInTheDocument()
  await user.click(screen.getByRole('switch', { name: 'Model routing' }))
  await waitFor(() => expect(posted()).toHaveLength(1), T)
  expect(posted()[0].body).toEqual({ action: 'addon_disable', name: 'model-routing' })
})

it('a non-owner cannot open setup', async () => {
  setup('publish', true, false)
  state.host.workspaces[0].me = TOM
  state.host.workspaces[0].role = 'member'
  show('/w/DEMO/settings/addons')
  expect(await screen.findByRole('switch', { name: 'Publish' }, T)).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Set up' })).toBeNull()
})
