import { describe, expect, it, vi } from 'vitest'
import { createMockStore } from './store'

describe('simulator', () => {
  it('plays steps in order on the clock and moves the cursor', () => {
    vi.useFakeTimers()
    const s = createMockStore({ persist: false })
    const ws = s.workspaceOf('DEMO-0043')!.id
    const before = s.cursor(ws)
    s.sim.play('demo', [
      { afterMs: 1000, run: (st) => st.append('DEMO-0043', { type: 'log.added', text: 'step 1' }) },
      { afterMs: 1000, run: (st) => st.append('DEMO-0043', { type: 'log.added', text: 'step 2' }) },
    ])
    vi.advanceTimersByTime(1000)
    expect(s.cursor(ws)).toBe(before + 1)
    vi.advanceTimersByTime(1000)
    expect(s.eventsOf('DEMO-0043').at(-1)?.text).toBe('step 2')
    vi.useRealTimers()
  })
  it('reset stops running scripts', () => {
    vi.useFakeTimers()
    const s = createMockStore({ persist: false })
    s.sim.play('x', [{ afterMs: 500, run: (st) => st.append('DEMO-0043', { type: 'log.added', text: 'late' }) }])
    s.reset()
    vi.advanceTimersByTime(1000)
    expect(s.eventsOf('DEMO-0043').some((e) => e.text === 'late')).toBe(false)
    vi.useRealTimers()
  })
  it('a step that throws ends its script: running() no longer lists it and the id can be played again', () => {
    vi.useFakeTimers()
    const s = createMockStore({ persist: false })
    s.sim.play('boom', [
      { afterMs: 100, run: () => { throw new Error('step failed') } },
      { afterMs: 100, run: (st) => st.append('DEMO-0043', { type: 'log.added', text: 'never' }) },
    ])
    expect(() => vi.advanceTimersByTime(100)).toThrow('step failed')
    expect(s.sim.running()).not.toContain('boom')
    vi.advanceTimersByTime(500)
    expect(s.eventsOf('DEMO-0043').some((e) => e.text === 'never')).toBe(false)
    s.sim.play('boom', [{ afterMs: 100, run: (st) => st.append('DEMO-0043', { type: 'log.added', text: 'again' }) }])
    expect(s.sim.running()).toContain('boom')
    vi.advanceTimersByTime(100)
    expect(s.eventsOf('DEMO-0043').at(-1)?.text).toBe('again')
    expect(s.sim.running()).not.toContain('boom')
    vi.useRealTimers()
  })
})
