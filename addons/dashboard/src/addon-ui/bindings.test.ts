import { describe, expect, it } from 'vitest'
import { FORMATTERS, resolveBindings } from './bindings'

describe('$ticket path segment', () => {
  const ctx = { ticket: { key: 'DEMO-0041' }, addon: { byTicket: { 'DEMO-0041': [{ title: 'a' }], 'DEMO-0042': [{ title: 'b' }] } } }
  it('reads the current ticket key inside a $ref path', () => {
    expect(resolveBindings({ $ref: 'addon.byTicket.$ticket' }, ctx)).toEqual([{ title: 'a' }])
  })
  it('is null without a ticket', () => {
    expect(resolveBindings({ $ref: 'addon.byTicket.$ticket' }, { addon: ctx.addon })).toBeNull()
  })
})

describe('prototype guard after $ticket substitution', () => {
  const addon = { byTicket: { safe: 1 } }
  it.each(['__proto__', 'constructor'])('a ticket key of %s resolves to nothing', (key) => {
    expect(resolveBindings({ $ref: 'addon.byTicket.$ticket' }, { ticket: { key }, addon })).toBeNull()
    expect(resolveBindings('x${addon.byTicket.$ticket}y', { ticket: { key }, addon })).toBe('xy')
  })
})

describe('ktok formatter', () => {
  it.each([
    [0, '0k'],
    [84_000, '84k'],
    [999_499, '999k'],
    [999_600, '1.0 M'],
    [1_000_000, '1.0 M'],
    [2_400_000, '2.4 M'],
  ])('%d reads %s (no "1000k" at the boundary)', (n, text) => {
    expect(FORMATTERS.ktok(n)).toBe(text)
  })
})
