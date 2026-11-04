import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import * as gesture from '@/components/sign/trustedPress'
import { READS, TOM, settingsOf, show, useFakeHost, workspace } from '@/test/hostHarness'

// The production app on a fake server: Settings, Add-ons, Model routing (a signed settings change, One click).
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
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, 'model_routing', 'click'], addOns: addOns(true), click: { custody: 'convenience', enabled: true, available: true } } as never)])
const route = (models: unknown = null, on = true) => {
  const ws = state.host.workspaces[0]
  ws.addOns = addOns(on) as never
  ws.settings = settingsOf({ models } as never)
  state.host.override = (path, method) => (path.endsWith('/model-routing') && method === 'GET' ? Response.json(CHOICES) : undefined)
}
const calls = (p: string) => state.host.calls.filter((c) => c.path.includes(p))
const posted = () => calls('/handoff').filter((c) => c.method === 'POST')
const prepared = () => state.host.clickPrepares().map((c) => c.body)
/** The press that opened the review was a real one (jsdom events are untrusted): the review prepares as it opens. */
const trusted = () => vi.spyOn(gesture, 'takeTrustedPress').mockReturnValue(true)
afterEach(() => vi.restoreAllMocks())

const setup = (name: 'publish', on = false, setUp: boolean | null = false, suggest: string | null = null) => {
  route(null, on)
  state.host.workspaces[0].addOns = { available: true, add_ons: [{ id: name, title: 'Publish', desc: 'Settings for this workspace.', state: on ? 'on' : 'off', locked: null, blocked: null, needs: 'publish', set_up: setUp, suggest }] } as never
}

it.each(['publish'] as const)('off %s opens settings; Turn on prepares one enable action with One click', async (name) => {
  setup(name)
  const user = userEvent.setup()
  show('/w/DEMO/addons')
  const title = name === 'publish' ? 'Publish' : 'Model routing'
  await user.click(await screen.findByRole('switch', { name: title }, T))
  const form = within(await screen.findByRole('dialog', { name: `Turn on ${title}` }, T))
  expect(form.getByRole('button', { name: 'Turn on and sign' })).toBeDisabled()
  expect(screen.getByRole('dialog', { name: `Turn on ${title}` })).not.toHaveTextContent(/terminal/i)
  await user.type(form.getByLabelText('Publish server address'), 'https://files.example.test')
  await user.click(form.getByRole('button', { name: 'Links open at a different address' }))
  await user.type(form.getByLabelText('Links open at'), 'https://links.example.test')
  trusted()
  await user.click(form.getByRole('button', { name: 'Turn on and sign' }))
  await screen.findByTestId('click-panel', {}, T)
  expect(prepared()).toEqual([{ action: 'addon_enable', name, publish: { server: 'https://files.example.test', share: 'https://links.example.test' } }])
  expect(posted()).toEqual([]) // no hand-off is ever prepared
})

it('one valid address enables Turn on and signs server equal to share; a bad address shows a message', async () => {
  setup('publish')
  const user = userEvent.setup()
  show('/w/DEMO/addons')
  await user.click(await screen.findByRole('switch', { name: 'Publish' }, T))
  const form = within(await screen.findByRole('dialog', { name: 'Turn on Publish' }, T))
  expect(form.queryByLabelText('Links open at')).toBeNull()
  await user.type(form.getByLabelText('Publish server address'), 'http://evil.example')
  expect(form.getByRole('alert')).toHaveTextContent('must start with https://')
  expect(form.getByRole('button', { name: 'Turn on and sign' })).toBeDisabled()
  await user.clear(form.getByLabelText('Publish server address'))
  await user.type(form.getByLabelText('Publish server address'), 'http://127.0.0.1:8102')
  expect(form.getByRole('button', { name: 'Turn on and sign' })).toBeEnabled()
  // P04: no trusted press needed: the pressed button opens the review at once (prepare only), and nothing else stacks on it
  await user.click(form.getByRole('button', { name: 'Turn on and sign' }))
  await screen.findByTestId('click-panel', {}, T)
  expect(screen.queryByRole('button', { name: 'Turn on and sign' })).toBeNull()
  expect(screen.queryByRole('dialog', { name: 'Turn on Publish' })).toBeNull() // no empty second dialog, no Turn on to press again
  expect(state.host.clickSigned).toEqual([]) // never signed by itself
  expect(prepared()).toEqual([{ action: 'addon_enable', name: 'publish', publish: { server: 'http://127.0.0.1:8102', share: 'http://127.0.0.1:8102' } }])
})

it('a suggested address makes Turn on one click, and Change shows the field', async () => {
  setup('publish', false, false, 'http://127.0.0.1:8102')
  const user = userEvent.setup()
  show('/w/DEMO/addons')
  await user.click(await screen.findByRole('switch', { name: 'Publish' }, T))
  const form = within(await screen.findByRole('dialog', { name: 'Turn on Publish' }, T))
  expect(form.queryByText(/http:\/\/127\.0\.0\.1:8102/)).toBeNull() // the address is behind Details
  await user.click(form.getByRole('button', { name: 'Details' }))
  expect(await screen.findByTestId('info-drawer-content')).toHaveTextContent('Files and links use the Publish server on this computer: http://127.0.0.1:8102')
  await user.keyboard('{Escape}')
  expect(form.queryByLabelText('Publish server address')).toBeNull()
  expect(form.getByRole('button', { name: 'Turn on and sign' })).toBeEnabled()
  trusted()
  await user.click(form.getByRole('button', { name: 'Turn on and sign' }))
  await screen.findByTestId('click-panel', {}, T)
  expect(prepared()).toEqual([{ action: 'addon_enable', name: 'publish', publish: { server: 'http://127.0.0.1:8102', share: 'http://127.0.0.1:8102' } }])
})

