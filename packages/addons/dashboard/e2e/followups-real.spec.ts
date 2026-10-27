// Follow-ups (#720) against a REAL dev host (not part of the fake-host run: needs FU_PY, FU_ENV, FU_BASE).
//   FU_PY=<server venv python> FU_ENV=e2e/followups-real-env.py FU_BASE=<dir under ~/orch-dev> ORCH_CORE_DIR=<checkout> \
//     playwright test -c e2e/playwright.config.ts followups-real.spec.ts
// A pending follow-up shows on the ticket strip, the board chip and the Follow-ups list; one that fell due with nobody to run
// it is a "did not run" Today card (Hide for me hides the card only); Cancel is signed by the test human in the terminal.
import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { expect, test } from '@playwright/test'

const PY = process.env.FU_PY ?? ''
const HELPER = process.env.FU_ENV ?? ''
const BASE = process.env.FU_BASE ?? ''
const SHOTS = process.env.SHOTS_DIR ?? path.resolve(import.meta.dirname, 'shots')
const helper = (...args: string[]) => execFileSync(PY, ['-I', HELPER, ...args], { env: { ...process.env }, encoding: 'utf8' }).trim()

test.skip(!PY || !HELPER || !BASE, 'needs a real dev host (FU_PY, FU_ENV, FU_BASE)')

test('real host: pending follow-up on the strip, the chip and the list; did not run on Today; Cancel signed', async ({ page }) => {
  test.setTimeout(420_000)
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message))
  page.on('response', (r) => r.status() >= 400 && console.log('HTTP', r.status(), r.url()))
  const state = JSON.parse(helper('state', BASE)) as { waiting: string; waiting_fu: string; stuck: string; stuck_fu: string }

  // the list
  await page.goto(helper('link', BASE, '/schedules'))
  await expect(page.getByRole('heading', { name: 'Schedules', level: 1 })).toBeVisible()
  const waiting = page.getByTestId('followups-waiting')
  await expect(waiting.getByTestId('followup-row').filter({ hasText: state.waiting })).toBeVisible({ timeout: 30_000 })
  await expect(waiting).toContainText('The agent\'s note to itself')
  await page.screenshot({ path: path.join(SHOTS, 'fu-real-list.png') })

  // the ticket strip (a quiet card in the right column)
  await page.goto(helper('link', BASE, `/ticket/${state.waiting}`))
  const card = page.getByTestId('ticket-followup')
  await expect(card).toBeVisible({ timeout: 30_000 })
  await expect(card).toContainText('Waiting')
  await expect(card).toContainText('Check whether backfill 8812 finished.')
  await page.screenshot({ path: path.join(SHOTS, 'fu-real-ticket.png') })

  // the board chip
  await page.goto(helper('link', BASE, '/board'))
  await expect(page.getByTestId('followup-chip').filter({ hasText: /^Resumes / })).toBeVisible({ timeout: 30_000 })
  await page.screenshot({ path: path.join(SHOTS, 'fu-real-board.png') })

  // the other follow-up falls due with no runner: the host records "did not run"; Today gets a card
  await expect
    .poll(() => helper('cli', BASE, 'unattended', 'follow-up', 'show', state.stuck_fu, '--json'), { timeout: 240_000, intervals: [5_000] })
    .toMatch(/"state": ?"(did_not_run|failed)"/)
  await page.goto(helper('link', BASE, '/'))
  const cards = page.getByTestId('followup-cards')
  await expect(cards).toBeVisible({ timeout: 60_000 })
  const stuck = cards.getByTestId('followup-card').filter({ hasText: state.stuck })
  await expect(stuck).toBeVisible()
  await page.screenshot({ path: path.join(SHOTS, 'fu-real-today.png') })
  await stuck.getByRole('button', { name: 'Hide for me' }).click()
  await expect(cards.getByTestId('followup-card').filter({ hasText: state.stuck })).toHaveCount(0)
  // hiding the card changes nothing on the ticket: its strip still says what happened
  await page.goto(helper('link', BASE, `/ticket/${state.stuck}`))
  await expect(page.getByTestId('followup-banner')).toBeVisible({ timeout: 30_000 })

  // Cancel the waiting one: core's review, the test human signs in the terminal
  await page.goto(helper('link', BASE, `/ticket/${state.waiting}`))
  await page.getByTestId('ticket-followup').getByRole('button', { name: 'Cancel follow-up' }).click()
  const code = (await page.getByText(/\b[0-9A-Z]{4}-[0-9A-Z]{4}\b/).first().innerText()).match(/\b[0-9A-Z]{4}-[0-9A-Z]{4}\b/)![0]
  helper('sign', BASE, code)
  await expect.poll(() => helper('cli', BASE, 'unattended', 'follow-up', 'show', state.waiting_fu, '--json'), { timeout: 60_000 }).toMatch(/"state": ?"cancelled"/)
  await page.screenshot({ path: path.join(SHOTS, 'fu-real-cancelled.png') })
})
