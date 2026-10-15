import { expect, it, vi } from 'vitest'
import { bestOfFive } from './bestOf'
it('warms once and measures five runs, ignoring a single machine stall', async () => {
  let clock = 0
  const times = [100, 80, 60, 40, 30, 20]
  const now = vi.spyOn(performance, 'now').mockImplementation(() => clock)
  const run = vi.fn(() => { clock += times.shift()! })
  try {
    expect(await bestOfFive(run)).toBe(20)
    expect(run).toHaveBeenCalledTimes(6)
  } finally { now.mockRestore() }
})
