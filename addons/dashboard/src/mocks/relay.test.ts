import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from './store'

const setup = (viewer = 'p_sev') => {
  const store = createMockStore({ persist: false })
  store.setViewer(viewer)
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
type S = ReturnType<typeof setup>
const fail = (p: Promise<unknown>) => p.then(() => 'ok', (e: { status: number; code: string }) => `${e.status} ${e.code}`)
const online = async (s: S) => {
  await s.api.postRelay(s.ws, { op: 'connect' })
  vi.advanceTimersByTime(2500)
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('relay state (simulated)', () => {
  it('starts off, with each member\'s devices, epoch 1 and the next 90-day rotation', async () => {
    const { api, ws } = setup()
    const r = await api.getRelay(ws)
    expect(r.simulated).toBe(true)
    expect(r.link).toBe('off')
    expect(r.devices.map((d) => d.label)).toEqual(["Severin's MacBook Pro", "Severin's iPhone", "Mara's MacBook Air", "Tom's iPad"])
    expect(r.devices.find((d) => d.this_device)).toMatchObject({ id: 'd_mac', primary: true, last_seen: r.now })
    expect(r.epoch).toBe(1)
    expect(r.next_rotation).toBe('2026-11-12T07:42:10Z')
    expect(r.queue.every((q) => q.state === 'queued')).toBe(true)
  })
  it('only shows devices of the workspace\'s members', async () => {
    const { store, api } = setup()
    const int = store.workspaces.find((w) => w.prefix === 'INT')!.id
    expect((await api.getRelay(int)).devices.map((d) => d.person)).not.toContain('p_tom')
  })
  it('connect: connecting for 2 s, then online; the queue drains one item every 1.5 s; stop', async () => {
    const s = setup()
    const r0 = await s.api.postRelay(s.ws, { op: 'connect' })
    expect(r0.link).toBe('connecting')
    expect(s.store.workspaces.find((w) => w.id === s.ws)!.relay).toBe('on')
    vi.advanceTimersByTime(2100)
    expect((await s.api.getRelay(s.ws)).link).toBe('online')
    vi.advanceTimersByTime(1900) // the mock clock has whole seconds: 4 s after connect, one item went out
    expect((await s.api.getRelay(s.ws)).queue.filter((q) => q.state === 'sent')).toHaveLength(1)
    vi.advanceTimersByTime(10_000)
    expect((await s.api.getRelay(s.ws)).queue.every((q) => q.state === 'sent')).toBe(true)
    expect((await s.api.postRelay(s.ws, { op: 'stop' })).link).toBe('stopped')
  })
  it('a simulated drop shows reconnecting for 4 s and comes back online', async () => {
    const s = setup()
    await online(s)
    expect((await s.api.simulateRelay(s.ws, { op: 'drop' })).link).toBe('reconnecting')
    vi.advanceTimersByTime(4100)
    expect((await s.api.getRelay(s.ws)).link).toBe('online')
  })
  it('hides a queued push about a ticket the viewer cannot see (DEMO-0044 is restricted to Severin and Mara)', async () => {
    const sev = setup('p_sev')
    expect((await sev.api.getRelay(sev.ws)).queue.some((q) => q.ticket === 'DEMO-0044')).toBe(true)
    const tom = setup('p_tom')
    const r = await tom.api.getRelay(tom.ws)
    expect(r.queue.some((q) => q.ticket === 'DEMO-0044' || q.label.includes('DEMO-0044'))).toBe(false)
    expect(r.queue.length).toBeGreaterThan(0)
  })
  it('only owners change it; members read it; outsiders get 403', async () => {
    const mara = setup('p_mara')
    expect((await mara.api.getRelay(mara.ws)).devices.length).toBe(4)
    expect(await fail(mara.api.postRelay(mara.ws, { op: 'connect' }))).toBe('403 forbidden')
    const tom = setup('p_tom')
    const int = tom.store.workspaces.find((w) => w.prefix === 'INT')!.id
    expect(await fail(tom.api.getRelay(int))).toBe('403 forbidden')
  })
})

describe('pairing a device', () => {
  it('needs the relay online', async () => {
    const s = setup()
    expect(await fail(s.api.postRelay(s.ws, { op: 'pair.start' }))).toBe('409 relay.offline')
  })
  it('code valid 10 minutes; the phone joins; both screens show the same 6 characters; the owner confirms; single use', async () => {
    const s = setup()
    await online(s)
    const r1 = await s.api.postRelay(s.ws, { op: 'pair.start' })
    const p = r1.pairing!
    expect(p.state).toBe('waiting')
    expect(Date.parse(p.expires_at) - Date.parse(p.started_at)).toBe(600_000)
    expect(await fail(s.api.postRelay(s.ws, { op: 'pair.confirm', pairing: p.id, fingerprint: 'XXXXXX' }))).toBe('409 pairing.waiting')
    const r2 = await s.api.simulateRelay(s.ws, { op: 'scan' })
    expect(r2.pairing).toMatchObject({ state: 'confirm', label: "Severin's iPad", platform: 'ipad' })
    expect(r2.pairing!.fingerprint).toMatch(/^[0-9A-HJKMNP-TV-Z]{6}$/)
    expect(await fail(s.api.postRelay(s.ws, { op: 'pair.confirm', pairing: p.id, fingerprint: 'ZZZZZZ' }))).toBe('409 pairing.mismatch')
    const r3 = await s.api.postRelay(s.ws, { op: 'pair.confirm', pairing: p.id, fingerprint: r2.pairing!.fingerprint! })
    expect(r3.pairing).toBeNull()
    const ipad = r3.devices.find((d) => d.label === "Severin's iPad")!
    expect(ipad.epoch).toBe(0) // the key is on its way
    expect(r3.queue.some((q) => q.kind === 'seal_key' && q.device === ipad.id)).toBe(true)
    vi.advanceTimersByTime(20_000)
    expect((await s.api.getRelay(s.ws)).devices.find((d) => d.id === ipad.id)!.epoch).toBe(1)
    expect(s.store.workspaces.find((w) => w.id === s.ws)!.members.find((m) => m.person === 'p_sev')!.devices).toBe(3)
    expect(await fail(s.api.postRelay(s.ws, { op: 'pair.confirm', pairing: p.id, fingerprint: r2.pairing!.fingerprint! }))).toBe('404 not_found')
  })
  it('an expired code cannot be scanned or confirmed', async () => {
    const s = setup()
    await online(s)
    const p = (await s.api.postRelay(s.ws, { op: 'pair.start' })).pairing!
    vi.advanceTimersByTime(600_001)
    expect((await s.api.getRelay(s.ws)).pairing!.state).toBe('expired')
    expect(await fail(s.api.simulateRelay(s.ws, { op: 'scan' }))).toBe('409 pairing.none')
    expect(await fail(s.api.postRelay(s.ws, { op: 'pair.confirm', pairing: p.id, fingerprint: 'AAAAAA' }))).toBe('409 pairing.expired')
  })
})

describe('removing a device', () => {
  it('starts a new epoch and seals its key to every remaining device through the queue', async () => {
    const s = setup()
    const r = await s.api.postRelay(s.ws, { op: 'device.remove', device: 'd_tom_ipad' })
    expect(r.devices.map((d) => d.id)).not.toContain('d_tom_ipad')
    expect(r.epoch).toBe(2)
    expect((await s.api.getIdentity(s.ws)).epoch).toBe(2)
    expect(r.queue.filter((q) => q.kind === 'seal_key').map((q) => q.device).sort()).toEqual(['d_mara_mba', 'd_sev_iphone'])
    expect(r.devices.find((d) => d.id === 'd_sev_iphone')!.epoch).toBe(1) // offline: still on the old key
    expect(s.store.wsEventsOf(s.ws).slice(-2).map((e) => e.type)).toEqual(['device.removed', 'epoch.rotated'])
    await online(s)
    vi.advanceTimersByTime(20_000)
    expect((await s.api.getRelay(s.ws)).devices.every((d) => d.epoch === 2)).toBe(true)
  })
  it('refuses this device and unknown devices', async () => {
    const s = setup()
    expect(await fail(s.api.postRelay(s.ws, { op: 'device.remove', device: 'd_mac' }))).toBe('409 relay.this_device')
    expect(await fail(s.api.postRelay(s.ws, { op: 'device.remove', device: 'd_nope' }))).toBe('404 not_found')
  })
})
