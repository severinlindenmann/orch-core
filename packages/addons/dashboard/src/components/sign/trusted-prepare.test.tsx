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
function showWith(props: Record<string, unknown>, children?: unknown) {
  return <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SignPopover ws="w" title="Close task" action={{ action: 'close', key: 'TEST-0001' }} inline onClose={() => {}} {...props}>{children as never}</SignPopover></QueryClientProvider>
}
it('P06: autoStart with array-of-false children (approve) prepares without a second press and shows the review', async () => {
  const { prepare } = mount()
  cleanup()
  render(showWith({ autoStart: true }, [false, false]))
  await waitFor(() => expect(prepare).toHaveBeenCalledTimes(1))
  expect(await screen.findByTestId('click-panel')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Close task' })).toBeNull()
})
it('P06: without autoStart (or with false) nothing prepares; the idle box stays', async () => {
  const { prepare } = mount()
  cleanup()
  const first = render(showWith({}, [false]))
  await Promise.resolve()
  first.unmount()
  render(showWith({ autoStart: false }, [false]))
  await Promise.resolve()
  expect(prepare).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Close task' })).toBeInTheDocument()
})
it('P06: a real form (children) never auto-starts, even with autoStart', async () => {
  const { prepare } = mount()
  cleanup()
  render(showWith({ autoStart: true }, <input aria-label="note" />))
  await Promise.resolve()
  expect(prepare).not.toHaveBeenCalled()
})
it('P06: autoStart alone never arms the countdown; with armOnStart the review arms, and nothing signs while it loads', async () => {
  const { prepare, sign } = mount()
  cleanup()
  const plain = render(showWith({ autoStart: true }))
  await screen.findByTestId('click-panel')
  expect(screen.queryByTestId('sign-arm-status')).toBeNull()
  expect(sign).not.toHaveBeenCalled()
  plain.unmount()
  vi.spyOn(api, 'viewClick').mockResolvedValue({ view_digest: 'sha256:abc', view: { headline: 'Close TEST-0001', risk_lines: [], risk: CLICK_RISK, op: 'close', workspace: { id: 'w', prefix: 'TEST', name: 'Test' }, fingerprint: '0123456789ab', lines: ['Close task'], checks: [], confirm: { kind: 'click' }, expires_at: '2099-01-01T00:00:00Z', auth: 'convenience', device: 'd_test', artifacts: null } as never })
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  sign.mockResolvedValue({ state: 'submitted', duplicate: false, key: 'k' } as never)
  render(showWith({ autoStart: true, armOnStart: true }))
  expect(sign).not.toHaveBeenCalled() // still loading
  await screen.findByTestId('click-panel')
  await waitFor(() => expect(screen.queryByTestId('sign-arm-status')).not.toBeNull())
  expect(prepare).toHaveBeenCalledTimes(2)
  expect(sign).not.toHaveBeenCalled() // the countdown has not run out
  await waitFor(() => expect(sign).toHaveBeenCalledTimes(1), { timeout: 2500 }) // and it signs by itself after ~1 s
})
it('P06: a remount without autoStart (reload) never prepares', async () => {
  const { prepare, rerender } = mount()
  rerender(showWith({}))
  await Promise.resolve()
  expect(prepare).not.toHaveBeenCalled()
})
it('r10-P09: prepareOnOpen opens the review without any press, never armed, and signs nothing', async () => {
  const { prepare, sign } = mount()
  cleanup()
  render(showWith({ prepareOnOpen: true, autoStart: false }))
  await waitFor(() => expect(prepare).toHaveBeenCalledTimes(1))
  await screen.findByTestId('click-panel')
  expect(screen.queryByTestId('sign-arm-status')).toBeNull()
  expect(sign).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: 'Close task' })).toBeNull()
})
it('r10-P09: prepareOnOpen still never prepares a real form', async () => {
  const { prepare } = mount()
  cleanup()
  render(showWith({ prepareOnOpen: true }, <input aria-label="note" />))
  await Promise.resolve()
  expect(prepare).not.toHaveBeenCalled()
})
