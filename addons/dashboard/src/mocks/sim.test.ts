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
})
