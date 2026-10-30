// R5 T4: drives the remote client's pages in a real Chromium (own temp profile). Started by
// tests/test_remote_client_browser.py with a JSON config on argv[2]; talks to the in-process client only through the
// pages, plus small files the Python side writes (opened.log) and reads (words.json).
import { createRequire } from 'node:module'
import fs from 'node:fs'
const cfg = JSON.parse(process.argv[2])
const { chromium } = createRequire(import.meta.url)(cfg.playwright)
const shot = (p, n) => p.screenshot({ path: `${cfg.shots}/r5-e2e-${n}.png` })
const until = async (fn, ms = 30000) => { const end = Date.now() + ms; for (;;) { const v = await fn(); if (v) return v; if (Date.now() > end) throw new Error('timeout'); await new Promise((r) => setTimeout(r, 100)) } }
const browser = await chromium.launch({ headless: true, args: ['--use-mock-keychain', '--password-store=basic', '--no-first-run', '--disable-extensions'] })
const ctx = await browser.newContext({ viewport: { width: 1100, height: 760 } })
const log = []
try {
  const home = await ctx.newPage()
  home.on('pageerror', (e) => log.push('home pageerror ' + e.message))
  await home.goto(cfg.home)
  await home.getByLabel('Connect link').fill('https://hub.test/c#h1.example')
  await home.getByLabel('User').fill('ann')
  await home.getByLabel('Password').fill('correct horse battery')
  await home.getByLabel('6-digit code').fill('123456')
  await shot(home, '1-connect')
  await home.getByRole('button', { name: 'Connect' }).click()
  await home.getByRole('heading', { name: 'Workspaces' }).waitFor()
  await home.getByRole('button', { name: 'Add this computer' }).waitFor()
  await shot(home, '2-workspaces-not-added')
  // add this computer
  await home.getByRole('button', { name: 'Add this computer' }).click()
  await home.getByPlaceholder('Paste the code here').fill(cfg.code)
  await home.getByRole('button', { name: 'Add', exact: true }).click()
  await home.getByRole('button', { name: /Confirm with/ }).waitFor()
  await shot(home, '3-confirm-add')
  await home.getByRole('button', { name: /Confirm with/ }).click()
  await home.getByLabel('Six words').waitFor()
  const words = await home.locator('ol.words li').evaluateAll((l) => l.map((x) => x.textContent.replace(/^\d+/, '')))
  fs.writeFileSync(cfg.words, JSON.stringify(words))
  await shot(home, '4-six-words')
  await home.getByRole('heading', { name: 'Added' }).waitFor({ timeout: 40000 })
  await shot(home, '5-added')
  await home.getByRole('button', { name: 'Open workspace' }).click()
  await home.getByRole('button', { name: 'Open', exact: true }).click()
  await home.getByRole('button', { name: /Confirm with/ }).waitFor()
  await shot(home, '5b-confirm-open')
  await home.getByRole('button', { name: /Confirm with/ }).click()
  // the session page, opened by the client's redirect file
  const uri = await until(() => { const l = fs.readFileSync(cfg.opened, 'utf8').trim().split('\n').filter(Boolean); return l.length >= 2 ? l[l.length - 1] : null })
  const ws = await ctx.newPage()
  ws.on('pageerror', (e) => log.push('ws pageerror ' + e.message))
  ws.on('console', (m) => { if (m.type() === 'error') log.push('ws console ' + m.text()) })
  await ws.goto(uri)
  ws.on('response', (r) => { if (r.status() >= 400) log.push(`ws ${r.status()} ${r.url()}`) })
  await ws.getByText('Billing Service').first().waitFor()
  await until(async () => (await ws.locator('body').innerText()).trim().length > 0)
  await ws.waitForTimeout(1500)
  await shot(ws, '6-workspace-through-the-session')
  // a presence-gated action from the workspace page
  const pending = ws.evaluate(async (w) => {
    const token = sessionStorage.getItem('orch-session')
    const r = await fetch(`/api/w/${w}/agents/start`, { method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: '{"model":"sonnet"}' })
    return r.status
  }, cfg.ws)
  await home.bringToFront()
  await home.getByRole('heading', { name: new RegExp(`${cfg.ownName} asks you to confirm`) }).waitFor({ timeout: 30000 })
  await shot(home, '7-confirm-action-own-name')
  await home.getByRole('button', { name: /Confirm with/ }).click()
  const status = await pending
  fs.writeFileSync(cfg.result, JSON.stringify({ status, log }))
  await home.getByRole('heading', { name: 'Workspaces' }).waitFor()
  await home.getByRole('button', { name: 'End' }).click()
  await home.getByRole('button', { name: 'End' }).waitFor({ state: 'detached' })
  await shot(home, '8-ended')
} catch (e) {
  for (const pg of ctx.pages()) { log.push(`PAGE ${pg.url()} :: ${(await pg.locator('body').innerText().catch(() => '')).slice(0, 300)}`); await pg.screenshot({ path: `${cfg.shots}/r5-e2e-FAIL-${ctx.pages().indexOf(pg)}.png` }).catch(() => null) }
  console.log(log.join('\n')); throw e
} finally {
  if (!fs.existsSync(cfg.result)) fs.writeFileSync(cfg.result, JSON.stringify({ status: null, log }))
  await browser.close()
}
