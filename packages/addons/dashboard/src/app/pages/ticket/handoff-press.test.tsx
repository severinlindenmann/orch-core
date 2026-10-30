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

// P10: Pass is preselected only when every requirement is checked, and a preselected verdict is never started by itself.
type Prop = { autoStart?: boolean; action?: { action: string; outcome?: string } | null; hint?: string | null; confirmLabel?: string }
const withAc = (states: ('proven' | 'unproven')[]) =>
  ({ ...ticket, acceptance_state: states.map((state, i) => ({ id: `AC${i + 1}`, text: `Req ${i + 1}`, state, evidence: [{ kind: 'task', ref: 'T1' }] })), tasks_state: [], artifacts: [], branch: {} }) as unknown as TicketDocument
const showT = (t: TicketDocument) => <QueryClientProvider client={new QueryClient()}><HandoffSignDialog ticket={t} action={{ kind: 'verdict' }} ws="w" onClose={() => {}} /></QueryClientProvider>
it('verdict: Pass is preselected when every requirement is checked, and nothing starts on its own', () => {
  render(showT(withAc(['proven'])))
  const p = last() as Prop
  expect(p.action).toMatchObject({ action: 'verdict', outcome: 'pass' })
  expect(p.confirmLabel).toBe('Pass')
  expect(p.autoStart).toBeUndefined() // the person still presses; the dialog arms only on that press
})
it('verdict: nothing is preselected when a requirement is not checked; the hint says what to do', () => {
  render(showT(withAc(['proven', 'unproven'])))
  const p = last() as Prop
  expect(p.action).toBeNull()
  expect(p.hint).toBe('Pick Pass or Send back to continue')
})

// R11 P09: the combined approval asks the signer for ONE sequence request (never two separate approvals).
it('approve_both prepares one approve_both request bound to the head the page showed, and arms only on a press', () => {
  const t = { ...ticket, title: 'Add a hello.txt', head: { seq: 7, hash: 'h' } } as unknown as TicketDocument
  const r = render(<QueryClientProvider client={new QueryClient()}><HandoffSignDialog ticket={t} action={{ kind: 'approve_both', pressed: true }} ws="w" onClose={() => {}} /></QueryClientProvider>)
  const p = last() as unknown as { action: unknown; title: string; autoStart: boolean; confirmLabel: string }
  expect(p.action).toEqual({ action: 'approve_both', key: 'T-1', head_seq: 7 })
  expect(p.title).toBe('Approve requirements and plan: Add a hello.txt')
  expect(p.autoStart).toBe(true)
  r.unmount()
  render(<QueryClientProvider client={new QueryClient()}><HandoffSignDialog ticket={t} action={{ kind: 'approve_both' }} ws="w" onClose={() => {}} /></QueryClientProvider>)
  expect((last() as unknown as { autoStart: boolean }).autoStart).toBe(false)
})

// A combined approval that cannot be prepared is no dead end: the dialog closes and the two single approvals are offered.
it('approve_both: when the request cannot be prepared the dialog closes and the combined card is blocked for the ticket', async () => {
  const { bothIsBlocked, unblockBoth } = await import('./actions')
  const onClose = vi.fn()
  const t = { ...ticket, key: 'T-FB', head: { seq: 7, hash: 'h' } } as unknown as TicketDocument
  render(<QueryClientProvider client={new QueryClient()}><HandoffSignDialog ticket={t} action={{ kind: 'approve_both' }} ws="w" onClose={onClose} /></QueryClientProvider>)
  expect(bothIsBlocked('T-FB')).toBe(false)
  ;(last() as unknown as { onPrepareFailed: () => void }).onPrepareFailed()
  expect(bothIsBlocked('T-FB')).toBe(true)
  expect(onClose).toHaveBeenCalled()
  unblockBoth('T-FB')
})
it('a failed prepare of another action does not block the combined card', async () => {
  const { bothIsBlocked } = await import('./actions')
  render(show({ kind: 'approve', gate: 'plan' }))
  ;(last() as unknown as { onPrepareFailed: () => void }).onPrepareFailed()
  expect(bothIsBlocked('T-1')).toBe(false)
})

// R14 P04: the dialog is mounted before it opens; Pass is derived when the verdict opens, and a pick of the person wins.
it('verdict: mounted closed, then opened on a fully proven ticket -> Pass is preselected, nothing started', () => {
  const t = withAc(['proven'])
  const view = (action: HumanAction | null) => <QueryClientProvider client={new QueryClient()}><HandoffSignDialog ticket={t} action={action} ws="w" onClose={() => {}} /></QueryClientProvider>
  const r = render(view(null))
  expect(seen.props.length).toBe(0)
  r.rerender(view({ kind: 'verdict' }))
  const p = last() as Prop
  expect(p.action).toMatchObject({ action: 'verdict', outcome: 'pass' })
  expect(p.confirmLabel).toBe('Pass')
  expect(p.autoStart).toBeUndefined()
})
it('verdict: mounted closed, then opened with an unproven requirement -> nothing preselected', () => {
  const t = withAc(['proven', 'unproven'])
  const r = render(<QueryClientProvider client={new QueryClient()}><HandoffSignDialog ticket={t} action={null} ws="w" onClose={() => {}} /></QueryClientProvider>)
  r.rerender(<QueryClientProvider client={new QueryClient()}><HandoffSignDialog ticket={t} action={{ kind: 'verdict' }} ws="w" onClose={() => {}} /></QueryClientProvider>)
  expect((last() as Prop).action).toBeNull()
})
