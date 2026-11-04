import { afterEach, expect, it, vi } from 'vitest'
import { fetchRail, hubUrl } from '@/api/switch'
import { mockStore } from '@/api/client'
import { setHubScenario } from './hub'

afterEach(() => { setHubScenario(null); vi.unstubAllEnvs() })
it('Open hub resolves through the mock router to an http(s) mockup URL', async () => {
  setHubScenario('selective')
  const ws = mockStore.workspaces[0].id
  const url = new URL(await hubUrl(ws, 'open'))
  expect(url.protocol).toMatch(/^https?:$/)
  expect(url.href).toBe('http://127.0.0.1:8787/hub/index.html#home')
  expect((await fetchRail(ws)).hub?.name).toBe('Company hub')
  expect((await fetchRail(ws)).workspaces.find((w) => w.current)?.workspace_id).toBe(ws)
})
it('honors the configured mockup and requested hash page', async () => {
  setHubScenario('full')
  vi.stubEnv('VITE_HUB_MOCKUP_URL', 'https://mock.example.test/hub/index.html')
  expect(await hubUrl(mockStore.workspaces[0].id, 'open', '/spaces')).toBe('https://mock.example.test/hub/index.html#spaces')
  expect(await hubUrl(mockStore.workspaces[0].id, 'signout')).toBe('https://mock.example.test/hub/index.html#signed-out')
})
it('standalone keeps a null hub', async () => {
  setHubScenario('standalone')
  expect(await fetchRail(mockStore.workspaces[0].id)).toEqual({ hub: null, workspaces: [] })
})
