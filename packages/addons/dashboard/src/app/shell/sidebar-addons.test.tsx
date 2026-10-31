import { fireEvent, screen, within } from '@testing-library/react'
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
  fireEvent.click(nav.getByRole('button', { name: 'More' }))
  if (status === 'on') expect(await nav.findByRole('link', { name: 'Work copies' })).toBeInTheDocument()
  else expect(nav.queryByRole('link', { name: 'Work copies' })).not.toBeInTheDocument()
})

it('shows Schedules as soon as the workspace list says the page is served, with nothing else changing (R14b)', async () => {
  const { queryClient } = await import('@/production/Shell')
  await show('/')
  const nav = within(await screen.findByRole('navigation', { name: 'Main' }))
  expect(await nav.findByRole('link', { name: /^Today/ })).toBeInTheDocument()
  fireEvent.click(nav.getByRole('button', { name: 'More' }))
  expect(nav.queryByRole('link', { name: 'Schedules' })).not.toBeInTheDocument()
  state.host.workspaces[0].features = [...READS, 'worktrees', 'schedules']
  await queryClient.invalidateQueries({ queryKey: ['workspaces'] })
  expect(await nav.findByRole('link', { name: 'Schedules' })).toBeInTheDocument()
})


it('keeps only everyday links at the top level and preserves the secondary order', async () => {
  state.host.workspaces[0].features = [...READS, 'artifacts', 'agents', 'publish', 'terminals_page', 'usage', 'schedules']
  await show('/')
  const nav = within(await screen.findByRole('navigation', { name: 'Main' }))
  expect(nav.getAllByRole('link').map((a) => a.getAttribute('aria-label'))).toEqual(['Today', 'Board', 'Tickets', 'Shares'])
  fireEvent.click(nav.getByRole('button', { name: 'More' }))
  const more = nav.getByRole('group', { name: 'More' })
  expect(within(more).getAllByRole('link').map((a) => a.getAttribute('aria-label'))).toEqual([
    'Artifacts', 'Agents', 'Command window', 'Add-ons', 'Usage', 'Schedules', 'Guide', 'Settings', 'Feedback',
  ])
  expect(nav.queryByRole('link', { name: 'Test checklist' })).not.toBeInTheDocument()
  expect(localStorage.getItem(`orch.sidebar.more.${state.host.workspaces[0].id}`)).toBe('true')
  fireEvent.click(nav.getByRole('button', { name: 'More' }))
  expect(localStorage.getItem(`orch.sidebar.more.${state.host.workspaces[0].id}`)).toBe('false')
})

it('opens More on a nested secondary route even when the saved choice is closed', async () => {
  localStorage.setItem(`orch.sidebar.more.${state.host.workspaces[0].id}`, 'false')
  await show('/settings/general')
  const nav = within(await screen.findByRole('navigation', { name: 'Main' }))
  expect(nav.getByRole('button', { name: 'More' })).toHaveAttribute('aria-expanded', 'true')
  expect(within(nav.getByRole('group', { name: 'More' })).getByRole('link', { name: 'Settings' })).toBeVisible()
})

it('restores More for this workspace and uses its badge prefix for the avatar', async () => {
  state.host.workspaces[0].prefix = 'BT'
  localStorage.setItem(`orch.sidebar.more.${state.host.workspaces[0].id}`, 'true')
  await show('/')
  const nav = await screen.findByRole('navigation', { name: 'Main' })
  expect(within(nav).getByRole('button', { name: 'More' })).toHaveAttribute('aria-expanded', 'true')
  const sidebar = nav.closest('aside')!
  const expand = within(sidebar).queryByRole('button', { name: 'Expand sidebar' })
  if (expand) fireEvent.click(expand)
  const badge = sidebar.querySelector('[data-ws-prefix]')!
  const avatar = sidebar.querySelector('[data-slot="avatar-fallback"]')!
  expect(badge).toHaveTextContent('BT')
  expect(avatar.textContent).toBe(badge.textContent)
})

it('shows Test checklist only on a dev host', async () => {
  const { queryClient } = await import('@/production/Shell')
  queryClient.setQueryData(['host-info'], { dev: { sandbox: null }, alarms: [] })
  await show('/')
  const nav = within(await screen.findByRole('navigation', { name: 'Main' }))
  fireEvent.click(nav.getByRole('button', { name: 'More' }))
  expect(nav.getByRole('link', { name: 'Test checklist' })).toHaveAttribute('href', '/checklist')
})


it('does not restore another workspace’s More preference', async () => {
  localStorage.setItem(`orch.sidebar.more.${'b'.repeat(32)}`, 'true')
  await show('/')
  const nav = within(await screen.findByRole('navigation', { name: 'Main' }))
  expect(nav.getByRole('button', { name: 'More' })).toHaveAttribute('aria-expanded', 'false')
})
