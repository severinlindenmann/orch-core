import { expect, it } from 'vitest'
import { primaryAction, primaryLabel, type Available } from './actions'

const q = { id: 'Q1' } as Available['answer'][number]
const none: Available = { approve: [], approveBoth: false, requestChanges: [], verdict: false, answer: [] }

it('an open question is the primary action when nothing else needs the viewer (run 8, 6.3)', () => {
  const p = primaryAction({ ...none, answer: [q] })
  expect(p).toEqual({ kind: 'answer', question: 'Q1' })
  expect(primaryLabel(p!)).toBe('Answer question')
})
it('a verdict or an approval still comes first', () => {
  expect(primaryAction({ ...none, verdict: true, answer: [q] })).toEqual({ kind: 'verdict' })
  expect(primaryAction({ ...none, approve: ['plan'], answer: [q] })).toEqual({ kind: 'approve', gate: 'plan' })
})
