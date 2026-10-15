import { describe, expect, it } from 'vitest'
import { sameTerms, validTerms } from '@/api/addons'
import { openDecisions } from '@/mocks/addons/registry'
import type { AddonDecision } from '@/api/types'

// Decision terms (proposal for the addon contract): plain values core shows line by line, checks and records.
describe('decision terms', () => {
  it('accepts at most 12 plain values under arg-like keys; anything else fails closed', () => {
    expect(validTerms(undefined)).toBe(true)
    expect(validTerms({ peer: 'INT', expires_after: '90 days', n: 3 })).toBe(true)
    expect(validTerms({})).toBe(false)
    expect(validTerms({ 'bad key': 'x' })).toBe(false)
    expect(validTerms({ nested: { a: 1 } })).toBe(false)
    expect(validTerms({ n: Number.NaN })).toBe(false)
    expect(validTerms(Object.fromEntries(Array.from({ length: 13 }, (_, i) => [`k${i}`, i])))).toBe(false)
  })
  it('the signed terms must equal the decision\'s terms now: same keys, same values, same types', () => {
    const now = { peer: 'INT', days: 90 }
    expect(sameTerms({ peer: 'INT', days: 90 }, now)).toBe(true)
    expect(sameTerms({ peer: 'INT', days: '90' }, now)).toBe(false)
    expect(sameTerms({ peer: 'INT' }, now)).toBe(false)
    expect(sameTerms({ peer: 'INT', days: 90, extra: 1 }, now)).toBe(false)
    expect(sameTerms(undefined, now)).toBe(false)
    expect(sameTerms(undefined, undefined)).toBe(true)
    expect(sameTerms({ peer: 'INT' }, undefined)).toBe(false)
  })
  it('a decision whose terms core cannot show is not offered', () => {
    const d = (id: string, terms?: unknown) => ({ kind: 'decision', id, addon: 'x', title: 't', question: 'q', options: [{ key: 'a', label: 'A' }], action: 'decide', terms }) as AddonDecision
    const open = openDecisions(undefined, {}, [d('ok', { a: 1 }), d('none'), d('bad', { a: { b: 1 } })], {} as never)
    expect(open.map((x) => x.id)).toEqual(['ok', 'none'])
  })
})
