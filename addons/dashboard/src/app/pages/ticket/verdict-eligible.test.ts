import { expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { availableActions, primaryAction, primaryLabel, type Available } from './actions'

// Run 8, 7.5: Today offers the verdict from the HOST's eligible list (core's may_decide, incl. the solo-owner rule);
// the ticket page used to re-derive it from the policy as written and showed nothing. Now it follows the host list.
const me = { person: 'p_sev', role: 'owner' as const, members: [], name: (id: string | null | undefined) => String(id), ready: true }

function testing() {
  const doc = structuredClone(mockStore.ticket('DEMO-0041')!)
  doc.status = 'testing'
  doc.verdict = null as never
  doc.gates.verify = { ...doc.gates.verify, state: 'pending', approvals: [], approvers: 'reviewers', not: 'assignees', needed: 1, hash: 'h' }
  doc.people.reviewers = ['p_other']
  doc.people.assignees = []
  return doc
}

it('a solo owner who is not a reviewer gets "Give verdict" when the host lists them as eligible', () => {
  const doc = testing()
  expect(availableActions(doc, me).verdict).toBe(false)
  doc.gates.verify.eligible = ['p_sev']
  const av = availableActions(doc, me)
  expect(av.verdict).toBe(true)
  expect(primaryAction(av)).toEqual({ kind: 'verdict' })
})

it('a person the host does not list is not offered the verdict, whatever the policy as written says', () => {
  const doc = testing()
  doc.people.reviewers = ['p_sev']
  doc.gates.verify.eligible = ['p_other']
  expect(availableActions(doc, me).verdict).toBe(false)
})

it('a verdict the host marks stale (current: false) does not hide "Give verdict"; a current one does', () => {
  const doc = testing()
  doc.gates.verify.eligible = ['p_sev']
  doc.verdict = { result: 'fail', by: 'p_sev', at: '2026-10-23T10:00:00Z', current: false } as never
  expect(availableActions(doc, me).verdict).toBe(true)
  doc.verdict = { result: 'pass', by: 'p_sev', at: '2026-10-23T10:00:00Z', current: true } as never
  expect(availableActions(doc, me).verdict).toBe(false)
})

const q = (id: string, blocking: boolean) => ({ id, blocking }) as Available['answer'][number]
const none: Available = { approve: [], requestChanges: [], verdict: false, answer: [] }

it('header priority: blocking question > verdict > approve > other question > start agent', () => {
  expect(primaryAction({ ...none, verdict: true, approve: ['plan'], answer: [q('Q1', true)] })).toEqual({ kind: 'answer', question: 'Q1' })
  expect(primaryAction({ ...none, verdict: true, approve: ['plan'], answer: [q('Q1', false)] })).toEqual({ kind: 'verdict' })
  expect(primaryAction({ ...none, approve: ['plan'], answer: [q('Q1', false)] }, { start: true })).toEqual({ kind: 'approve', gate: 'plan' })
  expect(primaryAction({ ...none, answer: [q('Q1', false)] }, { start: true })).toEqual({ kind: 'answer', question: 'Q1' })
  expect(primaryAction(none, { start: true })).toEqual({ kind: 'start' })
  expect(primaryAction(none)).toBeNull()
})

it('labels: Answer question(s) with the count, Start agent', () => {
  expect(primaryLabel({ kind: 'answer', question: 'Q1' }, { answer: [q('Q1', false), q('Q2', false)] })).toBe('Answer questions (2)')
  expect(primaryLabel({ kind: 'start' })).toBe('Start agent')
})
