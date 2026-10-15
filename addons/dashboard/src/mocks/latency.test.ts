import { describe, expect, it, vi } from 'vitest'
import { BASE_MS, BURST_MS, createLatency, JITTER, readLatencySettings } from './latency'

const store = () => {
  const m = new Map<string, string>()
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k) }
}

describe('mock latency (G4)', () => {
  it('requests in one burst share one delay and resolve together; a later request waits on its own', async () => {
    vi.useFakeTimers()
    let now = 0
    const delay = createLatency({ base: 120, jitter: false }, { now: () => now, random: () => 0.5 })
    const done: string[] = []
    const a = delay().then(() => done.push('a'))
    now = BURST_MS - 1
    const b = delay().then(() => done.push('b'))
    await vi.advanceTimersByTimeAsync(30)
    now = BURST_MS + 30
    const c = delay().then(() => done.push('c'))
    await vi.advanceTimersByTimeAsync(89)
    expect(done).toEqual([])
    await vi.advanceTimersByTimeAsync(1)
    await Promise.all([a, b])
    expect(done).toEqual(['a', 'b']) // the same moment
    await vi.advanceTimersByTimeAsync(30)
    await c
    expect(done).toEqual(['a', 'b', 'c'])
    vi.useRealTimers()
  })

  it('with jitter every request waits on its own, 120-300 ms', async () => {
    vi.useFakeTimers()
    const randoms = [0, 1]
    const delay = createLatency({ base: 120, jitter: true }, { now: () => 0, random: () => randoms.shift() ?? 0 })
    const done: number[] = []
    void delay().then(() => done.push(1))
    void delay().then(() => done.push(2))
    await vi.advanceTimersByTimeAsync(JITTER.min)
    expect(done).toEqual([1])
    await vi.advanceTimersByTimeAsync(JITTER.spread)
    expect(done).toEqual([1, 2])
    vi.useRealTimers()
  })

  it('reads the dev switches from the address and keeps them for the tab', () => {
    const s = store()
    expect(readLatencySettings('', s)).toEqual({ base: BASE_MS, jitter: false })
    expect(readLatencySettings('?jitter=1', s)).toEqual({ base: BASE_MS, jitter: true })
    expect(readLatencySettings('', s).jitter).toBe(true) // remembered
    expect(readLatencySettings('?jitter=0', s).jitter).toBe(false)
    expect(readLatencySettings('?latency=0', s).base).toBe(0)
    expect(readLatencySettings('', s).base).toBe(0)
    expect(readLatencySettings('?latency=', s).base).toBe(BASE_MS)
    expect(readLatencySettings('?latency=abc', s).base).toBe(BASE_MS)
  })
})
