import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { CLICK_RISK } from '@/api/click'
import * as gesture from './trustedPress'
import { SignPopover } from './SignPopover'
vi.mock('@/app/workspace', () => ({ useWorkspace: () => ({ workspaces: [{ id: 'w', prefix: 'TEST', name: 'Test', click: { enabled: true, available: true, custody: 'convenience' } }] }) }))
vi.mock('@/app/useMe', () => ({ useMe: () => ({ data: { person: 'p_test' } }) }))
vi.mock('@tanstack/react-router', () => ({ useRouter: () => null, Link: () => null }))
vi.mock('./HubSignPanel', () => ({ HubAddonPanel: () => null, HubSettingsPanel: () => null, HubSignPanel: () => null, NotFromHub: () => null }))
afterEach(() => { cleanup(); vi.restoreAllMocks() })
function mount() {
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([])
  const prepare = vi.spyOn(api, 'prepareClick').mockResolvedValue({ request_id: 'hr_1', expires_at: '2099-01-01T00:00:00Z', op: 'close', confirm: 'click' })
  vi.spyOn(api, 'viewClick').mockResolvedValue({ view_digest: 'sha256:abc', view: { risk: CLICK_RISK, op: 'close', workspace: { id: 'w', prefix: 'TEST', name: 'Test' }, fingerprint: '0123456789ab', lines: ['Close task'], checks: [], confirm: { kind: 'click' }, expires_at: '2099-01-01T00:00:00Z', auth: 'convenience', device: 'd_test', artifacts: null } })
  const sign = vi.spyOn(api, 'signClick')
  vi.spyOn(api, 'cancelClick').mockResolvedValue({ id: 'hr_1', state: 'cancelled' })
  const show = () => <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SignPopover ws="w" title="Close task" action={{ action: 'close', key: 'TEST-0001' }} inline onClose={() => {}} /></QueryClientProvider>
  const rendered = render(show())
  return { prepare, sign, ...rendered, show }
}
it('render, rerender, and synthetic clicks cannot prepare', async () => {
  const { prepare, rerender, show } = mount()
  rerender(show())
  fireEvent.click(screen.getByRole('button', { name: 'Close task' }))
  await Promise.resolve()
  expect(prepare).not.toHaveBeenCalled()
  expect(document.body).not.toHaveTextContent('orch sign')
})
it('the trusted outcome press prepares once and never bypasses the arm', async () => {
  const { prepare, sign } = mount()
  vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
  fireEvent.click(screen.getByRole('button', { name: 'Close task' }))
  await waitFor(() => expect(prepare).toHaveBeenCalledTimes(1))
  await screen.findByTestId('click-panel')
  expect(sign).not.toHaveBeenCalled()
  expect(document.body).not.toHaveTextContent('orch sign')
})
it('only trusted non-repeat gestures qualify', () => {
  expect(gesture.trustedPress({ isTrusted: false })).toBe(false)
  expect(gesture.trustedPress({ isTrusted: true, repeat: true })).toBe(false)
  expect(gesture.trustedPress({ isTrusted: true, repeat: false })).toBe(true)
})
