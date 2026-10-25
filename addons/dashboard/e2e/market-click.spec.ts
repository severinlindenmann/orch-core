// V3-D38: with One click on, install is: browse -> review -> Review installation -> the signer's whole review -> ONE Sign press -> installed.
// No Terminal, no typed word. The fake dashboard server answers /click/* from plain data (e2e/fakeHost.ts, clickOn).
import path from 'node:path'
import { expect, test } from '@playwright/test'
import { FakeServer, STAGE } from './fakeHost'

const SHOTS = process.env.SHOTS_DIR ?? path.resolve(import.meta.dirname, 'shots')
let fake: FakeServer
let base = ''

test.beforeAll(async () => {
  fake = new FakeServer(path.resolve(import.meta.dirname, '../dist'))
  fake.clickOn = true
  base = await fake.start()
})
test.afterAll(() => fake.stop())

test('browse -> review -> Review installation -> one Sign press -> installed', async ({ page }) => {
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message))
  await page.goto(`${base}/#launch=demo-not-a-real-code&to=${encodeURIComponent('/w/DEMO/addons?tab=browse')}`)
  await expect(page.getByRole('heading', { name: 'Add-ons', level: 1 })).toBeVisible()
  const jira = page.getByRole('article', { name: 'jira' })
  await jira.getByRole('button', { name: /Install/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('The add-on will be installed Off.')
  await expect(dialog).not.toContainText('Terminal')
  await page.screenshot({ path: path.join(SHOTS, 'oneclick-review.png') })
  await dialog.getByRole('button', { name: 'Review installation' }).click()
  // the signer's whole review, the marketplace block labelled, and exactly one way to sign
  const sign = page.getByRole('dialog').getByRole('button', { name: 'Approve and install' })
  await expect(page.getByRole('dialog')).toContainText('package_sha256')
  await expect(page.getByRole('dialog')).toContainText('(per host, not signed)') // core's label, exact
  await expect(page.getByTestId('install-summary')).toContainText('After installation it is Off')
  await expect(page.getByRole('dialog')).not.toContainText('7F3K-Q2MA')
  expect(fake.posts('/handoff')).toHaveLength(0) // no Terminal hand-off
  expect(fake.posts('/click/prepare')[0].body).toMatchObject({ action: 'market_grant', name: 'jira', staged: STAGE })
  await expect(sign).toBeEnabled({ timeout: 10_000 })
  await page.screenshot({ path: path.join(SHOTS, 'oneclick-sign.png') })
  await page.setViewportSize({ width: 390, height: 844 }) // phone width: the same review, no sideways spill
  await expect(sign).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: path.join(SHOTS, 'oneclick-sign-390.png') })
  await page.setViewportSize({ width: 1440, height: 900 })
  await sign.click()
  await expect(jira).toContainText('Off', { timeout: 20_000 })
  expect(fake.posts('/click/sign')).toHaveLength(1)
  await page.screenshot({ path: path.join(SHOTS, 'oneclick-installed.png') })
})
