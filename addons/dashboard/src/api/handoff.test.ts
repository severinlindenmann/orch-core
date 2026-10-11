import { describe, expect, it } from 'vitest'
import { IDEM, cj, expiryMs, fingerprintOf, newIdem, requestIsConsistent, reviewDigestOf, signCommand, type HandoffEvent, type HandoffRequest } from './handoff'
import vector from '@/test/fixtures/handoff/vector.json'

const events = vector.events as unknown as HandoffEvent[]

describe('the fingerprint is the one `orch sign` computes (host contract v1.4 section 0.8.1)', () => {
  it('canonical JSON matches the Python reference on the restricted subset', () => {
    expect(cj(vector.cj_input)).toBe(vector.cj_output)
    expect(() => cj({ n: 1.5 })).toThrow()
    expect(() => cj({ n: 2 ** 60 })).toThrow()
  })

  it('review_digest and the 12-hex fingerprint equal the Python vectors', () => {
    expect(reviewDigestOf(vector.material)).toBe(vector.review_digest)
    expect(fingerprintOf(vector.review_digest, events)).toBe(vector.fingerprint)
  })

  it('binding the device and the factor does not change it; the decision does', () => {
    const bound = [{ ...events[0], actor: { ...events[0].actor, device: 'd_' + '1'.repeat(16) }, auth: 'passphrase', sig: 'x' }]
    expect(fingerprintOf(vector.review_digest, bound as HandoffEvent[])).toBe(vector.fingerprint_bound)
    expect(fingerprintOf(vector.review_digest, [{ ...events[0], gate: 'requirements' }])).toBe(vector.fingerprint_other_gate)
    expect(vector.fingerprint_other_gate).not.toBe(vector.fingerprint)
  })

  it('a request is consistent only when both hashes recompute from its own bytes', () => {
    const request = { material: vector.material, events, review_digest: vector.review_digest, fingerprint: vector.fingerprint, expires_at: new Date(Date.now() + 600_000).toISOString().replace(/\.\d+Z$/, 'Z') } as unknown as HandoffRequest
    expect(requestIsConsistent(request)).toBe(true)
    expect(requestIsConsistent({ ...request, fingerprint: '0'.repeat(12) })).toBe(false)
    expect(requestIsConsistent({ ...request, review_digest: 'sha256:' + '0'.repeat(64) })).toBe(false)
    const text = structuredClone(vector.material) as typeof vector.material
    text.input.tasks[0].text = 'Do something else'
    expect(requestIsConsistent({ ...request, material: text as never })).toBe(false)
    expect(requestIsConsistent({ ...request, events: [{ ...events[0], gate: 'requirements' }] })).toBe(false)
    expect(requestIsConsistent({ ...request, material: { bad: 1.5 } as never })).toBe(false)
  })
})

describe('idempotency keys and the command', () => {
  it('an idem has the host shape and is fresh each time', () => {
    const seen = new Set(Array.from({ length: 50 }, () => newIdem()))
    expect(seen.size).toBe(50)
    for (const id of seen) expect(id).toMatch(IDEM)
    expect(newIdem(() => new Uint8Array(26).fill(255))).toMatch(IDEM) // the first character stays 0..7
  })

  it('the command is exactly orch sign CODE', () => {
    expect(signCommand('7F3K-Q2MA')).toBe('orch sign 7F3K-Q2MA')
  })
})

describe('a request must be what it says it is', () => {
  const base = () => ({ material: structuredClone(vector.material), events, review_digest: vector.review_digest, fingerprint: vector.fingerprint, expires_at: new Date(Date.now() + 600_000).toISOString().replace(/\.\d+Z$/, 'Z') }) as unknown as HandoffRequest
  const rehash = (r: HandoffRequest): HandoffRequest => {
    const digest = reviewDigestOf(r.material)
    return { ...r, review_digest: digest, fingerprint: fingerprintOf(digest, r.events) }
  }
  it('accepts the vector, refuses a review that does not describe its event even with matching hashes', () => {
    expect(requestIsConsistent(base())).toBe(true)
    const wrong = base()
    wrong.material.review[0].payload.gate = 'requirements'
    expect(requestIsConsistent(rehash(wrong))).toBe(false)
    const type = base()
    type.material.review[0].type = 'ticket.closed'
    expect(requestIsConsistent(rehash(type))).toBe(false)
  })
  it('refuses an expires_at that is malformed or far away', () => {
    expect(requestIsConsistent({ ...base(), expires_at: 'soon' })).toBe(false)
    expect(requestIsConsistent({ ...base(), expires_at: '2099-01-01T00:00:00Z' })).toBe(false)
    expect(expiryMs('2020-01-01T00:00:00Z')).toBeNull()
  })
})
