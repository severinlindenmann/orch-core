import { afterEach, expect, it } from 'vitest'
import { hasFeature, setFeatures } from '@/api/features'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '../store'
import { getAddon } from './index'

afterEach(() => setFeatures(null))

it.each(['normal', 'busy'] as const)('focus is off by default in %s', async (dataset) => {
  setFeatures(null)
  const store = createMockStore({ persist: false, dataset })
  const api = createApi(createMockTransport(store, { latency: false }))
  expect(getAddon('focus')).toBeDefined()
  expect(store.addons.find((addon) => addon.name === 'focus')?.capabilities).toEqual([])
  await api.getWorkspaces()
  expect(hasFeature('focus')).toBe(false)
  for (const workspace of store.workspaces) expect(hasFeature('focus', workspace.id)).toBe(false)
})

it('focus appears only after an owner grants and enables it, and disappears on disable', async () => {
  setFeatures(null)
  const store = createMockStore({ persist: false })
  store.setViewer('p_sev')
  const api = createApi(createMockTransport(store, { latency: false }))
  const ws = store.workspaces[0].id
  await api.postAddonOp(ws, 'focus', { op: 'install' })
  await api.getWorkspaces()
  expect(hasFeature('focus', ws)).toBe(false)
  const addon = (await api.getWorkspaceAddons(ws)).find((addon) => addon.name === 'focus')!
  await api.postAddonOp(ws, 'focus', { op: 'grant', version: addon.ws.version, package_sha256: addon.ws.package_sha256, capabilities: [], viewer_actions: [], enable: true })
  await api.getWorkspaces()
  expect(hasFeature('focus', ws)).toBe(true)
  expect(hasFeature('focus', store.workspaces[1].id)).toBe(false)
  await api.postAddonOp(ws, 'focus', { op: 'disable' })
  await api.getWorkspaces()
  expect(hasFeature('focus', ws)).toBe(false)
})
