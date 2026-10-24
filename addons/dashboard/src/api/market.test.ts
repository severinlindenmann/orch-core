import { expect, it } from 'vitest'
import { EXTERNAL_HELP, marketError, permissionEffect, publisherWording, reviewLines, stateLabel, widened } from './market'
import { stage, updateStage } from '@/test/fixtures/market'
import { clickEligible } from './click'
it('labels available, installed and pending states without inventing enabled state', () => {
  expect([stateLabel(), stateLabel('disabled'), stateLabel('active'), stateLabel('pending'), stateLabel('unknown')]).toEqual([
    'Available',
    'Off',
    'On',
    'Awaiting Terminal approval',
    'Installed',
  ])
})
it('describes effects and publisher memory without claiming safety', () => {
  expect(permissionEffect('network:calendar.example.test')).toBe('Connects to calendar.example.test')
  expect(permissionEffect('secret:calendar')).toBe('Uses your calendar token')
  expect(publisherWording('changed')).toBe('Key changed - review required')
  expect(marketError({ code: 'market.not_allowed' })).toBe(EXTERNAL_HELP)
})
it('keeps widened reviews full, with new lines first', () => {
  expect(widened(stage())).toBe(false)
  expect(widened(updateStage(false))).toBe(false)
  expect(widened(updateStage(true))).toBe(true)
  expect(reviewLines(updateStage(true))[0]).toEqual({ text: 'Connects to calendar.example.test', added: true })
})
it('market grants are never click eligible', () => {
  expect(clickEligible({ action: 'market_grant', name: 'calendar', staged: 'demo-stage' })).toBe(false)
})
