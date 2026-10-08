import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import addons from '@/mocks/fixtures/addons.json'
import { getAddon } from './index'

const setup = (viewer: string) => {
  const store = createMockStore({ persist: false })
  store.setViewer(viewer)
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws }
}
const status = (p: Promise<unknown>) => p.then(() => 200, (e: { status: number }) => e.status)

describe('action minimum roles are declared once, in the package manifest', () => {
  it('every addon declares save_settings as owner-only', () => {
    for (const name of ['publish', 'github', 'usage', 'terminals', 'wiki', 'estimate']) {
      const pkg = addons.find((a) => a.name === name) as unknown as { actions?: Record<string, { minRole: string }> }
      expect(pkg.actions?.save_settings.minRole, `${name}.save_settings`).toBe('owner')
    }
    const publish = addons.find((a) => a.name === 'publish') as unknown as { actions: Record<string, { minRole: string }> }
    expect(publish.actions.decide.minRole).toBe('maintainer')
  })
  it('mock modules carry no role of their own', () => {
    for (const name of ['publish', 'github', 'usage', 'terminals', 'wiki', 'estimate']) {
      for (const [id, a] of Object.entries(getAddon(name)!.actions)) expect(typeof a, `${name}.${id}`).toBe('function')
    }
  })
  it('the store enforces the manifest: save_settings owner-only, decide maintainer, others member', async () => {
    const mara = setup('p_mara') // maintainer
    expect(await status(mara.api.runAddonAction(mara.ws, 'estimate', 'save_settings', { formData: {} }))).toBe(403)
    expect(await status(mara.api.runAddonAction(mara.ws, 'publish', 'decide', { id: 'nope', option: 'no' }))).toBe(200)
    expect(await status(mara.api.runAddonAction(mara.ws, 'publish', 'share', { ticket: 'DEMO-0041' }))).toBe(200)
    const sev = setup('p_sev')
    expect(await status(sev.api.runAddonAction(sev.ws, 'estimate', 'save_settings', { formData: {} }))).toBe(200)
    const tom = setup('p_tom') // viewer
    expect(await status(tom.api.runAddonAction(tom.ws, 'publish', 'share', { ticket: 'DEMO-0041' }))).toBe(403)
    expect(await status(tom.api.runAddonAction(tom.ws, 'publish', 'decide', { id: 'x' }))).toBe(403)
  })
})
