import { expect, it } from 'vitest'
import { queries as q } from './queries'

const interval = (data: unknown) => {
  const fn = q.ticket('w', 'T-1').refetchInterval as (x: { state: { data: unknown } }) => number | false
  return fn({ state: { data } })
}

it('an open ticket is read again every 30 s while an agent holds it, not otherwise (P12)', () => {
  expect(interval({ claim: { agent: 'a', live: true } })).toBe(30_000)
  expect(interval({ claim: { agent: 'a' } })).toBe(30_000)
  expect(interval({ claim: { agent: 'a', live: false } })).toBe(false)
  expect(interval({ claim: null })).toBe(false)
  expect(interval(undefined)).toBe(false)
})
