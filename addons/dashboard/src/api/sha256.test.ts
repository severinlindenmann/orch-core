import { describe, expect, it } from 'vitest'
import { sha256Hex } from './sha256'

const ref = async (s: string) =>
  [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s)))].map((b) => b.toString(16).padStart(2, '0')).join('')

describe('sha256Hex', () => {
  it('matches the known vectors', () => {
    expect(sha256Hex('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
    expect(sha256Hex('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
  })
  it('matches SubtleCrypto across block boundaries and for multi-byte text', async () => {
    for (const n of [1, 55, 56, 63, 64, 65, 119, 120, 1000, 5000]) expect(sha256Hex('x'.repeat(n))).toBe(await ref('x'.repeat(n)))
    const s = 'Größe: 412 kB → 286 kB ✓ 日本語 😀'
    expect(sha256Hex(s)).toBe(await ref(s))
  })
})
