import { describe, expect, it } from 'vitest'
import {
  MAX_CERT_BYTES,
  PUBLIC_KEY,
  canAddRole,
  canChangeRoles,
  canChangeSettings,
  canRemoveMember,
  canRevokeHostGrant,
  canonicalPolicy,
  certProblem,
  inRange,
  keyText,
  policyProblem,
  samePolicy,
  type HostPolicy,
} from './settings'

const policy = (over: Partial<HostPolicy> = {}): HostPolicy => ({ approvers: ['owner'], count: 1, not: [], applies: 'all', independent: false, ...over })

describe('who may sign what (the host stays the authority)', () => {
  it('settings, policies and roles are owners only', () => {
    expect(canChangeSettings('owner')).toBe(true)
    for (const r of ['maintainer', 'member', 'viewer', undefined] as const) {
      expect(canChangeSettings(r)).toBe(false)
      expect(canChangeRoles(r)).toBe(false)
    }
  })
  it('owners add and remove anyone; maintainers only members and viewers; nobody else', () => {
    for (const role of ['owner', 'maintainer', 'member', 'viewer'] as const) {
      expect(canAddRole('owner', role)).toBe(true)
      expect(canRemoveMember('owner', role)).toBe(true)
    }
    expect(canAddRole('maintainer', 'member')).toBe(true)
    expect(canAddRole('maintainer', 'viewer')).toBe(true)
    expect(canAddRole('maintainer', 'maintainer')).toBe(false)
    expect(canRemoveMember('maintainer', 'owner')).toBe(false)
    expect(canAddRole('member', 'viewer')).toBe(false)
    expect(canAddRole('viewer', 'viewer')).toBe(false)
    expect(canAddRole(undefined, 'viewer')).toBe(false)
  })
  it('owners revoke any grant, others their own, a viewer none', () => {
    expect(canRevokeHostGrant('owner', 'p_x', 'p_me')).toBe(true)
    expect(canRevokeHostGrant('maintainer', 'p_me', 'p_me')).toBe(true)
    expect(canRevokeHostGrant('maintainer', 'p_x', 'p_me')).toBe(false)
    expect(canRevokeHostGrant('member', 'p_me', 'p_me')).toBe(true)
    expect(canRevokeHostGrant('viewer', 'p_me', 'p_me')).toBe(false)
    expect(canRevokeHostGrant(undefined, 'p_me', 'p_me')).toBe(false)
  })
})

describe('gate policies', () => {
  it('the canonical form sorts and removes repeats, and compares equal regardless of order', () => {
    const p = policy({ approvers: ['owner', 'maintainer', 'owner'], not: ['watchers', 'assignees'], applies: ['feature', 'bug'] })
    expect(canonicalPolicy(p)).toEqual({ approvers: ['maintainer', 'owner'], count: 1, not: ['assignees', 'watchers'], applies: ['bug', 'feature'], independent: false })
    expect(samePolicy(p, policy({ approvers: ['maintainer', 'owner'], not: ['assignees', 'watchers'], applies: ['bug', 'feature'] }))).toBe(true)
    expect(samePolicy(p, policy({ approvers: ['owner'] }))).toBe(false)
  })
  it('a policy needs an approver, a count of 1 to 9, and a type when it applies to types', () => {
    expect(policyProblem('plan', policy())).toBeNull()
    expect(policyProblem('plan', policy({ approvers: [] }))).toMatch(/at least one kind of approver/)
    expect(policyProblem('plan', policy({ count: 0 }))).toMatch(/1 to 9/)
    expect(policyProblem('plan', policy({ count: 10 }))).toMatch(/1 to 9/)
    expect(policyProblem('plan', policy({ count: Number.NaN }))).toMatch(/1 to 9/)
    expect(policyProblem('plan', policy({ applies: [] }))).toMatch(/at least one ticket type/)
  })
  it('the code review is never approved by the assignees and is always independent', () => {
    expect(policyProblem('code', policy({ not: ['assignees'], independent: true }))).toBeNull()
    expect(policyProblem('code', policy({ independent: true }))).toMatch(/never approved by the assignees/)
    expect(policyProblem('code', policy({ not: ['assignees'], independent: false }))).toMatch(/always independent/)
    expect(policyProblem('verify', policy())).toBeNull()
  })
})

describe('the invitee\'s files', () => {
  it('a public key is 87 base64url characters; a trailing newline of a file is trimmed, nothing else', () => {
    const key = 'B' + 'x'.repeat(86)
    expect(PUBLIC_KEY.test(key)).toBe(true)
    expect(PUBLIC_KEY.test(key + '=')).toBe(false)
    expect(PUBLIC_KEY.test(key.slice(1))).toBe(false)
    expect(keyText(`\n ${key}\n`)).toBe(key)
    expect(keyText('a b')).toBe('a b')
  })
  it('a certificate is a JSON object under 16 KiB', () => {
    expect(certProblem('{"o":{}}')).toBeNull()
    expect(certProblem('[]')).toMatch(/JSON object/)
    expect(certProblem('"x"')).toMatch(/JSON object/)
    expect(certProblem('null')).toMatch(/JSON object/)
    expect(certProblem('{nope')).toMatch(/not JSON/)
    expect(certProblem('{"a":"' + 'x'.repeat(MAX_CERT_BYTES) + '"}')).toMatch(/larger than 16 KiB/)
  })
  it('durations are whole numbers in range', () => {
    expect(inRange(24, 1, 24)).toBe(true)
    expect(inRange(25, 1, 24)).toBe(false)
    expect(inRange(1.5, 1, 24)).toBe(false)
    expect(inRange(Number.NaN, 1, 24)).toBe(false)
  })
})
