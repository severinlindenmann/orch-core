import { expect, it } from 'vitest'
import { clickEligible } from '@/api/click'

// Run 8 (1.1): on a one-click workspace every settings action the signer serves must be offered as a click, so the
// person gets one Sign button; only the terminal-only ones fall back to the Terminal hand-off (which works with One click on).
it('settings actions the signer serves by click are click-eligible', () => {
  expect(clickEligible({ action: 'icon_set', icon: { icon: 'cloud', colour: 'teal' } })).toBe(true)
  expect(clickEligible({ action: 'icon_set', icon: null })).toBe(true)
  expect(clickEligible({ action: 'models_set', models: {} })).toBe(true)
  expect(clickEligible({ action: 'repo_set', name: 'a', path: '/x' })).toBe(true)
  expect(clickEligible({ action: 'settings_set', sign_methods: ['terminal', 'click'] })).toBe(true) // #1161: core classes it click
  expect(clickEligible({ action: 'repo_remove', name: 'a' })).toBe(true)
})
it('terminal-only actions stay terminal-only', () => {
  expect(clickEligible({ action: 'publish_setup', server: 'https://a.test', share: 'https://b.test' })).toBe(false)
})
