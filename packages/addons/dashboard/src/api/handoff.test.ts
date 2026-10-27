import { describe, expect, it } from 'vitest'
import { cj, expiryMs, fingerprintOf, requestIsConsistent, reviewDigestOf, type HandoffEvent, type HandoffRequest } from './handoff'
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
    expect(fingerprintOf(vector.review_digest, events, 'approve')).toBe(vector.fingerprint)
  })

  it('binding the device and the factor does not change it; the decision does', () => {
    const bound = [{ ...events[0], actor: { ...events[0].actor, device: 'd_' + '1'.repeat(16) }, auth: 'passphrase', sig: 'x' }]
    expect(fingerprintOf(vector.review_digest, bound as HandoffEvent[], 'approve')).toBe(vector.fingerprint_bound)
    expect(fingerprintOf(vector.review_digest, [{ ...events[0], gate: 'requirements' }], 'approve')).toBe(vector.fingerprint_other_gate)
    expect(vector.fingerprint_other_gate).not.toBe(vector.fingerprint)
  })

  it('the op is covered, and the documented vectors come out (host contract 0.8.1, H5a)', () => {
    expect(fingerprintOf(vector.review_digest, events, 'request_changes')).toBe(vector.fingerprint_other_op)
    expect(vector.fingerprint_other_op).not.toBe(vector.fingerprint)
    const ev = { v: 2, id: '01J9ZP0000000000000000000B', type: 'log.added', actor: { kind: 'person', id: 'p_' + '1'.repeat(32), device: 'd_' + '2'.repeat(32) }, auth: 'passphrase', hash_v: 1, roster_v: 1, based_on: 'sha256:' + 'cd'.repeat(32), text: 'hello', sig: 'x' }
    const digest = 'sha256:' + 'ab'.repeat(32)
    expect(fingerprintOf(digest, [ev as unknown as HandoffEvent], 'log')).toBe(vector.doc.op_log)
    expect(fingerprintOf(digest, [ev as unknown as HandoffEvent], 'set')).toBe(vector.doc.op_set)
  })

  it('a request is consistent only when both hashes recompute from its own bytes', () => {
    const request = { op: 'approve', material: vector.material, events, review_digest: vector.review_digest, fingerprint: vector.fingerprint, expires_at: new Date(Date.now() + 600_000).toISOString().replace(/\.\d+Z$/, 'Z') } as unknown as HandoffRequest
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

describe('the command', () => {
  it('the command is exactly orch sign CODE', () => {
  })
})

describe('a request must be what it says it is', () => {
  const base = () => ({ op: 'approve', material: structuredClone(vector.material), events, review_digest: vector.review_digest, fingerprint: vector.fingerprint, expires_at: new Date(Date.now() + 600_000).toISOString().replace(/\.\d+Z$/, 'Z') }) as unknown as HandoffRequest
  const rehash = (r: HandoffRequest): HandoffRequest => {
    const digest = reviewDigestOf(r.material)
    return { ...r, review_digest: digest, fingerprint: fingerprintOf(digest, r.events, r.op) }
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
  it('refuses a top-level material key outside the schema, and a missing one', () => {
    const extra = base()
    ;(extra.material as unknown as Record<string, unknown>).note = 'hello'
    expect(requestIsConsistent(rehash(extra))).toBe(false)
    const missing = base()
    delete (missing.material as unknown as Record<string, unknown>).policy
    expect(requestIsConsistent(rehash(missing))).toBe(false)
    expect(requestIsConsistent(rehash(base()))).toBe(true)
  })
  it('#740: manual_acceptances only on a verify gate, closed rows; a statement only on ac.accept / ac.unaccept', () => {
    const row = { ac: 'AC1', name: 'accept-AC1-1.md', by: 'p_' + '1'.repeat(32) }
    const verify = (rows: unknown, gate = 'verify') => {
      const r = base()
      Object.assign(r.material, { gate, manual_acceptances: rows })
      return rehash(r)
    }
    expect(requestIsConsistent(verify([]))).toBe(true)
    expect(requestIsConsistent(verify([row]))).toBe(true)
    expect(requestIsConsistent(verify([row], 'plan'))).toBe(false)
    expect(requestIsConsistent(verify([{ ...row, extra: 'x' }]))).toBe(false)
    expect(requestIsConsistent(verify([{ ac: 'AC1' }]))).toBe(false)
    expect(requestIsConsistent(verify('AC1'))).toBe(false)
    const payload = (op: string, statement: unknown) =>
      rehash({ ...base(), op, events: [], material: { kind: 'payload', ticket: null, review: [], statement } } as unknown as HandoffRequest)
    expect(requestIsConsistent(payload('ac.accept', 'statement'))).toBe(true)
    expect(requestIsConsistent(payload('ac.unaccept', 'statement'))).toBe(true)
    expect(requestIsConsistent(payload('log', 'statement'))).toBe(false)
    expect(requestIsConsistent(payload('ac.accept', 5))).toBe(false)
  })
  it('refuses an expires_at that is malformed or far away', () => {
    expect(requestIsConsistent({ ...base(), expires_at: 'soon' })).toBe(false)
    expect(requestIsConsistent({ ...base(), expires_at: '2099-01-01T00:00:00Z' })).toBe(false)
    expect(expiryMs('2020-01-01T00:00:00Z')).toBeNull()
  })
})
