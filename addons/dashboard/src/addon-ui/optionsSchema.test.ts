import { describe, expect, it } from 'vitest'
import { RESERVED_KEYS } from './actionRuntime'
import { parseOptions } from './optionsSchema'

const field = (over: Record<string, unknown> = {}) => ({ key: 'days', label: 'Works for', default: 7, choices: [{ value: 1, label: '1 day' }, { value: 7, label: '7 days' }], ...over })

describe('parseOptions (core checks the package\'s options)', () => {
  it('accepts a valid object', () => {
    expect(parseOptions({ fields: [field()] })?.fields[0].default).toBe(7)
  })
  it('refuses missing or empty fields', () => {
    expect(parseOptions(undefined)).toBeNull()
    expect(parseOptions({})).toBeNull()
    expect(parseOptions({ fields: [] })).toBeNull()
  })
  it('refuses duplicate field keys and duplicate choices', () => {
    expect(parseOptions({ fields: [field(), field()] })).toBeNull()
    expect(parseOptions({ fields: [field({ choices: [{ value: 1, label: 'a' }, { value: 1, label: 'b' }] })] })).toBeNull()
  })
  it('refuses more than 6 fields and more than 20 choices', () => {
    expect(parseOptions({ fields: Array.from({ length: 7 }, (_, i) => field({ key: `k${i}` })) })).toBeNull()
    expect(parseOptions({ fields: [field({ choices: Array.from({ length: 21 }, (_, i) => ({ value: i, label: `c${i}` })), default: 0 })] })).toBeNull()
    expect(parseOptions({ fields: Array.from({ length: 6 }, (_, i) => field({ key: `k${i}` })) })).not.toBeNull()
  })
  it('bounds string lengths and key shape', () => {
    expect(parseOptions({ fields: [field({ label: 'x'.repeat(81) })] })).toBeNull()
    expect(parseOptions({ fields: [field({ key: 'bad key' })] })).toBeNull()
    expect(parseOptions({ note: 'x'.repeat(201), fields: [field()] })).toBeNull()
    expect(parseOptions({ fields: [field({ choices: [{ value: 'v'.repeat(61), label: 'a' }], default: 'v' })] })).toBeNull()
  })
  it('refuses a field keyed like an arg core sets itself (it would be stripped, never sent)', () => {
    expect(RESERVED_KEYS).toEqual(expect.arrayContaining(['ticket', 'confirmed', 'launch', 'ws']))
    for (const key of RESERVED_KEYS) expect(parseOptions({ fields: [field({ key })] }), key).toBeNull()
  })
  it('coerces a default that is not a choice to the first choice', () => {
    expect(parseOptions({ fields: [field({ default: 99 })] })?.fields[0].default).toBe(1)
  })
})
