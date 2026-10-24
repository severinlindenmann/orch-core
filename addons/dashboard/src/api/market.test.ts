import { expect, it } from 'vitest'
import { EXTERNAL_HELP, marketError, permissionEffect, publisherWording, reviewLines, stateLabel, widened } from './market'
import { stage, updateStage } from '@/test/fixtures/market'
import { clickEligible } from './click'
it('labels available, installed and pending states without inventing enabled state', () => {
  expect([stateLabel(), stateLabel('disabled'), stateLabel('active'), stateLabel('pending'), stateLabel('unknown')]).toEqual([
    'Available',
    'Off',
    'On',
    'Waiting for installation approval',
    'Installed',
  ])
})
it('describes effects and publisher memory without claiming safety', () => {
  expect(permissionEffect('network:calendar.example.test')).toBe('Connects to calendar.example.test')
  expect(permissionEffect('secret:calendar')).toBe('Uses your calendar token')
  expect(publisherWording('changed')).toBe('Key changed - review required')
  expect(marketError({ code: 'market.not_allowed' })).toBe(EXTERNAL_HELP)
})
it('keeps the host order and marks only lines the structured diff names', () => {
  expect(widened(stage())).toBe(false)
  expect(widened(updateStage(false))).toBe(false)
  expect(widened(updateStage(true))).toBe(true)
  const lines = reviewLines(updateStage(true))
  expect(lines.map((l) => l.text)).toEqual(updateStage(true).review) // never reordered
  expect(lines.map((l) => l.added)).toEqual([false, false, true, true])
  expect(reviewLines(updateStage(false)).some((l) => l.added)).toBe(false)
  // no substring matching: an added field named "a" does not mark every line
  const s = updateStage(true)
  s.update!.added.fields = ['a']
  expect(reviewLines(s).map((l) => l.added)).toEqual([false, false, true, false])
})
it('market grants are one click (V3-D38), except Publish', () => {
  expect(clickEligible({ action: 'market_grant', name: 'calendar', staged: 'demo-stage' })).toBe(true)
  expect(clickEligible({ action: 'market_grant', name: 'publish', staged: 'demo-stage' })).toBe(false)
})
