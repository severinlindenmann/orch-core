import { describe, expect, it } from 'vitest'
import { createMockStore } from '@/mocks/store'
import { roleMeets } from './roles'

describe('roleMeets', () => {
  const table: [string, string, boolean][] = [
    ['owner', 'owner', true], ['maintainer', 'owner', false], ['member', 'owner', false], ['viewer', 'owner', false],
    ['owner', 'maintainer', true], ['maintainer', 'maintainer', true], ['member', 'maintainer', false], ['viewer', 'maintainer', false],
    ['owner', 'reviewers', false], ['maintainer', 'reviewers', false], ['member', 'reviewers', false], ['viewer', 'reviewers', false],
  ]
  it.each(table)('%s vs approvers %s -> %s', (role, approvers, expected) => {
    expect(roleMeets(role, approvers)).toBe(expected)
  })
})

describe('store.canApprove with approvers "maintainer"', () => {
  it('lets owners and maintainers approve, not members or viewers', () => {
    const store = createMockStore({ persist: false })
    const ws = store.workspaces[0].id
    store.appendWs(ws, { type: 'gate.policy_set', gate: 'verify', approvers: 'maintainer', count: 1, not: null })
    store.appendWs(ws, { type: 'member.added', person: 'p_mem', name: 'Mem', role: 'member' })
    const t = store.ticket('DEMO-0043')!
    const why = (p: string) => store.canApprove(t, 'verify', p)
    expect(why('p_sev')).toBeNull()
    expect(why('p_mara')).toBeNull()
    expect(why('p_mem')).toMatch(/Only an owner or a maintainer/)
    expect(why('p_tom')).toBe('Viewers cannot approve.')
  })
})
