// P02 (run 14): the Shares page when the host cannot list shares. Before: a bare 409 gave "Could not load shares" with a
// Retry that never helps. After: one line saying why, and one button only where one click fixes it. Screenshots go to
// SHOTS_DIR (run14-p02-*.png).
import path from 'node:path'
import { expect, test } from '@playwright/test'
import { FakeServer, WS } from './fakeHost'

const SHOTS = process.env.SHOTS_DIR ?? path.resolve(import.meta.dirname, 'shots')
let fake: FakeServer
let base = ''
let answer: { status: number; body: unknown } = { status: 200, body: {} }

test.beforeAll(async () => {
  fake = new FakeServer(path.resolve(import.meta.dirname, '../dist'))
  fake.clickOn = true
  fake.publishOn = true
  base = await fake.start()
  const inner = (fake as unknown as { handle: (q: import('node:http').IncomingMessage, r: import('node:http').ServerResponse) => Promise<void> }).handle.bind(fake)
  fake.server.removeAllListeners('request')
  fake.server.on('request', (req, res) => {
    const url = new URL(req.url ?? '/', 'http://x')
    if (url.pathname === `/api/w/${WS}/shares` && req.method === 'GET') {
      res.writeHead(answer.status, { 'content-type': 'application/json' })
      return res.end(JSON.stringify(answer.body))
    }
    void inner(req, res)
  })
})
test.afterAll(() => fake.stop())

const open = async (page: import('@playwright/test').Page) => {
  await page.goto(`${base}/#launch=demo-not-a-real-code&to=${encodeURIComponent('/w/DEMO/shares')}`)
}

test('before: a bare 409 is "Could not load"; after: the reason, one button, no Retry', async ({ page }) => {
  answer = { status: 409, body: { ok: false, error: { code: 'hub.unknown_for_test', message: 'x', retryable: false } } }
  await open(page)
  await expect(page.getByText('Could not load shares')).toBeVisible()
  await page.waitForTimeout(500)
  await page.screenshot({ path: path.join(SHOTS, 'run14-p02-before.png') })

  for (const [reason, title, name, role] of [
    ['off', 'Sharing is switched off', 'Turn on sharing', 'button'],
    ['not_configured', 'Sharing has no server yet', 'Set up sharing', 'link'],
    ['paused', 'Your hub paused sharing', null, null],
    ['hub_mode', 'Sharing is not available here', null, null],
  ] as const) {
    answer = { status: 200, body: { available: false, reason, can_fix: name !== null } }
    await open(page)
    await page.reload()
    const card = page.getByTestId('shares-unavailable')
    await expect(card).toContainText(title)
    await expect(page.getByRole('button', { name: /Retry|Try again/ })).toHaveCount(0)
    if (name && role) await expect(card.getByRole(role, { name })).toBeVisible()
    await page.waitForTimeout(500)
    await page.screenshot({ path: path.join(SHOTS, `run14-p02-after-${reason}.png`) })
  }
})
