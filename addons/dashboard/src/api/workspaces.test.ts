import { describe, expect, it } from 'vitest'
import { workspaceOfTicket } from './workspaces'

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
