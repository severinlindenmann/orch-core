import { describe, expect, it } from 'vitest'
import { createDropFixture, dropItem } from './dropFixture'

describe('retire preconditions', () => {
  it.each([
    { expect_version: 2, expect_head: 'demo-head-current' },
    { expect_version: 3, expect_head: 'demo-head-stale' },
  ])('refuses stale list values: %j', async (expected) => {
    const item = dropItem({ version: 3, head: 'demo-head-current' })
    const fixture = createDropFixture({ received: [item] })
    await expect(fixture.api.retire('w', { object_id: item.object_id, ...expected })).rejects.toMatchObject({ code: 'drop.version_conflict' })
    expect(fixture.items('received')[0]).toEqual(item)
  })

  it('retires matching nullable heads and sets a 30-day tombstone deadline', async () => {
    const item = dropItem({ head: null })
    const fixture = createDropFixture({ received: [item] })
    const before = Date.now()
    await fixture.api.retire('w', { object_id: item.object_id, expect_version: item.version, expect_head: null })
    const retired = fixture.items('received')[0]
    expect(retired.state).toBe('retired')
    expect(retired.tombstone_until_ms).toBeGreaterThanOrEqual(before + 30 * 86_400_000)
    expect(retired.tombstone_until_ms).toBeLessThanOrEqual(Date.now() + 30 * 86_400_000)
  })
})
