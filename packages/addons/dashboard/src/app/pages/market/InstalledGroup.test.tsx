import { configure, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'
import { market, installedAddon } from '@/test/fixtures/market'
// Allow the route and signing review to settle on busy development machines.
configure({ asyncUtilTimeout: 15000 })
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async original => ({ ...await original<typeof import('@/api/useLiveUpdates')>(), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))
const state = useFakeHost(() => [workspace({ id: 'a'.repeat(32), features: [...READS, 'actions', 'market'], market: { markets: [market()] }, marketInstalled: { addons: [] } })])
it.each([false, true])('puts Built in above From marketplaces (marketplace installed: %s)', async installed => {
  if (installed) state.host.workspaces[0].marketInstalled = { addons: [installedAddon()] }
  show('/w/DEMO/addons?tab=installed')
  const builtin = await screen.findByRole('heading', { name: 'Built in' })
  const marketplaces = screen.getByRole('heading', { name: 'From marketplaces' })
  expect(builtin.compareDocumentPosition(marketplaces) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  for (const name of ['Browse', 'Installed', 'Marketplaces']) expect(screen.getByRole('button', { name })).toBeVisible()
  if (!installed) expect(screen.getByText('Nothing installed from a marketplace yet.')).toBeVisible()
}, 30000)
