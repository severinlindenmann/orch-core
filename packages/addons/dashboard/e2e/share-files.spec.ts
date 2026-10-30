// P05: Share files from a ticket without the Terminal. Add a file, Publish found in Browse, the Actions entry. Screenshots
// go to SHOTS_DIR (run12-p05-*.png). The share routes are answered by a thin layer in front of the shared fake server.
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { expect, test } from '@playwright/test'
import { FakeServer, WS } from './fakeHost'

const SHOTS = process.env.SHOTS_DIR ?? path.resolve(import.meta.dirname, 'shots')
const fixture = (n: string) => JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, '../src/test/fixtures/server-m2', n), 'utf8'))
let fake: FakeServer
let base = ''
const uploads: Record<string, any>[] = []
const prepares: Record<string, any>[] = []
let publishOn = false

const addOns = () => ({
  available: true,
  add_ons: [{ id: 'publish', title: 'Publish', desc: 'Share files of a ticket with a link. Nothing leaves this computer until you sign.', state: publishOn ? 'on' : 'off', locked: null, blocked: null, needs: 'publish', set_up: true }],
})

test.beforeAll(async () => {
  fake = new FakeServer(path.resolve(import.meta.dirname, '../dist'))
  fake.clickOn = true
  base = await fake.start()
  const inner = (fake as unknown as { handle: (q: http.IncomingMessage, r: http.ServerResponse) => Promise<void> }).handle.bind(fake)
  fake.server.removeAllListeners('request')
  fake.server.on('request', (req, res) => {
    void (async () => {
      const url = new URL(req.url ?? '/', 'http://x')
      const rest = new RegExp(`^/api/w/${WS}/(.*)$`).exec(url.pathname)?.[1]
      const method = req.method ?? 'GET'
      const json = (v: unknown) => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(v)) }
      if (rest === undefined) return inner(req, res)
      if (rest === 'add-ons' && method === 'GET') return json(addOns())
      if (rest === 'tickets/DEMO-0001' && method === 'GET') return json({ ...fixture('ticket.json'), artifacts: [{ name: 'report.md', kind: 'report', bytes: 2048, sha256: 'ab'.repeat(32), added_by: 'p_x', at: '2026-10-18T08:00:00Z' }] })
      if (rest === 'shares' && method === 'GET') return json({ available: true, configured: true, server: 'https://share.example.org', share: 'https://link.example.org', workspace: WS, owner: 'p_owner', live: true, shares: [], staged: [] })
      if (rest.startsWith('shares/') && method === 'POST') {
        const raw = await new Promise<string>((r) => { let s = ''; req.on('data', (c) => (s += c)); req.on('end', () => r(s)) })
        const body = JSON.parse(raw)
        if (rest === 'shares/upload') { uploads.push(body); return json({ upload_id: 'pu_' + '1'.repeat(32), name: body.name, size: Buffer.from(body.data, 'base64').length, sha256: 'ef'.repeat(32) }) }
        if (rest === 'shares/prepare') { prepares.push(body); return json({ staged: { staged_id: 'ps_' + '2'.repeat(32), ticket: 'DEMO-0001', kind: 'secret', days: 7, views: 0, origin: 'person', files: [{ name: 'readme.md', size: 2048, sha256: 'ef'.repeat(32) }], created_at: null }, server: null, share: null }) }
        if (rest === 'shares/discard') return json({ discarded: true })
      }
      return inner(req, res)
    })()
  })
})
test.afterAll(() => fake.stop())

const open = async (page: import('@playwright/test').Page, to: string) => {
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message))
  await page.goto(`${base}/#launch=demo-not-a-real-code&to=${encodeURIComponent(to)}`)
}

test('Publish is in Browse with Turn on; the ticket Actions menu has Share files', async ({ page }) => {
  publishOn = false
  fake.publishOn = false
  await open(page, `/w/DEMO/addons?tab=browse`)
  const list = page.getByRole('list', { name: 'Built-in add-ons that are off' })
  await expect(list).toContainText('Publish')
  await expect(list.getByRole('button', { name: 'Turn on Publish' })).toBeVisible()
  await page.waitForTimeout(500) // let the dialog finish its fade
  await page.screenshot({ path: path.join(SHOTS, 'run12-p05-after-browse.png') })
  await page.goto(`${base}/#launch=demo-not-a-real-code&to=${encodeURIComponent('/w/DEMO/ticket/DEMO-0001')}`)
  await page.getByRole('button', { name: /Actions/ }).click()
  await expect(page.getByRole('menuitem', { name: 'Share files…' })).toBeVisible()
  await page.waitForTimeout(500) // let the dialog finish its fade
  await page.screenshot({ path: path.join(SHOTS, 'run12-p05-after-actions.png') })
  await page.getByRole('menuitem', { name: 'Share files…' }).click()
  await expect(page.getByRole('dialog', { name: 'Turn on Publish' })).toBeVisible()
  await page.waitForTimeout(500) // let the dialog finish its fade
  await page.screenshot({ path: path.join(SHOTS, 'run12-p05-after-turnon.png') })
})

test('Add a file in Share files: the file goes alone, then Review names it', async ({ page }) => {
  publishOn = true
  fake.publishOn = true
  await open(page, `/w/DEMO/ticket/DEMO-0001`)
  await page.getByRole('button', { name: 'Share files' }).first().click()
  const dialog = page.getByRole('dialog', { name: 'Share files' })
  await dialog.getByTestId('share-file-input').setInputFiles({ name: 'readme.md', mimeType: 'text/markdown', buffer: Buffer.from('# Readme\n'.repeat(200)) })
  await expect(dialog.getByRole('checkbox', { name: 'readme.md from this computer' })).toBeChecked()
  expect(uploads).toHaveLength(1)
  expect(uploads[0]).toMatchObject({ name: 'readme.md' })
  await page.waitForTimeout(500) // let the dialog finish its fade
  await page.screenshot({ path: path.join(SHOTS, 'run12-p05-after-share-dialog.png') })
  await dialog.getByRole('button', { name: 'Review selected files' }).click()
  await expect(page.getByRole('dialog', { name: 'Review your share' })).toBeVisible()
  expect(prepares[0]).toMatchObject({ ticket: 'DEMO-0001', uploads: ['pu_' + '1'.repeat(32)] })
  await page.waitForTimeout(500) // let the dialog finish its fade
  await page.screenshot({ path: path.join(SHOTS, 'run12-p05-after-review.png') })
})
