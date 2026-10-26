import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { keyFingerprint } from './click'

// The same golden vectors core and the dashboard server test: the page, the signer and the server name a key alike.
const V = JSON.parse(readFileSync(resolve(process.cwd(), '../../plugins/orch-core/tests/click_reveal_fingerprint.json'), 'utf8')) as { cases: { key: string; fingerprint: string }[] }

describe('the page key fingerprint', () => {
  it('is the full SHA-256 and matches the shared golden vectors', async () => {
    expect(V.cases.length).toBeGreaterThan(0)
    for (const c of V.cases) {
      expect(c.fingerprint).toHaveLength(64)
      expect(await keyFingerprint(c.key)).toBe(c.fingerprint)
    }
  })
})
