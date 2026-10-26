import { screen, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'

vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const state = useFakeHost(() => [workspace({ id: 'a'.repeat(32), features: [...READS, 'worktrees'], addOns: { available: true, add_ons: [] } })])

it.each(['on', 'off', 'attention'] as const)('shows Ticket copies only when Worktrees is on: %s', async (status) => {
  state.host.workspaces[0].addOns!.add_ons = [{ id: 'worktrees', title: 'Worktrees', desc: '', state: status, locked: null, blocked: null }]
  await show('/')
  const nav = within(await screen.findByRole('navigation', { name: 'Main' }))
  if (status === 'on') expect(await nav.findByRole('link', { name: 'Ticket copies' })).toBeInTheDocument()
  else expect(nav.queryByRole('link', { name: 'Ticket copies' })).not.toBeInTheDocument()
  expect(nav.queryByRole('link', { name: 'Worktrees' })).not.toBeInTheDocument()
})
