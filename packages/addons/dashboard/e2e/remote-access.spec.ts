// R2 T4 (#1590): Settings > Devices > Remote access, before and after. Screenshots go to SHOTS_DIR.
import path from 'node:path'
import { expect, test } from '@playwright/test'
import { FakeServer, WS } from './fakeHost'

const SHOTS = process.env.SHOTS_DIR ?? path.resolve(import.meta.dirname, 'shots')
let fake: FakeServer
let base = ''

test.beforeAll(async () => {
  fake = new FakeServer(path.resolve(import.meta.dirname, '../dist'))
  base = await fake.start()
})
test.afterAll(() => fake.stop())

test('Remote access: off, the turn-on question, on with a device, a code and a waiting device', async ({ page }) => {
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message))
  await page.goto(`${base}/#launch=demo-not-a-real-code&to=${encodeURIComponent('/w/DEMO/settings/devices')}`)
  const section = page.getByTestId('remote-access')
  await expect(section).toContainText('Remote access: Off')
  await expect(page.getByTestId('remote-turn-on-btn')).toHaveCount(0) // not isolated: behind More
  await page.screenshot({ path: path.join(SHOTS, 'r2-remote-1-off.png') })
  await page.getByTestId('remote-more').click()
  await expect(section).toContainText('Agents on this computer can act as you')
  await page.getByTestId('remote-turn-on-btn').click()
  await expect(page.getByTestId('remote-level-terminals')).not.toBeChecked()
  await page.getByTestId('remote-premises-no').check()
  await page.screenshot({ path: path.join(SHOTS, 'r2-remote-2-turn-on.png') })

  const soon = Date.now() + 3 * 86_400_000
  fake.remote = { ...fake.remote, on: true, level: 'work', premises: 'no', until_ms: soon, devices: [{ device_id: 'rd_' + 'a'.repeat(32), name: "Anna's laptop", level_cap: 'view', added_ms: 1, expires_ms: soon, words: 'one two three four five six' }] }
  await page.reload()
  await expect(page.getByTestId('remote-state')).toContainText('Remote access: On')
  await expect(page.getByTestId('remote-no-sessions')).toBeVisible()
  await page.screenshot({ path: path.join(SHOTS, 'r2-remote-3-on.png') })
  await page.getByTestId('remote-add-device').click()
  await expect(page.getByTestId('remote-code')).toContainText('orch-rc1.')
  await page.screenshot({ path: path.join(SHOTS, 'r2-remote-4-code.png') })
  fake.remote = { ...fake.remote, pending: [{ pending: 'rp_' + 'b'.repeat(32), name: "Anna's phone", words: 'alpha bravo charlie delta echo foxtrot', expires_ms: soon }] }
  await expect(page.getByTestId('remote-pending')).toContainText("Add device Anna's phone, fingerprint alpha bravo charlie delta echo foxtrot?", { timeout: 10_000 })
  await page.screenshot({ path: path.join(SHOTS, 'r2-remote-5-pending.png') })
  await page.getByTestId('remote-device-remove').click()
  await expect(page.getByTestId('remote-no-devices')).toBeVisible()
  expect(fake.posts('/remove')).toHaveLength(1)
  expect(WS).toBeTruthy()
})
