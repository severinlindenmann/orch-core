import { describe, expect, it } from 'vitest'
import { addressHint, running, sharesPrefix, uuid8, workspaceByAddress, workspaceOfTicket } from './workspaces'

describe('workspaceOfTicket', () => {
  const workspaces = [
    { id: 'w1', prefix: 'DEMO' },
    { id: 'w2', prefix: 'INT' },
    { id: 'w3', prefix: 'MY-TEAM' },
  ]
  it('finds the workspace by the key prefix', () => {
    expect(workspaceOfTicket('DEMO-0043', workspaces)?.id).toBe('w1')
    expect(workspaceOfTicket('INT-0007', workspaces)?.id).toBe('w2')
  })
  it('takes everything before the last dash as the prefix', () => {
    expect(workspaceOfTicket('MY-TEAM-0001', workspaces)?.id).toBe('w3')
  })
  it('is undefined for an unknown prefix or a malformed key (no fallback)', () => {
    expect(workspaceOfTicket('NOPE-0001', workspaces)).toBeUndefined()
    expect(workspaceOfTicket('', workspaces)).toBeUndefined()
    expect(workspaceOfTicket('DEMO', workspaces)).toBeUndefined()
  })
})

describe('workspaces that share a prefix (design 5: ?ws=<last 8 hex of the UUID>)', () => {
  const a = { id: 'aaaaaaaa-1111-2222-3333-444444444444', prefix: 'DEMO' }
  const b = { id: 'bbbbbbbbbbbbbbbbbbbbbbbbc0ffee00', prefix: 'Demo' }
  const other = { id: 'dddddddd' + '0'.repeat(24), prefix: 'INT' }
  const all = [a, b, other]

  it('uuid8 (WS8) is the LAST 8 hex of the workspace id, dashes ignored', () => {
    expect(uuid8(a.id)).toBe('44444444')
    expect(uuid8(b.id)).toBe('c0ffee00')
  })

  it('an address with a prefix that one workspace has finds it; case does not matter', () => {
    expect(workspaceByAddress(all, 'int')).toEqual({ found: other })
    expect(workspaceByAddress(all, 'NOPE')).toEqual({ missing: true })
  })

  it('a shared prefix needs the hint; without it, or with one that fits neither, the page asks', () => {
    expect(workspaceByAddress(all, 'DEMO', 'c0ffee00')).toEqual({ found: b })
    expect(workspaceByAddress(all, 'DEMO', '44444444')).toEqual({ found: a })
    expect(workspaceByAddress(all, 'DEMO')).toEqual({ ambiguous: [a, b] })
    expect(workspaceByAddress(all, 'DEMO', 'deadbeef')).toEqual({ ambiguous: [a, b] })
  })

  it('a hint that fits nothing is not guessed away when only one workspace has the prefix', () => {
    expect(workspaceByAddress(all, 'INT', 'deadbeef')).toEqual({ missing: true })
    expect(workspaceByAddress(all, 'INT', '00000000')).toEqual({ found: other })
  })

  it('only a shared prefix gets a hint in the links', () => {
    expect(addressHint(a, all)).toBe('44444444')
    // A link made with the FIRST 8 (the older form) names nothing: the page asks which workspace.
    expect(workspaceByAddress(all, 'DEMO', 'aaaaaaaa')).toEqual({ ambiguous: [a, b] })
    expect(addressHint(other, all)).toBeNull()
    expect(sharesPrefix(a, b)).toBe(true)
    expect(sharesPrefix(a, other)).toBe(false)
  })

  it('a ticket key names the workspace the person is in when the prefix is shared', () => {
    const two = [a, { ...b, prefix: 'DEMO' }, other]
    expect(workspaceOfTicket('DEMO-0001', two, b.id)).toBe(two[1])
    expect(workspaceOfTicket('DEMO-0001', two, a.id)).toBe(a)
    expect(workspaceOfTicket('DEMO-0001', two, 'unknown')).toBe(a)
    expect(workspaceOfTicket('INT-0001', two, b.id)).toBe(other)
  })

  it('a workspace whose host does not answer is not running', () => {
    expect(running([{ host_state: 'ok' }, {}, { host_state: 'host.unavailable' }]).length).toBe(2)
  })
})
