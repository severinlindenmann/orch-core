import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ARM_MS, CLICK_RISK } from '@/api/click'
import * as gesture from './trustedPress'
import { SignPopover } from './SignPopover'

// P03: the preselected "let Claude Code open this ticket's folder" line of a Claude Code start's sign card.
const role = { current: 'owner' as 'owner' | 'member' }
vi.mock('@/app/workspace', () => ({ useWorkspace: () => ({ workspaces: [{ id: 'w', prefix: 'TEST', name: 'Test', click: { enabled: true, available: true, custody: 'convenience' }, members: [{ person: 'p_test', role: role.current }] }] }) }))
vi.mock('@/app/useMe', () => ({ useMe: () => ({ data: { person: 'p_test' } }) }))
vi.mock('@tanstack/react-router', () => ({ useRouter: () => null, Link: () => null }))
vi.mock('./HubSignPanel', () => ({ HubAddonPanel: () => null, HubSettingsPanel: () => null, HubSignPanel: () => null, NotFromHub: () => null }))
const T = { timeout: 5000 }
const trust = (over: object = {}) => ({ available: true, on: false, config: 'missing' as const, offered: true, why_not: '', parent_config: '', agent_needs_consent: true, agent_why_not: '', ...over })
let order: string[]
beforeEach(() => {
  order = []
  role.current = 'owner'
  vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([])
  vi.spyOn(api, 'prepareClick').mockResolvedValue({ request_id: 'hr_1', expires_at: '2099-01-01T00:00:00Z', op: 'close', confirm: 'click' })
  vi.spyOn(api, 'viewClick').mockResolvedValue({ view_digest: 'sha256:abc', view: { risk: CLICK_RISK, op: 'close', workspace: { id: 'w', prefix: 'TEST', name: 'Test' }, fingerprint: '0123456789ab', lines: ['Close task'], checks: [], confirm: { kind: 'click' }, expires_at: '2099-01-01T00:00:00Z', auth: 'convenience', device: 'd_test', artifacts: null } })
  vi.spyOn(api, 'signClick').mockImplementation(async () => { order.push('signed'); return { state: 'submitted', duplicate: false, key: 'TEST-0001', seq: 1 } })
  vi.spyOn(api, 'cancelClick').mockResolvedValue({ id: 'hr_1', state: 'cancelled' })
})
afterEach(async () => { cleanup(); await new Promise((r) => setTimeout(r, 20)); vi.restoreAllMocks() })

async function review(props: { claudeCode?: boolean } = { claudeCode: true }) {
  const onSettled = vi.fn(() => { order.push('settled') })
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SignPopover ws="w" title="Close task" action={{ action: 'close', key: 'TEST-0001' }} inline onClose={() => {}} onSettled={onSettled} {...props} /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Close task' }, T))
  const panel = await screen.findByTestId('click-panel', {}, T)
  return { panel, onSettled }
}
async function sign(panel: HTMLElement) {
  const b = within(panel).getByRole('button', { name: /^Sign( |$)/ })
  await waitFor(() => expect(b).toBeEnabled(), { timeout: ARM_MS + 2000 })
  fireEvent.pointerDown(b)
  fireEvent.click(b)
}

it('shows the line ticked and records the consent once, after the signature and before the caller starts', async () => {
  vi.spyOn(api, 'getClaudeTrust').mockResolvedValue(trust())
  const grant = vi.spyOn(api, 'grantClaudeTrust').mockImplementation(async () => { order.push('consent'); return { on: true } })
  const { panel, onSettled } = await review()
  const line = await screen.findByTestId('claude-consent', {}, T)
  expect(line).toHaveTextContent("Let Claude Code open this ticket's folder without asking")
  expect(within(line).getByRole('checkbox')).toBeChecked()
  await sign(panel)
  await waitFor(() => expect(onSettled).toHaveBeenCalledTimes(1), T)
  expect(grant).toHaveBeenCalledTimes(1)
  expect(grant).toHaveBeenCalledWith('w', false) // never "trust anyway"
  expect(order).toEqual(['signed', 'consent', 'settled'])
})

it('an unticked line records nothing, and the start still goes on', async () => {
  vi.spyOn(api, 'getClaudeTrust').mockResolvedValue(trust())
  const grant = vi.spyOn(api, 'grantClaudeTrust').mockResolvedValue({ on: true })
  const { panel, onSettled } = await review()
  fireEvent.click(within(await screen.findByTestId('claude-consent', {}, T)).getByRole('checkbox'))
  await sign(panel)
  await waitFor(() => expect(onSettled).toHaveBeenCalledTimes(1), T)
  expect(grant).not.toHaveBeenCalled()
})

it('a refused consent never stops the start', async () => {
  vi.spyOn(api, 'getClaudeTrust').mockResolvedValue(trust())
  vi.spyOn(api, 'grantClaudeTrust').mockRejectedValue(new Error('Found /x/.claude in this folder'))
  const { panel, onSettled } = await review()
  await screen.findByTestId('claude-consent', {}, T)
  await sign(panel)
  await waitFor(() => expect(onSettled).toHaveBeenCalledTimes(1), T)
})

it('shows no line when the consent is already given', async () => {
  const get = vi.spyOn(api, 'getClaudeTrust').mockResolvedValue(trust({ agent_needs_consent: false }))
  const grant = vi.spyOn(api, 'grantClaudeTrust')
  const { panel, onSettled } = await review()
  await waitFor(() => expect(get).toHaveBeenCalled(), T)
  expect(screen.queryByTestId('claude-consent')).toBeNull()
  await sign(panel)
  await waitFor(() => expect(onSettled).toHaveBeenCalledTimes(1), T)
  expect(grant).not.toHaveBeenCalled()
})

it('says the plain reason instead of the line when the host cannot give the consent', async () => {
  vi.spyOn(api, 'getClaudeTrust').mockResolvedValue(trust({ agent_needs_consent: false, agent_why_not: 'Found /x/.claude in this folder, so Claude Code will ask about this folder itself.' }))
  const grant = vi.spyOn(api, 'grantClaudeTrust')
  const { panel, onSettled } = await review()
  expect(await screen.findByTestId('claude-consent-refused', {}, T)).toHaveTextContent('Found /x/.claude in this folder')
  expect(screen.queryByRole('checkbox')).toBeNull()
  await sign(panel)
  await waitFor(() => expect(onSettled).toHaveBeenCalledTimes(1), T)
  expect(grant).not.toHaveBeenCalled()
})

it('asks nothing for another agent app, and nothing of a person who is not an owner', async () => {
  const get = vi.spyOn(api, 'getClaudeTrust').mockResolvedValue(trust())
  await review({ claudeCode: false })
  expect(screen.queryByTestId('claude-consent')).toBeNull()
  cleanup()
  role.current = 'member'
  await review({ claudeCode: true })
  expect(screen.queryByTestId('claude-consent')).toBeNull()
  expect(get).not.toHaveBeenCalled()
})
