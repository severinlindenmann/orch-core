import { screen, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'

vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const state = useFakeHost(() => [workspace({ id: 'a'.repeat(32), features: [...READS, 'worktrees'], addOns: { available: true, add_ons: [] } })])

it.each(['on', 'off', 'attention'] as const)('shows the Work copies page only when the Work copies add-on is on: %s', async (status) => {
  state.host.workspaces[0].addOns!.add_ons = [{ id: 'worktrees', title: 'Work copies', desc: '', state: status, locked: null, blocked: null }]
  await show('/')
  const nav = within(await screen.findByRole('navigation', { name: 'Main' }))
  if (status === 'on') expect(await nav.findByRole('link', { name: 'Work copies' })).toBeInTheDocument()
  else expect(nav.queryByRole('link', { name: 'Work copies' })).not.toBeInTheDocument()
})

it('shows Schedules as soon as the workspace list says the page is served, with nothing else changing (R14b)', async () => {
  const { queryClient } = await import('@/production/Shell')
  await show('/')
  const nav = within(await screen.findByRole('navigation', { name: 'Main' }))
  expect(await nav.findByRole('link', { name: /^Today/ })).toBeInTheDocument()
  expect(nav.queryByRole('link', { name: 'Schedules' })).not.toBeInTheDocument()
  state.host.workspaces[0].features = [...READS, 'worktrees', 'schedules']
  await queryClient.invalidateQueries({ queryKey: ['workspaces'] })
  expect(await nav.findByRole('link', { name: 'Schedules' })).toBeInTheDocument()
})
