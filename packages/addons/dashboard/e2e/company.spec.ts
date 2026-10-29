// M3 H-5: the owner's Company tab against the production build in demo mode (the mock hub offers). PHASE=before takes the
// settings page of a build without the tab; the default phase takes the tab, a skill sheet and a server sheet with tools.
import path from 'node:path'
import { expect, test } from '@playwright/test'
import { FakeServer } from './fakeHost'

const SHOTS = process.env.SHOTS_DIR ?? path.resolve(import.meta.dirname, 'shots')
const before = process.env.PHASE === 'before'
let fake: FakeServer
let base = ''

test.beforeAll(async () => {
  fake = new FakeServer(path.resolve(import.meta.dirname, process.env.DIST ?? '../dist'))
  fake.hubOn = !before
  base = await fake.start()
})
test.afterAll(() => fake.stop())

const to = (p: string) => `${base}/#launch=demo-not-a-real-code&to=${encodeURIComponent(p)}`

test('settings without the Company tab (before)', async ({ page }) => {
  test.skip(!before, 'only for the build without the tab')
  await page.goto(to('/w/DEMO/settings/general'))
  const nav = page.getByRole('navigation', { name: 'Settings' })
  await expect(nav.getByRole('link', { name: 'General' })).toBeVisible()
  await expect(nav.getByRole('link', { name: 'Company' })).toHaveCount(0)
  await page.screenshot({ path: path.join(SHOTS, 'before-settings.png') })
})

test('Company tab: lists, a skill sheet, a server sheet with tools (after)', async ({ page }) => {
  test.skip(before, 'only for the build with the tab')
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message))
  await page.goto(to('/w/DEMO/settings/general'))
  await page.getByRole('navigation', { name: 'Settings' }).getByRole('link', { name: 'Company' }).click()
  await expect(page.getByTestId('company-tab')).toBeVisible()
  await expect(page.getByTestId('company-status-support-tone')).toHaveText('Changed — look again')
  await page.screenshot({ path: path.join(SHOTS, 'after-company-list.png') })

  await page.getByTestId('company-open-style-guide').click()
  const pre = page.getByTestId('company-skill-text')
  await expect(pre).toContainText('Use **no** jargon.') // markdown stays literal
  await page.screenshot({ path: path.join(SHOTS, 'after-skill-sheet.png') })
  await page.getByTestId('company-accept').click()
  await expect(page.getByTestId('company-status-style-guide')).toHaveText('On')

  await page.getByTestId('company-on-crm').click()
  await page.getByTestId('company-accept').click()
  await page.getByTestId('company-open-crm').click()
  await page.getByTestId('company-tool-switch-find_customer').click()
  await expect(page.getByTestId('company-tool-switch-find_customer')).toHaveAttribute('aria-checked', 'true')
  await page.screenshot({ path: path.join(SHOTS, 'after-server-sheet.png') })
  await page.getByTestId('company-close').click()

  await page.getByTestId('company-on-billing').click()
  await expect(page.getByTestId('company-needs-isolation')).toBeVisible()
  await expect(page.getByTestId('company-accept')).toHaveCount(0)
  await page.screenshot({ path: path.join(SHOTS, 'after-secret-server-sheet.png') })
})
