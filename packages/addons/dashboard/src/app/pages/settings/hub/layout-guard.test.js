import { expect, it } from 'vitest'
import { SETTINGS_HUB_PAGES } from '../../../../../scripts/layout-guard-pages.mjs'

it('covers each Settings > Hub mode with its own mock scenario URL', () => {
  expect(SETTINGS_HUB_PAGES).toEqual([
    ['settings-hub-standalone', '/settings/hub?hub=standalone'],
    ['settings-hub-selective', '/settings/hub?hub=selective'],
    ['settings-hub-full', '/settings/hub?hub=full'],
    ['settings-hub-offline', '/settings/hub?hub=offline'],
  ])
})
