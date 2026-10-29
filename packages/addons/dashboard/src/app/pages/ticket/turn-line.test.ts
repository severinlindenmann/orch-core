import { expect, it } from 'vitest'
import { showTurn, turnReason } from './Header'

it('the turn line has no dangling separator when there is no reason', () => {
  expect(turnReason('')).toBe('')
  expect(turnReason('   ')).toBe('')
  expect(turnReason('Plan needs approval (owners)')).toBe(' · plan needs approval (owners)')
})

it('a done or closed ticket shows no Turn line; a lapsed open one reads plainly', () => {
  expect(showTurn({ status: 'done', turn: { who: 'nobody', why: '' } })).toBe(false)
  expect(showTurn({ status: 'closed', turn: { who: 'owner', why: 'x' } })).toBe(false)
  expect(showTurn({ status: 'in-progress', turn: { who: 'nobody', why: '' } })).toBe(false)
  expect(showTurn({ status: 'in-progress', turn: { who: 'nobody', why: 'Nobody is working on it' } })).toBe(true)
})
