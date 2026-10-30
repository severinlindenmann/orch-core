// Run 14 P01: Publish as link on a result card. Screenshots go to SHOTS_DIR (run14-p01-*.png). The share routes are
// answered by a thin layer in front of the shared fake server (as in share-files.spec.ts).
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { expect, test } from '@playwright/test'
import { FakeServer, WS } from './fakeHost'

const SHOTS = process.env.SHOTS_DIR ?? path.resolve(import.meta.dirname, 'shots')
const fixture = (n: string) => JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, '../src/test/fixtures/server-m2', n), 'utf8'))
let fake: FakeServer
let base = ''
const prepares: Record<string, any>[] = []
let refuse: string | null = null

test.beforeAll(async () => {
  fake = new FakeServer(path.resolve(import.meta.dirname, '../dist'))
  fake.clickOn = true
  fake.publishOn = true
  base = await fake.start()
  const inner = (fake as unknown as { handle: (q: http.IncomingMessage, r: http.ServerResponse) => Promise<void> }).handle.bind(fake)
  fake.server.removeAllListeners('request')
  fake.server.on('request', (req, res) => {
    void (async () => {
      const url = new URL(req.url ?? '/', 'http://x')
      const rest = new RegExp(`^/api/w/${WS}/(.*)$`).exec(url.pathname)?.[1]
      const method = req.method ?? 'GET'
      const json = (v: unknown, status = 200) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(v)) }
      if (rest === undefined) return inner(req, res)
      if (rest === 'add-ons' && method === 'GET') return json({ available: true, add_ons: [{ id: 'publish', title: 'Publish', desc: 'Share files of a ticket with a link.', state: 'on', locked: null, blocked: null, needs: 'publish', set_up: true }] })
      if (rest === 'tickets/DEMO-0001' && method === 'GET') return json({ ...fixture('ticket.json'), status: 'done', artifacts: [{ name: 'hello.txt', kind: 'report', bytes: 16, sha256: 'ab'.repeat(32), added_by: 'p_x', at: '2026-10-18T08:00:00Z' }] })
      if (rest.startsWith('shares') && method === 'GET') return json({ available: true, configured: true, server: 'https://share.example.org', share: 'https://link.example.org', workspace: WS, owner: 'p_owner', live: true, shares: [], staged: [] })
      if (rest === 'shares/prepare' && method === 'POST') {
        const raw = await new Promise<string>((r) => { let s = ''; req.on('data', (c) => (s += c)); req.on('end', () => r(s)) })
        prepares.push(JSON.parse(raw))
        if (refuse) return json({ ok: false, error: { code: refuse, message: refuse, retryable: false } }, 409)
        return json({ staged: { staged_id: 'ps_' + '2'.repeat(32), ticket: 'DEMO-0001', kind: 'secret', days: 7, views: 0, origin: 'person', files: [{ name: 'hello.txt', size: 16, sha256: 'ab'.repeat(32) }], created_at: null }, server: null, share: null })
      }
      if (rest === 'shares/discard' && method === 'POST') return json({ discarded: true })
      return inner(req, res)
    })()
  })
})
test.afterAll(() => fake.stop())

const results = async (page: import('@playwright/test').Page) => {
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message))
  await page.goto(`${base}/#launch=demo-not-a-real-code&to=${encodeURIComponent('/w/DEMO/ticket/DEMO-0001')}`)
  await page.getByRole('tab', { name: /Results/ }).click()
}

test('Publish as link: button on the result card, one small panel, then the signature', async ({ page }) => {
  refuse = null
  await results(page)
  const button = page.getByRole('button', { name: 'Publish hello.txt as a link' })
  await expect(button).toBeVisible()
  await page.waitForTimeout(500)
  await page.screenshot({ path: path.join(SHOTS, 'run14-p01-after-results.png') })
  await button.click()
  const panel = page.getByRole('dialog', { name: 'Publish hello.txt as a link?' })
  await expect(panel).toContainText('Anyone with the link can open this file')
  await page.waitForTimeout(500)
  await page.screenshot({ path: path.join(SHOTS, 'run14-p01-after-panel.png') })
  await panel.getByRole('button', { name: 'Sign and publish' }).click()
  await expect(page.getByRole('button', { name: /Share files of DEMO-0001/ })).toBeVisible()
  expect(prepares.at(-1)).toMatchObject({ ticket: 'DEMO-0001', artifacts: ['hello.txt'], kind: 'secret', days: 7 })
  await page.waitForTimeout(500)
  await page.screenshot({ path: path.join(SHOTS, 'run14-p01-after-sign.png') })
})

test('Publish as link: a hub that cannot carry it is said in words before anything is signed', async ({ page }) => {
  refuse = 'hub.not_joined'
  await results(page)
  await page.getByRole('button', { name: 'Publish hello.txt as a link' }).click()
  const panel = page.getByRole('dialog', { name: 'Publish hello.txt as a link?' })
  await panel.getByRole('button', { name: 'Sign and publish' }).click()
  await expect(panel.getByRole('alert')).toContainText('Connect this workspace to a hub in Settings, Hub')
  await page.waitForTimeout(500)
  await page.screenshot({ path: path.join(SHOTS, 'run14-p01-after-refused.png') })
})
