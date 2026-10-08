import { describe, expect, it } from 'vitest'
import { resolveBindings } from './bindings'

describe('$ticket path segment', () => {
  const ctx = { ticket: { key: 'DEMO-0041' }, addon: { byTicket: { 'DEMO-0041': [{ title: 'a' }], 'DEMO-0042': [{ title: 'b' }] } } }
  it('reads the current ticket key inside a $ref path', () => {
    expect(resolveBindings({ $ref: 'addon.byTicket.$ticket' }, ctx)).toEqual([{ title: 'a' }])
  })
  it('is null without a ticket', () => {
    expect(resolveBindings({ $ref: 'addon.byTicket.$ticket' }, { addon: ctx.addon })).toBeNull()
  })
})
