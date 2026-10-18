import { expect, test } from 'vitest'
import { eventDetail, invalidatedText } from './History'
import type { OrchEvent } from '@/api/types'

const ev = (extra: Record<string, unknown>): OrchEvent => ({ v: 2, id: '1', seq: 5, at: '2026-10-18T20:00:00Z', type: 'gate.invalidated', actor: { kind: 'host', id: 'orch' }, ...extra })
const viewer = { name: (id: string) => id } as never

test('a cancelled approval says why, in plain words, and what was edited', () => {
  const e = ev({ gate: 'requirements', cause: 'content_changed', summary: 'requirements voided' })
  expect(eventDetail(e, viewer, ['size'])).toBe('The approval of the requirements was cancelled because the ticket changed after it was approved (changed: size). It needs approving again.')
  expect(eventDetail(ev({ gate: 'code', cause: 'new_commits' }), viewer)).toMatch(/new commits were added/)
})

test('without a cause the host line stays', () => {
  expect(invalidatedText(ev({ gate: 'plan' }))).toBeNull()
  expect(eventDetail(ev({ gate: 'plan', summary: 'plan voided' }), viewer)).toBe('gate.invalidated: plan voided')
})
