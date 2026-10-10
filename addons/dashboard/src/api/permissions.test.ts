import { describe, expect, it } from 'vitest'
import type { Role } from './types'
import { atLeast, can, canRevokeGrant, roleOf, type Permission } from './permissions'

const ROLES: (Role | undefined)[] = ['owner', 'maintainer', 'member', 'viewer', undefined]

// Rows: permission; columns: owner, maintainer, member, viewer, not a member.
const TABLE: [Permission, boolean[]][] = [
  ['ticket.create', [true, true, true, false, false]],
  ['ticket.act', [true, true, true, false, false]],
  ['question.answer.any', [true, false, false, false, false]],
  ['ticket.move', [true, true, false, false, false]],
  ['ticket.label', [true, true, false, false, false]],
  ['view.share', [true, true, true, false, false]],
  ['grant.issue', [true, true, true, false, false]],
  ['grant.revoke.any', [true, false, false, false, false]],
  ['settings', [true, false, false, false, false]],
  ['addon.manage', [true, false, false, false, false]],
  ['addon.decide', [true, true, false, false, false]],
  ['addon.action', [true, true, true, false, false]],
]

describe('can(role, permission)', () => {
  for (const [perm, row] of TABLE) {
    it.each(ROLES.map((r, i) => [r ?? 'none', r, row[i]] as const))(`${perm}: %s -> %s`, (_label, role, expected) => {
      expect(can(role, perm)).toBe(expected)
    })
  }
})

describe('atLeast', () => {
  it('ranks owner > maintainer > member > viewer; no role meets nothing', () => {
    expect(atLeast('owner', 'maintainer')).toBe(true)
    expect(atLeast('maintainer', 'maintainer')).toBe(true)
    expect(atLeast('member', 'maintainer')).toBe(false)
    expect(atLeast('viewer', 'viewer')).toBe(true)
    expect(atLeast(undefined, 'viewer')).toBe(false)
  })
})

describe('canRevokeGrant', () => {
  it('owners revoke any grant, maintainers and members their own, viewers none', () => {
    expect(canRevokeGrant('owner', 'p_mara', 'p_sev')).toBe(true)
    expect(canRevokeGrant('maintainer', 'p_mara', 'p_mara')).toBe(true)
    expect(canRevokeGrant('maintainer', 'p_sev', 'p_mara')).toBe(false)
    expect(canRevokeGrant('member', 'p_tom', 'p_tom')).toBe(true)
    expect(canRevokeGrant('member', 'p_sev', 'p_tom')).toBe(false)
    expect(canRevokeGrant('viewer', 'p_tom', 'p_tom')).toBe(false)
  })
})

describe('roleOf', () => {
  it("reads a person's role from the workspace members", () => {
    const ws = { members: [{ person: 'p_sev', name: 'Severin', role: 'owner' as const }] }
    expect(roleOf(ws, 'p_sev')).toBe('owner')
    expect(roleOf(ws, 'p_x')).toBeUndefined()
    expect(roleOf(undefined, 'p_sev')).toBeUndefined()
  })
})