it('Change on a suggested address reveals the field, filled with the suggestion', async () => {
  setup('publish', false, false, 'http://127.0.0.1:8102')
  const user = userEvent.setup()
  show('/w/DEMO/addons')
  await user.click(await screen.findByRole('switch', { name: 'Publish' }, T))
  const form = within(await screen.findByRole('dialog', { name: 'Turn on Publish' }, T))
  await user.click(form.getByRole('button', { name: 'Details' }))
  await user.click(await screen.findByRole('button', { name: 'Change' }))
  expect(form.getByLabelText('Publish server address')).toHaveValue('http://127.0.0.1:8102')
})

it('#1838 the pair: a suggest_where with different server and share signs both, with no typing', async () => {
  setup('publish', false, false, 'https://pub.example.org')
  const row = state.host.workspaces[0].addOns as unknown as { add_ons: Record<string, unknown>[] }
  Object.assign(row.add_ons[0], { suggest_where: { server: 'https://pub.example.org', share: 'https://share.example.org' }, suggest_from: 'hub', suggest_hub: 'Anna’s hub' })
  const user = userEvent.setup()
  show('/w/DEMO/addons')
  await user.click(await screen.findByRole('switch', { name: 'Publish' }, T))
  const form = within(await screen.findByRole('dialog', { name: 'Turn on Publish' }, T))
  expect(form.queryByLabelText('Publish server address')).toBeNull() // the pair is the suggestion: no field
  await user.click(form.getByRole('button', { name: 'Details' }))
  expect(await screen.findByTestId('info-drawer-content')).toHaveTextContent('Links open at share.example.org')
  await user.keyboard('{Escape}')
  trusted()
  await user.click(form.getByRole('button', { name: 'Turn on and sign' }))
  await screen.findByTestId('click-panel', {}, T)
  expect(prepared()).toEqual([{ action: 'addon_enable', name: 'publish', publish: { server: 'https://pub.example.org', share: 'https://share.example.org' } }])
})

it('#1838 suggest_where wins over a different legacy suggest', async () => {
  setup('publish', false, false, 'http://127.0.0.1:1')
  const row = state.host.workspaces[0].addOns as unknown as { add_ons: Record<string, unknown>[] }
  Object.assign(row.add_ons[0], { suggest_where: { server: 'https://pub.example.org', share: 'https://share.example.org' }, suggest_from: 'hub' })
  const user = userEvent.setup()
  show('/w/DEMO/addons')
  await user.click(await screen.findByRole('switch', { name: 'Publish' }, T))
  const form = within(await screen.findByRole('dialog', { name: 'Turn on Publish' }, T))
  trusted()
  await user.click(form.getByRole('button', { name: 'Turn on and sign' }))
  await screen.findByTestId('click-panel', {}, T)
  expect(prepared()).toEqual([{ action: 'addon_enable', name: 'publish', publish: { server: 'https://pub.example.org', share: 'https://share.example.org' } }])
})

it('with nothing suggested the field shows and has no example in it', async () => {
  setup('publish')
  const user = userEvent.setup()
  show('/w/DEMO/addons')
  await user.click(await screen.findByRole('switch', { name: 'Publish' }, T))
  const form = within(await screen.findByRole('dialog', { name: 'Turn on Publish' }, T))
  expect(form.getByLabelText('Publish server address')).not.toHaveAttribute('placeholder')
  expect(form.getByRole('button', { name: 'Turn on and sign' })).toBeDisabled()
})

it.each(['publish'] as const)('on %s without settings signs settings only', async (name) => {
  setup(name, true, false)
  const user = userEvent.setup()
  show('/w/DEMO/addons')
  expect(await screen.findByTestId(`addon-${name}-state`, {}, T)).toHaveTextContent('Needs setup')
  await user.click(screen.getByRole('button', { name: 'Set up' }))
  const form = within(screen.getByRole('dialog'))
  await user.type(form.getByLabelText('Publish server address'), 'https://files.example.test')
  trusted()
  await user.click(form.getByRole('button', { name: 'Review and sign' }))
  await screen.findByTestId('click-panel', {}, T)
  expect(prepared()).toEqual([{ action: 'publish_setup', server: 'https://files.example.test', share: 'https://files.example.test' }])
  expect(posted()).toEqual([])
})

it('a non-owner cannot open setup', async () => {
  setup('publish', true, false)
  state.host.workspaces[0].me = TOM
  state.host.workspaces[0].role = 'member'
  show('/w/DEMO/addons')
  expect(await screen.findByRole('switch', { name: 'Publish' }, T)).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Set up' })).toBeNull()
})
