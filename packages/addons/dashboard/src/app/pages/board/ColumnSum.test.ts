import { describe, expect, it } from 'vitest'
import { withUnit } from './ColumnSum'

describe('withUnit', () => {
  it('pluralises pt except for exactly one', () => {
    expect(withUnit(13, 'pt')).toBe('13 pts')
    expect(withUnit(1, 'pt')).toBe('1 pt')
    expect(withUnit(0, 'pt')).toBe('0 pts')
    expect(withUnit(4, 'h')).toBe('4 h')
  })
})
