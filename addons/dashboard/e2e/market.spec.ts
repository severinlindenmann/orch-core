// A10: browse -> review -> Review installation (hand-off prepared with the staged id) -> installed, plus update with diff and widened.
import path from 'node:path'
import { expect, test } from '@playwright/test'
import { FakeServer, STAGE, STAGE_UPDATE, STAGE_WIDE } from './fakeHost'

const SHOTS = process.env.SHOTS_DIR ?? path.resolve(import.meta.dirname, 'shots')
let fake: FakeServer
let base = ''

test.beforeAll(async () => {
  fake = new FakeServer(path.resolve(import.meta.dirname, '../dist'))
  base = await fake.start()
})
test.afterAll(() => fake.stop())

const open = async (page: import('@playwright/test').Page, tab: string) => {
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message))
  page.on('response', (r) => r.status() >= 400 && console.log('HTTP', r.status(), r.url()))
  await page.goto(`${base}/#launch=demo-not-a-real-code&to=${encodeURIComponent(`/w/DEMO/addons?tab=${tab}`)}`)
  await expect(page.getByRole('heading', { name: 'Add-ons', level: 1 })).toBeVisible()
}

test('browse -> review -> Review installation -> installed', async ({ page }) => {
  await open(page, 'browse')
  const jira = page.getByRole('article', { name: 'jira' })
  await expect(jira).toContainText('official - severinlindenmann/orch-marketplace - 1.2.0')
  await expect(page.getByRole('article', { name: 'deploy-preview' })).toContainText('acme/orch-market - 1.0.0')
  await page.screenshot({ path: path.join(SHOTS, 'a10-browse.png') })
  await jira.getByRole('button', { name: /Install/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('The add-on will be installed turned off.')
  await expect(dialog).toContainText('Also installs 3 required software components')
  await page.screenshot({ path: path.join(SHOTS, 'a10-review.png') })
  expect(fake.posts('/market/stage')[0].body).toMatchObject({ id: 'jira' })
  await dialog.getByRole('button', { name: 'Review installation' }).click()
  await expect(page.getByText('7F3K-Q2MA')).toBeVisible()
  expect(fake.posts('/handoff')[0].body).toMatchObject({ action: 'market_grant', name: 'jira', staged: STAGE })
  await page.screenshot({ path: path.join(SHOTS, 'a10-terminal.png') })
  fake.signLast() // the mocked human.submit: the person approved in the Terminal
  await expect(jira).toContainText('Off', { timeout: 20_000 })
  await expect(page.getByText('Waiting for your signature')).toHaveCount(0)
  await page.screenshot({ path: path.join(SHOTS, 'a10-installed.png') })
})

test('update with a diff', async ({ page }) => {
  await open(page, 'installed')
  await page.getByRole('row').filter({ has: page.getByRole('cell', { name: /^github/ }) }).getByRole('button', { name: /Update/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('1.1.0')
  await expect(dialog).toContainText('1.2.0')
  await expect(dialog).toContainText('Old field')
  await expect(dialog).not.toContainText('asks for more than before')
  expect(fake.posts('/market/stage').pop()!.body).toMatchObject({ id: 'github', update: true })
  await page.screenshot({ path: path.join(SHOTS, 'a10-update-diff.png') })
  await dialog.getByRole('button', { name: 'Review installation' }).click()
  await expect.poll(() => fake.posts('/handoff').pop()?.body).toMatchObject({ action: 'market_grant', name: 'github', staged: STAGE_UPDATE })
})

test('widened update shows the full review', async ({ page }) => {
  await open(page, 'installed')
  await page.getByRole('row').filter({ has: page.getByRole('cell', { name: /^schedules/ }) }).getByRole('button', { name: /Update/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('asks for more than before')
  await expect(dialog.getByRole('list', { name: 'Full review' })).toBeVisible()
  await expect(dialog).toContainText('Starts agents on a timetable')
  await page.screenshot({ path: path.join(SHOTS, 'a10-update-widened.png') })
  await dialog.getByRole('button', { name: 'Review installation' }).click()
  await expect.poll(() => fake.posts('/handoff').pop()?.body).toMatchObject({ staged: STAGE_WIDE })
})

test('marketplaces table', async ({ page }) => {
  await open(page, 'marketplaces')
  await expect(page.getByText('First use - key will be remembered')).toBeVisible()
  await expect(page.getByText(/aren't available in this workspace yet/).first()).toBeVisible()
  await expect(page.getByRole('button', { name: '+ Add marketplace' })).toBeDisabled()
  await page.screenshot({ path: path.join(SHOTS, 'a10-marketplaces.png') })
})

test('phone width: no sideways spill', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  for (const tab of ['browse', 'installed', 'marketplaces']) {
    await open(page, tab)
    await page.screenshot({ path: path.join(SHOTS, `a10-phone-${tab}.png`) })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  }
})
