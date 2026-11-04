import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { DASHBOARD_EXECUTION_LINE } from '@/api/quickRecords'
import { EXECUTION_LINE } from './executionLine'

/** The card line is core's words (D-P2-31, `orch.runner.enablement.CARD_TEXT`): the bundle's copies must not drift from it. */
function coreCardText(): string {
  const src = readFileSync(resolve(__dirname, '../../../../../../core/src/orch/runner/enablement.py'), 'utf8')
  const body = /^CARD_TEXT = \(\n([\s\S]*?)\n\)$/m.exec(src)?.[1]
  if (!body) throw new Error('CARD_TEXT not found in enablement.py')
  return [...body.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1]).join('')
}

describe('the dashboard execution line equals core CARD_TEXT', () => {
  it('both bundle constants', () => {
    const core = coreCardText()
    expect(core.length).toBeGreaterThan(100)
    expect(EXECUTION_LINE).toBe(core)
    expect(DASHBOARD_EXECUTION_LINE).toBe(core)
  })
})
