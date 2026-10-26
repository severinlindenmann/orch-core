import { expect, it } from 'vitest'
import { turnReason } from './Header'

it('the turn line has no dangling separator when there is no reason', () => {
  expect(turnReason('')).toBe('')
  expect(turnReason('   ')).toBe('')
  expect(turnReason('Plan needs approval (owners)')).toBe(' · plan needs approval (owners)')
})
