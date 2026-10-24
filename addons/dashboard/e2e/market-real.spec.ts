// A10 against a REAL dev host with the S13 test marketplace (not part of the fake-host run: needs A10_PY, A10_ENV, A10_BASE).
//   A10_PY=<python with orch-core> A10_ENV=e2e/market-real-env.py A10_BASE=<base dir> playwright test market-real.spec.ts
// browse -> review -> Review installation -> the test human signs in the terminal (`orch sign CODE`) -> installed.
import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { expect, test } from '@playwright/test'

const PY = process.env.A10_PY ?? ''
const HELPER = process.env.A10_ENV ?? ''
const BASE = process.env.A10_BASE ?? ''
const SHOTS = process.env.SHOTS_DIR ?? path.resolve(import.meta.dirname, 'shots')
const helper = (...args: string[]) => execFileSync(PY, ["-I", HELPER, ...args], { env: { ...process.env }, encoding: 'utf8' }).trim()

test.skip(!PY || !HELPER || !BASE, 'needs a real dev host (A10_PY, A10_ENV, A10_BASE)')

test('real host: browse -> review -> Review installation -> sign in the terminal -> installed', async ({ page }) => {
  test.setTimeout(240_000)
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message))
  page.on('response', (r) => r.status() >= 400 && console.log('HTTP', r.status(), r.url()))
  const url = helper('link', BASE, '/addons?tab=browse')
  await page.goto(url)
  await expect(page.getByRole('heading', { name: 'Add-ons', level: 1 })).toBeVisible()
  const hello = page.getByRole('article', { name: 'hello' })
  await expect(hello).toBeVisible()
  await page.screenshot({ path: path.join(SHOTS, 'a10-real-browse.png') })
  await hello.getByRole('button', { name: /Install/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('The add-on will be installed turned off.', { timeout: 60_000 })
  await page.screenshot({ path: path.join(SHOTS, 'a10-real-review.png') })
  await dialog.getByRole('button', { name: 'Review installation' }).click()
  const code = (await page.getByText(/\b[0-9A-Z]{4}-[0-9A-Z]{4}\b/).first().innerText()).match(/\b[0-9A-Z]{4}-[0-9A-Z]{4}\b/)![0]
  await page.screenshot({ path: path.join(SHOTS, 'a10-real-terminal.png') })
  helper('sign', BASE, code) // the scripted test human, in a terminal
  await expect(page.getByText('Waiting for your signature')).toHaveCount(0, { timeout: 60_000 })
  await expect.poll(() => helper('cli', BASE, 'human', 'addon', 'list'), { timeout: 30_000 }).toMatch(/hello\s+1\.0\.0/)
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: 'Installed', exact: true }).click({ timeout: 15_000 })
  await expect(page.getByRole('row').filter({ hasText: 'hello' })).toBeVisible({ timeout: 30_000 })
  await page.screenshot({ path: path.join(SHOTS, 'a10-real-installed.png') })
})
