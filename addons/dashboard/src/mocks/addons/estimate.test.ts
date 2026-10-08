import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'

const setup = (viewer = 'p_sev') => {
  const store = createMockStore({ persist: false })
  store.setViewer(viewer)
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws }
}
type Schema = { type: string; enum: (string | number)[] }
type State = { settings: { scale: string }; pointsSchema: { properties: { points: Schema } } }
const est = (s: ReturnType<typeof setup>) => s.api.getAddonState(s.ws, 'estimate') as unknown as Promise<State>

describe('estimate addon', () => {
  it('offers the fibonacci-style numbers on the default scale', async () => {
    const s = setup()
    expect((await est(s)).pointsSchema.properties.points.enum).toEqual([1, 2, 3, 5, 8, 13, 21])
  })
  it('linear scale offers 1 to 10, t-shirt offers XS..XL', async () => {
    const s = setup()
    await s.api.runAddonAction(s.ws, 'estimate', 'save_settings', { formData: { scale: 'linear' } })
    expect((await est(s)).pointsSchema.properties.points.enum).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    await s.api.runAddonAction(s.ws, 'estimate', 'save_settings', { formData: { scale: 't-shirt' } })
    const p = (await est(s)).pointsSchema.properties.points
    expect(p.enum).toEqual(['XS', 'S', 'M', 'L', 'XL'])
    expect(p.type).toBe('string')
  })
  it('set stores the points and a numeric weight on the ticket', async () => {
    const s = setup()
    await s.api.runAddonAction(s.ws, 'estimate', 'set', { ticket: 'DEMO-0043', formData: { points: 8 } })
    expect(s.store.ticket('DEMO-0043')!.addons.estimate).toMatchObject({ points: 8, weight: 8 })
  })
  it('t-shirt sizes keep their label and weigh XS=1 S=2 M=3 L=5 XL=8', async () => {
    const s = setup()
    await s.api.runAddonAction(s.ws, 'estimate', 'save_settings', { formData: { scale: 't-shirt' } })
    await s.api.runAddonAction(s.ws, 'estimate', 'set', { ticket: 'DEMO-0043', formData: { points: 'L' } })
    expect(s.store.ticket('DEMO-0043')!.addons.estimate).toMatchObject({ points: 'L', weight: 5 })
  })
  it('refuses a value that is not on the current scale and changes nothing', async () => {
    const s = setup()
    const before = JSON.stringify(s.store.ticket('DEMO-0043')!.addons.estimate)
    const r = await s.api.runAddonAction(s.ws, 'estimate', 'set', { ticket: 'DEMO-0043', formData: { points: 'M' } })
    expect(r.message).toMatch(/not on the fibonacci scale/)
    expect(r.changed).toBeFalsy()
    expect(JSON.stringify(s.store.ticket('DEMO-0043')!.addons.estimate)).toBe(before)
  })
})
