import { describe, expect, it } from 'vitest'
import { clickEligible } from '@/api/click'

// Run 8 (1.1): on a one-click workspace every settings action the signer serves must be offered as a click, so the
// person gets one Sign button; only the terminal-only ones fall back to the terminal hand-off (which works with One click on).
it('settings actions the signer serves by click are click-eligible', () => {
  expect(clickEligible({ action: 'icon_set', icon: { icon: 'cloud', colour: 'teal' } })).toBe(true)
  expect(clickEligible({ action: 'icon_set', icon: null })).toBe(true)
  expect(clickEligible({ action: 'repo_set', name: 'a', path: '/x' })).toBe(true)
  expect(clickEligible({ action: 'settings_set', sign_methods: ['terminal', 'click'] })).toBe(true) // #1161: core classes it click
  expect(clickEligible({ action: 'repo_remove', name: 'a' })).toBe(true)
})
it('Publish is click-eligible (#1198 C2)', () => {
  expect(clickEligible({ action: 'publish_setup', server: 'https://a.test', share: 'https://b.test' })).toBe(true)
  expect(clickEligible({ action: 'publish_share', key: 'DEMO-1', staged_id: 's' })).toBe(true)
  expect(clickEligible({ action: 'addon_enable', name: 'publish', publish: { server: 'https://a.test', share: 'https://b.test' } })).toBe(true)
})

import { DEPOSIT_LINE, DEPOSIT_RISK, depositViewOk, type ClickView } from './click'
describe('depositViewOk on a contract-3 click View', () => {
  const WS = 'a'.repeat(32)
  const view = (over: Partial<ClickView> = {}): ClickView => ({
    risk: 'x', op: 'grant', workspace: { id: WS, name: 'w', prefix: 'DEMO' }, fingerprint: '0123456789ab',
    lines: ['grant', '', DEPOSIT_RISK, DEPOSIT_LINE], checks: [], confirm: { kind: 'click' }, expires_at: '2026-01-01T00:00:00Z',
    auth: 'convenience', device: 'd_x', artifacts: null, deposit: true, deposit_line: DEPOSIT_LINE, ...over,
  })
  it('is offered Sign on a plain click View of this workspace', () => expect(depositViewOk(view(), WS)).toBe(true))
  it('is not offered for another workspace or a missing deposit line', () => {
    expect(depositViewOk(view(), 'b'.repeat(32))).toBe(false)
    expect(depositViewOk(view({ lines: ['grant'] }), WS)).toBe(false)
  })
})
it('hub steps are click-eligible (#1198 C10) and carry no word', () => {
  expect(clickEligible({ action: 'hub_join', hub_card: '{}', hub_id: 'a'.repeat(32) })).toBe(true)
  expect(clickEligible({ action: 'hub_leave' })).toBe(true)
})
it('Cancel follow-up is click-eligible (run 12, 6.4): one click, no terminal word', () => {
  expect(clickEligible({ action: 'followup_cancel', key: 'DEMO-1', id: 'fu_1' })).toBe(true)
})
