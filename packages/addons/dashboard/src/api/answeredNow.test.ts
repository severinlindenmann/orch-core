import { QueryClient } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { answeredNow } from './signedDrop'

const doc = {
  key: 'T-1',
  questions_state: [
    { id: 'q1', state: 'open', options: [{ key: 'a', label: 'Red' }] },
    { id: 'q2', state: 'open' },
  ],
}

describe('answeredNow', () => {
  it('marks the question answered in the cached ticket', () => {
    const qc = new QueryClient()
    qc.setQueryData(['ticket', 'w1', 'T-1'], doc)
    answeredNow(qc, 'w1', 'T-1', 'q1', { option: 'a' }, 'ann')
    const d = qc.getQueryData<typeof doc & { questions_state: Array<{ id: string; state: string; answer?: { by: string; option?: string; at: string } }> }>(['ticket', 'w1', 'T-1'])!
    expect(d.questions_state[0].state).toBe('answered')
    expect(d.questions_state[0].answer?.by).toBe('ann')
    expect(d.questions_state[0].answer?.option).toBe('a')
    expect(d.questions_state[0].answer?.at).toBeTruthy()
    expect(d.questions_state[1].state).toBe('open')
  })

  it('does nothing for a ticket that is not loaded', () => {
    const qc = new QueryClient()
    answeredNow(qc, 'w1', 'T-9', 'q1', { text: 'yes' }, 'ann')
    expect(qc.getQueryData(['ticket', 'w1', 'T-9'])).toBeUndefined()
  })
})
