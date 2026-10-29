import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { TicketDocument } from '@/api/types'
import { HandoffSignDialog } from './HandoffSignDialog'
import type { HumanAction } from './shared'

const seen = vi.hoisted(() => ({ props: [] as { autoStart?: boolean; onPending?: (p: boolean) => void }[] }))
vi.mock('@/components/sign/SignPopover', () => ({ SignPopover: (p: { autoStart?: boolean; onPending?: (p: boolean) => void }) => { seen.props.push(p); return null } }))
vi.mock('@tanstack/react-router', () => ({ useNavigate: () => vi.fn() }))
vi.mock('./shared', async (orig) => ({ ...(await orig<typeof import('./shared')>()), useViewer: () => ({ name: (x: string) => x }) }))
afterEach(() => { cleanup(); seen.props = [] })
const ticket = { key: 'T-1', gates: { plan: { hash: null }, requirements: { hash: null }, code: { hash: null }, verify: { hash: null } }, questions_state: [], acceptance_state: [] } as unknown as TicketDocument
const show = (action: HumanAction | null) => <QueryClientProvider client={new QueryClient()}><HandoffSignDialog ticket={ticket} action={action} ws="w" onClose={() => {}} /></QueryClientProvider>
const last = () => seen.props[seen.props.length - 1]

it('a pressed approve opens the review once; a later render or remount never re-arms', () => {
  const action: HumanAction = { kind: 'approve', gate: 'plan', pressed: true }
  const r = render(show(action))
  expect(last().autoStart).toBe(true)
  last().onPending?.(true) // the review started: the press is spent
  r.rerender(show(action))
  expect(last().autoStart).toBe(false)
})
it('approve without a press (menu item, palette) never depends on the ambient flag', () => {
  render(show({ kind: 'approve', gate: 'plan' }))
  expect(last().autoStart).toBe(false)
  cleanup()
  render(show({ kind: 'approve', gate: 'plan', pressed: false }))
  expect(last().autoStart).toBe(false)
})
