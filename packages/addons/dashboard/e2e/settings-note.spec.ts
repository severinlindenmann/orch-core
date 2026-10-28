// Run 13 owner feedback: no Switches card; one quiet line with a small i in Settings > General.
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

test('Settings > General has no switches, only one quiet line with an i', async ({ page }) => {
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message))
  await page.goto(`${base}/#launch=demo-not-a-real-code&to=${encodeURIComponent('/w/DEMO/settings/general')}`)
  const note = page.getByTestId('workspace-note')
  await expect(note).toBeVisible()
  await expect(note).toContainText(/History (on|off)/)
  await expect(page.getByText('Switches', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'More about this' }).click()
  await expect(page.getByText(/Claude Code asks you to trust/)).toBeVisible()
  await page.screenshot({ path: path.join(SHOTS, 'settings-note.png') })
})
