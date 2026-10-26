import { describe, expect, it } from 'vitest'
import { sameSet } from './addons'

describe('sameSet', () => {
  it('compares as sets, ignoring order and duplicates', () => {
    expect(sameSet(['a', 'b'], ['b', 'a'])).toBe(true)
    expect(sameSet(['a', 'a', 'b'], ['a', 'b'])).toBe(true)
    expect(sameSet(['launch', 'launch'], ['launch', 'pty'])).toBe(false)
    expect(sameSet(['launch', 'pty'], ['launch', 'launch'])).toBe(false)
    expect(sameSet([], [])).toBe(true)
  })
})
