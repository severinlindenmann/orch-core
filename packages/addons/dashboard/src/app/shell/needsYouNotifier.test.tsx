import { describe, expect, it } from 'vitest'
import { itemId, titleFor, toastText } from './NeedsYouNotifier'

const item = (kind: string, extra: Record<string, unknown> = {}) =>
  ({ kind, ticket: 'T-1', ref: 'q1', hash: 'sha256:ab', title: 'Pick a colour', ...extra }) as Parameters<typeof itemId>[0]

describe('itemId', () => {
  it('is stable and tells versions apart', () => {
    expect(itemId(item('question'))).toBe('question:T-1:q1:sha256:ab')
    expect(itemId(item('question'))).toBe(itemId(item('question')))
    expect(itemId(item('question', { hash: 'sha256:cd' }))).not.toBe(itemId(item('question')))
    expect(itemId(item('approval', { ref: undefined, hash: undefined }))).toBe('approval:T-1::')
  })
})

describe('toastText', () => {
  it('says it in plain words per kind', () => {
    expect(toastText(item('question'))).toBe('A question about Pick a colour')
    expect(toastText(item('approval'))).toBe('Ready for your approval: Pick a colour')
    expect(toastText(item('verdict'))).toBe('Ready for your check: Pick a colour')
  })
})

describe('titleFor', () => {
  it('puts the count in front', () => {
    expect(titleFor('orch', 1)).toBe('(1) orch')
  })
  it('strips an earlier count', () => {
    expect(titleFor('(3) orch', 2)).toBe('(2) orch')
  })
  it('is the base when nothing waits', () => {
    expect(titleFor('orch', 0)).toBe('orch')
    expect(titleFor('(4) orch', 0)).toBe('orch')
  })
})
