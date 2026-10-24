import { describe, expect, it } from 'vitest'
import type { QuickRow } from '@/api/quickRecords'
import { quickKey, taskState } from './status'

const row = (over: Partial<QuickRow> = {}) => ({ key: 'DEMO-1', title: 't', status: 'open', open_questions: 0, claim: null, awaiting_gate: null, labels: ['quick'], quick: null, ...over }) as unknown as QuickRow

describe('what a quick task row says and does', () => {
  it('a task nobody started: Not started and Start task', () => {
    expect(taskState(row())).toMatchObject({ word: 'Not started', action: 'start', actionLabel: 'Start task' })
  })
  it('an agent holds it: Working, no button', () => {
    expect(taskState(row({ claim: { agent: 'claude-code', for: 'p', session: 's', live: true } }))).toMatchObject({ word: 'Working', action: null })
  })
  it('waiting at verify: Ready for your review', () => {
    expect(taskState(row({ awaiting_gate: 'verify' }))).toMatchObject({ word: 'Ready for your review', action: 'review', actionLabel: 'Review result' })
  })
  it('an open question: Needs your answer, before anything else but a finished task', () => {
    expect(taskState(row({ open_questions: 1, awaiting_gate: 'verify' }))).toMatchObject({ word: 'Needs your answer', action: 'answer' })
  })
  it('a start this page made failed: Couldn’t start and Try again', () => {
    expect(taskState(row(), { failed: true })).toMatchObject({ word: 'Couldn’t start', action: 'retry', actionLabel: 'Try again' })
  })
  it('a start in progress says so and offers no button', () => {
    expect(taskState(row(), { starting: true })).toMatchObject({ word: 'Starting agent…', action: null })
  })
  it('done and closed are Finished', () => {
    expect(taskState(row({ status: 'done' })).word).toBe('Finished')
    expect(taskState(row({ status: 'closed' }), { failed: true }).action).toBeNull()
  })
  it('the created task is the one in the request’s own state', () => {
    expect(quickKey({ id: 'a', key: 'DEMO-9' }, { id: 'a' })).toBe('DEMO-9')
    expect(quickKey({ id: 'a', key: 'DEMO-9' }, { id: 'b' })).toBeNull()
    expect(quickKey({ id: 'a', key: null }, { id: 'a' })).toBeNull()
  })
})
