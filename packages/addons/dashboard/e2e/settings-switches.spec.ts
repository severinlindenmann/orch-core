// Run 13 lane UE: two one-click switches in Settings > General (Claude Code folder trust, history timer). No typed word.
import path from 'node:path'
import { expect, test } from '@playwright/test'
import { FakeServer } from './fakeHost'

const SHOTS = process.env.SHOTS_DIR ?? path.resolve(import.meta.dirname, 'shots')
let fake: FakeServer
let base = ''

test.beforeAll(async () => {
  fake = new FakeServer(path.resolve(import.meta.dirname, '../dist'))
  base = await fake.start()
})
test.afterAll(() => fake.stop())

test('both switches are one click, plain words, details behind the i', async ({ page }) => {
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message))
  await page.goto(`${base}/#launch=demo-not-a-real-code&to=${encodeURIComponent('/w/DEMO/settings/general')}`)
  const trust = page.getByRole('switch', { name: /Let Claude Code open this workspace's folders without asking/ })
  const history = page.getByRole('switch', { name: 'Keep a history of your work' })
  await expect(trust).toBeVisible()
  await expect(history).toBeVisible()
  await page.screenshot({ path: path.join(SHOTS, 'switches-off.png') })
  await trust.click()
  await expect(trust).toBeChecked()
  expect(fake.posts('/claude-trust')[0].body).toEqual({ allow_parent: false })
  await history.click()
  await expect(history).toBeChecked()
  expect(fake.posts('/records/auto-on')).toHaveLength(1)
  await page.getByRole('button', { name: /More about Keep a history/ }).click()
  await expect(page.getByText(/does not send them anywhere/)).toBeVisible()
  await page.screenshot({ path: path.join(SHOTS, 'switches-on.png') })
  await history.click()
  await expect(history).not.toBeChecked()
  expect(fake.posts('/records/auto-off')).toHaveLength(1)
})
