#!/usr/bin/env node
// Layout guard (N11): every page must fit a 13" notebook with the terminal docked on the right.
//
// Renders every route of the running dev server in headless Chrome (plain node, Chrome DevTools Protocol, no
// Playwright) at 1440x900 and 1470x956, with the right dock open at its minimum, default and maximum width, the
// sidebar wide and as the rail, for the Normal and the Busy day. On each page it looks for horizontal overflow:
// the document scrolling sideways, or any element that scrolls sideways (overflow-x auto/scroll with
// scrollWidth > clientWidth). Terminals, code blocks, diffs, log viewers and tab rows may scroll inside themselves
// (see ALLOWED below; a component opts in with `data-scroll-x`). Exits 1 when anything overflows.
//
//   npm run dev -- --port 5201 --strictPort --host 127.0.0.1     # in another terminal
//   npm run layout:guard                                           # all configurations (a few minutes)
//   npm run layout:guard -- --quick                                # 1440x900, default dock, sidebar left to choose, Normal
//   npm run layout:guard -- --only tickets --shots ./shots         # routes matching /tickets/, with screenshots
//
// Options: --url <base> (default http://127.0.0.1:5201), --quick, --only <regex>, --dataset normal|busy, --docks min,default,max,
// --selftest (plants overflow on a page and checks the detector finds it; then reruns the guard with --break tabs|overlay|loading|tabrow and expects exit 1 with the reason), --shots <dir> (a PNG per page of the first configuration), --prefix <name> (screenshot file prefix),
// --chrome <path> (or CHROME_PATH). Needs the dev build: it moves between routes through `window.__orchRouter` (main.tsx). jsdom cannot lay out, so this runs against the real dev server.

import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { waitForTarget, waitForDevTools } from './layout-startup.mjs'
import { selectsRealHost } from './layout-guard-pages.mjs'

const argv = process.argv.slice(2)
const opt = (name, fallback) => {
  const i = argv.indexOf(`--${name}`)
  return i >= 0 ? argv[i + 1] : fallback
}
const flag = (name) => argv.includes(`--${name}`)

const BASE = opt('url', process.env.LAYOUT_URL ?? 'http://127.0.0.1:5201')
const ONLY = opt('only') ? new RegExp(opt('only')) : null
const SHOTS = opt('shots')
const PREFIX = opt('prefix', 'page')
const QUICK = flag('quick')
/** Self-test only: break one step on purpose ('tabs', 'overlay' or 'loading') to prove the guard then fails. */
const BREAK = opt('break')
/** DEMO-0043's tabs: Overview, Acceptance & tasks, Questions, Artifacts, History, Raw. */
const TICKET_TABS = 6

const VIEWPORTS = QUICK ? [[1440, 900]] : [[1440, 900], [1470, 956]]
const DOCKS = opt('docks') ? opt('docks').split(',') : QUICK ? ['default'] : ['min', 'default', 'max']
// 'auto' = no choice made (beside the dock the sidebar becomes the rail by itself); 'wide' and 'rail' = the person's choice.
const SIDEBARS = QUICK ? ['auto'] : ['wide', 'rail']
const DATASETS = opt('dataset') ? [opt('dataset')] : QUICK ? ['normal'] : ['normal', 'busy']

// Places that may scroll sideways inside themselves.
const ALLOWED = [
  '[aria-label="Terminal dock"]', // the dock: terminals
  '.xterm',
  'pre',
  'code',
  '[role="tablist"]', // tab rows scroll within their row
  '[data-scroll-x]', // a component's own scroller (diffs, logs, raw views)
].join(',')

// ------------------------------------------------------------------ Chrome over CDP

function findChrome() {
  const env = opt('chrome', process.env.CHROME_PATH)
  if (env) return env
  const candidates = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
  ]
  // Playwright's browser cache, when one is there.
  const cache = join(homedir(), process.platform === 'darwin' ? 'Library/Caches/ms-playwright' : '.cache/ms-playwright')
  if (existsSync(cache)) {
    for (const d of readdirSync(cache).filter((n) => /^chromium-\d+$/.test(n)).sort().reverse()) {
      candidates.push(join(cache, d, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium'), join(cache, d, 'chrome-linux/chrome'))
    }
  }
  const hit = candidates.find((p) => existsSync(p))
  if (!hit) throw new Error('No Chrome found: pass --chrome <path> or set CHROME_PATH')
  return hit
}

async function launch() {
  const chrome = findChrome()
  const profile = mkdtempSync(join(tmpdir(), 'layout-guard-'))
  const proc = spawn(chrome, [
    '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check',
    '--hide-scrollbars=false', '--force-device-scale-factor=1', 'about:blank',
  ], { stdio: 'ignore' })
  let ws
  let closed = false
  let spawnError
  proc.on('error', (error) => { spawnError = error })
  const onExit = () => close()
  const onInt = () => { close(); process.exit(130) }
  const onTerm = () => { close(); process.exit(143) }
  const close = () => {
    if (closed) return
    closed = true
    try { ws?.close() } catch { /* cleanup must still terminate Chrome */ }
    proc.kill('SIGKILL')
    process.removeListener('exit', onExit)
    process.removeListener('SIGINT', onInt)
    process.removeListener('SIGTERM', onTerm)
    try { rmSync(profile, { recursive: true, force: true }) } catch { /* OS may hold the profile */ }
  }
  process.once('exit', onExit)
  process.once('SIGINT', onInt)
  process.once('SIGTERM', onTerm)
  try {
    const page = await waitForDevTools(join(profile, 'DevToolsActivePort'), { alive: () => !spawnError && proc.exitCode === null && proc.signalCode === null })
    ws = new WebSocket(page.webSocketDebuggerUrl)
    await new Promise((ok, fail) => {
      const timeout = setTimeout(() => fail(new Error('Chrome DevTools WebSocket did not become ready')), 10000)
      ws.onopen = () => { clearTimeout(timeout); ok() }
      ws.onerror = (error) => { clearTimeout(timeout); fail(error) }
    })
    let id = 0
    const waiting = new Map()
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data)
      if (msg.id && waiting.has(msg.id)) {
        const { ok, fail } = waiting.get(msg.id)
        waiting.delete(msg.id)
        msg.error ? fail(new Error(msg.error.message)) : ok(msg.result)
      }
    }
    const send = (method, params = {}) =>
      new Promise((ok, fail) => {
        waiting.set(++id, { ok, fail })
        ws.send(JSON.stringify({ id, method, params }))
      })
    return { send, close }
  } catch (error) {
    close()
    throw error
  }
}

// ------------------------------------------------------------------ page helpers

function makePage(send) {
  const evaluate = async (fn, ...args) => {
    // The dev server may reload the page (a full HMR reload): wait for it and try once more.
    try {
      return await evaluateOnce(fn, ...args)
    } catch (e) {
      if (!/navigated or closed|context was destroyed|Cannot find context/i.test(String(e))) throw e
      await sleep(2000)
      return evaluateOnce(fn, ...args)
    }
  }
  const evaluateOnce = async (fn, ...args) => {
    const r = await send('Runtime.evaluate', { expression: `(${fn})(...${JSON.stringify(args)})`, awaitPromise: true, returnByValue: true })
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text)
    return r.result.value
  }
  /** Waits until the page settled: no page skeleton, no pending lazy chunk, layout stable for a moment. */
  const settle = () =>
    evaluate(async () => {
      const loadingText = () => [...document.querySelectorAll('main p')].some((p) => /^Loading\b.*…$/.test(p.textContent.trim()))
      const still = () => document.querySelector('[aria-label="Loading page"], main [data-slot="skeleton"]') || !document.querySelector('main#main') || loadingText()
      for (let i = 0; i < 150 && still(); i++) await new Promise((r) => setTimeout(r, 100))
      let last = ''
      for (let i = 0; i < 20; i++) {
        await new Promise((r) => setTimeout(r, 150))
        const now = `${document.body.scrollHeight}:${document.querySelectorAll('*').length}`
        if (now === last) break
        last = now
      }
      // What is still loading, for the report ('' = nothing).
      if (!still()) return ''
      const sk = document.querySelector('main [data-slot="skeleton"]')
      return document.querySelector('[aria-label="Loading page"]') ? 'the page chunk' : sk ? `a skeleton (${sk.getAttribute('aria-label') ?? sk.parentElement?.className ?? ''})` : loadingText() ? 'a "Loading…" text' : 'no main'
    })
  return { evaluate, settle }
}

/**
 * In the page: everything that scrolls sideways where it should not (problems), and content cut off without saying so
 * (clipped: overflow-x hidden/clip with wider content that is not an ellipsis or a line clamp by design). A clipped
 * box that cuts off a button or a link is a problem too; other clipping is a warning.
 */
function findOverflow(allowed) {
  const describe = (el) => {
    const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).slice(0, 6).join('.') : ''
    const label = el.getAttribute('aria-label') || el.getAttribute('data-slot') || ''
    const text = (el.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 50)
    return `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}${cls ? `.${cls}` : ''}${label ? ` [${label}]` : ''} "${text}"`
  }
  const out = []
  const doc = document.scrollingElement ?? document.documentElement
  // The whole page (also when html/body clip it: then the sidebar or topbar is cut off instead).
  const docWidth = Math.max(doc.scrollWidth, document.body.scrollWidth)
  if (docWidth > window.innerWidth + 1) {
    out.push(`document scrolls sideways (${docWidth} > ${window.innerWidth})`)
    // Name the outermost elements that reach past the window, to find the culprit.
    const past = [...document.querySelectorAll('body *')].filter((el) => el.getBoundingClientRect().right > window.innerWidth + 1)
    for (const el of past.filter((el) => !past.includes(el.parentElement)).slice(0, 3)) out.push(`  past the window edge: ${describe(el)} (right ${Math.round(el.getBoundingClientRect().right)})`)
  }
  for (const el of document.querySelectorAll('body *')) {
    if (el.scrollWidth <= el.clientWidth + 1 || el.clientWidth === 0) continue
    if (el.closest(allowed)) continue
    const ox = getComputedStyle(el).overflowX
    if (ox !== 'auto' && ox !== 'scroll') continue
    out.push(`${describe(el)} scrolls sideways (${el.scrollWidth} > ${el.clientWidth})`)
  }
  const warnings = []
  for (const el of document.querySelectorAll('body *')) {
    if (el.scrollWidth <= el.clientWidth + 1 || el.clientWidth <= 2) continue
    if (el.closest(allowed) || el.closest('[aria-hidden="true"], [inert]')) continue
    const cs = getComputedStyle(el)
    if (cs.overflowX !== 'hidden' && cs.overflowX !== 'clip') continue
    // By design: an ellipsis, a line clamp, a screen-reader-only text, a progress bar track.
    if (cs.textOverflow === 'ellipsis' || cs.webkitLineClamp !== 'none' || cs.position === 'absolute' || el.getAttribute('role') === 'progressbar') continue
    const box = el.getBoundingClientRect()
    const cut = [...el.querySelectorAll('a, button, [role="button"], [role="tab"]')].filter((c) => {
      const r = c.getBoundingClientRect()
      return r.width > 0 && (r.right > box.right + 1 || r.left < box.left - 1)
    })
    if (cut.length) out.push(`${describe(el)} cuts off ${cut.length} button/link(s), e.g. ${describe(cut[0])}`)
    else warnings.push(`${describe(el)} clips its content (${el.scrollWidth} > ${el.clientWidth})`)
  }
  const main = document.querySelector('main#main')
  return { problems: out, warnings, pageWidth: main ? Math.round(main.getBoundingClientRect().width) : 0 }
}

// ------------------------------------------------------------------ the run

async function main() {
  await waitForTarget(BASE)
  const { send, close } = await launch()
  try {
    await run(send, close)
  } finally {
    close()
  }
}

async function run(send, close) {
  const { evaluate, settle } = makePage(send)
  await send('Page.enable')
  await send('Runtime.enable')
  const failures = []
  let checked = 0
  let warned = 0
  let shotsTaken = false

  const shoot = async (name) => {
    if (!SHOTS || shotsTaken) return
    mkdirSync(SHOTS, { recursive: true })
    const { data } = await send('Page.captureScreenshot', { format: 'png' })
    writeFileSync(join(SHOTS, `${PREFIX}-${name}.png`), Buffer.from(data, 'base64'))
  }

  // Set when the last navigation was still loading after the wait: its check fails rather than passing an empty page.
  let stillLoading = ''
  const go = async (path) => {
    await evaluate((p) => window.__orchRouter.history.push(p), path)
    // Self-test: a page that never finishes loading.
    if (BREAK === 'loading') await evaluate(() => setTimeout(() => document.querySelector('main')?.prepend(Object.assign(document.createElement('p'), { textContent: 'Loading forever…' })), 0))
    // A loaded machine can be slow: one more round before calling it stuck.
    stillLoading = (await settle()) && (await settle())
  }

  const check = async (config, name, expect) => {
    checked++
    const { problems, warnings, pageWidth } = await evaluate(findOverflow, ALLOWED)
    // A step that did not reach its page (missing tabs, no overlay) fails instead of passing on the wrong page.
    if (expect) problems.unshift(...expect)
    if (stillLoading) problems.unshift(`the page was still loading after 30 s: ${stillLoading}`)
    const status = problems.length ? 'FAIL' : warnings.length ? 'warn' : 'ok'
    console.log(`  ${status.padEnd(4)} ${name} (page ${pageWidth}px)`)
    for (const p of problems) console.log(`         ${p}`)
    for (const w of warnings) console.log(`         warning: ${w}`)
    warned += warnings.length
    if (problems.length) failures.push({ config, name, problems })
    await shoot(name)
  }

  if (flag('selftest')) {
    // Proves the detector: plants a sideways scroller, a cut-off button, silent clipping and an ellipsis on Today.
    await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })
    await send('Page.navigate', { url: BASE })
    await sleep(1500)
    await settle()
    const r = await evaluate((find, allowed) => {
      const m = document.querySelector('main')
      const plant = (css, html) => {
        const d = document.createElement('div')
        d.style.cssText = `width:100px;white-space:nowrap;${css}`
        d.innerHTML = html
        m.prepend(d)
        return d
      }
      const planted = [
        plant('overflow-x:auto', 'SCROLLER a very long line that scrolls'),
        plant('overflow:hidden', '<button style="width:300px">CUTBUTTON</button>'),
        plant('overflow:hidden', 'SILENTCLIP a very long line that is cut off'),
        plant('overflow:hidden;text-overflow:ellipsis', 'ELLIPSIS a very long line with an ellipsis'),
      ]
      const res = new Function(`return (${find})`)()(allowed)
      planted.forEach((d) => d.remove())
      const has = (list, word) => list.some((x) => x.includes(word))
      return { scroller: has(res.problems, 'SCROLLER'), cut: has(res.problems, 'CUTBUTTON'), clip: has(res.warnings, 'SILENTCLIP'), ellipsis: has([...res.problems, ...res.warnings], 'ELLIPSIS') }
    }, findOverflow.toString(), ALLOWED)
    close()
    let ok = r.scroller && r.cut && r.clip && !r.ellipsis
    console.log(`selftest detector: ${JSON.stringify(r)} → ${ok ? 'ok' : 'FAIL'}`)
    // The steps that must not pass silently: run the guard with each one broken; it has to exit 1 and say why.
    const runs = [
      ['tabs', 'ticket-overview', /expected 6 ticket tabs/],
      ['overlay', 'new-ticket-overlay', /New ticket overlay \(\[role=dialog\]\) did not open/],
      ['loading', 'tickets', /still loading after 30 s: a "Loading…" text/],
      ['tabrow', 'ticket-overview', /ticket tab row disappeared/],
    ]
    for (const [what, only, says] of runs) {
      const child = spawn(process.execPath, [process.argv[1], '--quick', '--url', BASE, '--only', `^(${only}|ticket-tabs)$`, '--break', what], { stdio: ['ignore', 'pipe', 'pipe'] })
      let out = ''
      child.stdout.on('data', (d) => (out += d))
      child.stderr.on('data', (d) => (out += d))
      const code = await new Promise((res) => child.on('close', res))
      const pass = code === 1 && says.test(out)
      ok &&= pass
      console.log(`selftest --break ${what}: exit ${code}, ${says.test(out) ? 'reason reported' : 'reason missing'} → ${pass ? 'ok' : 'FAIL'}`)
    }
    process.exitCode = ok ? 0 : 1
    return
  }

  for (const dataset of DATASETS) {
    for (const [width, height] of VIEWPORTS) {
      for (const dock of DOCKS) {
        for (const sidebar of SIDEBARS) {
          const config = `${dataset} ${width}x${height} dock-${dock} sidebar-${sidebar}`
          console.log(`\n${config}`)
          await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false })
          await send('Page.navigate', { url: BASE })
          await sleep(300)
          await evaluate(
            (ds, dockW, sidebar) => {
              localStorage.clear()
              localStorage.setItem('orch-mock-v2', JSON.stringify({ v: 2, ticketEvents: {}, created: {}, wsEvents: {}, addonState: {}, dataset: ds }))
              localStorage.setItem('orch.dock.p_sev', JSON.stringify({ side: 'right', open: true, bottom: 280, right: dockW, harness: 'claude' }))
              if (sidebar !== 'auto') {
                localStorage.setItem('orch.sidebar.p_sev', sidebar === 'rail' ? 'narrow' : 'wide')
                localStorage.setItem('orch.sidebar.docked.p_sev', sidebar === 'rail' ? 'narrow' : 'wide')
              }
            },
            dataset,
            { min: 0, default: 440, max: 99999 }[dock],
            sidebar,
          )
          await send('Page.reload', { ignoreCache: false })
          await sleep(500)
          await settle()

          // The routes: core pages, every addon page (also those under "More addons"), settings, the ticket tabs.
          const addonPages = await evaluate(async () => {
            const more = [...document.querySelectorAll('button')].find((b) => /^More addons/.test(b.getAttribute('aria-label') ?? ''))
            if (more) {
              more.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0, pointerType: 'mouse' }))
              more.click()
              await new Promise((r) => setTimeout(r, 300))
            }
            // Links carry the workspace (/w/DEMO/addon/…); the guard goes by the in-app path, which the app redirects back.
            const hrefs = [...new Set([...document.querySelectorAll('a[href*="/addon/"]')].map((a) => a.getAttribute('href').replace(/^\/w\/[^/]+(?=\/addon\/)/, '')).filter((h) => h.startsWith('/addon/')))]
            document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
            document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
            return hrefs
          })
          const addonNames = [...new Set(addonPages.map((h) => h.split('/')[2]))]
          const routes = [
            ['today', '/'],
            ['board', '/board'],
            ['tickets', '/tickets'],
            ['artifacts', '/artifacts'],
            ['agents', '/agents'],
            ['agents-mandates', '/agents?tab=mandates'],
            ['new-ticket-page', '/tickets/new'],
            ...addonPages.map((h) => [`addon-${h.split('/').slice(2).join('-')}`, h]),
            // Repos details must fit beside the maximum dock too (720 px page).
            ['repos-expanded', '/addon/repos/repos?tab.repos=structure&row=web-portal'],
            ['repos-ssh', '/addon/repos/repos?tab.repos=structure&row=infra'],
            ['repos-checks', '/addon/repos/repos?tab.repos=checks'],
            ['repos-activity', '/addon/repos/repos?tab.repos=activity'],
            ...['general', 'members', 'gates', 'relay', 'addons', 'skills', 'connections'].map((t) => [`settings-${t}`, `/settings/${t}`]),
            ...addonNames.map((n) => [`settings-addon-${n}`, `/settings/addon/${n}`]),
          ]
          for (const [name, path] of routes) {
            if (ONLY && !ONLY.test(name)) continue
            await go(path)
            await check(config, name)
          }

          // The ticket page, tab by tab.
          if (!ONLY || ONLY.source.includes('ticket-')) {
            await go('/w/DEMO/ticket/DEMO-0043')
            // The ticket's own tab row: the first tablist in the page.
            // Self-test: the tab row went missing.
            if (BREAK === 'tabs') await evaluate(() => document.querySelector('main [role="tablist"]')?.remove())
            const tabs = await evaluate(() => [...(document.querySelector('main [role="tablist"]')?.querySelectorAll('[role="tab"]') ?? [])].map((t) => t.textContent.trim()))
            if (tabs.length < TICKET_TABS && (!ONLY || ONLY.test('ticket-tabs'))) await check(config, 'ticket-tabs', [`expected ${TICKET_TABS} ticket tabs on DEMO-0043, found ${tabs.length}`])
            for (let i = 0; i < tabs.length; i++) {
              const name = `ticket-${tabs[i].toLowerCase().replace(/[^a-z]+.*$/, '')}`
              if (ONLY && !ONLY.test(name)) continue
              // The tab row can vanish between steps (a hot reload, a page that re-renders): a failing check, never a crash.
              // Self-test: the tab row vanishes after it was read.
              if (BREAK === 'tabrow') await evaluate(() => document.querySelector('main [role="tablist"]')?.remove())
              const found = await evaluate(async (n) => {
                const tab = document.querySelector('main [role="tablist"]')?.querySelectorAll('[role="tab"]')[n]
                if (!tab) return false
                tab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 }))
                tab.focus()
                tab.click()
                return true
              }, i)
              if (!found) {
                await check(config, name, [`ticket tab row disappeared (before opening the ${tabs[i]} tab)`])
                continue
              }
              await settle()
              const selected = await evaluate((n) => document.querySelector('main [role="tablist"]')?.querySelectorAll('[role="tab"]')[n]?.getAttribute('aria-selected') === 'true', i)
              await check(config, name, selected ? undefined : [`the ${tabs[i]} tab did not open`])
            }
          }

          // The New ticket overlay over a page.
          if (!ONLY || ONLY.test('new-ticket-overlay')) {
            await go('/tickets')
            const BREAK_OVERLAY = BREAK === 'overlay'
            await evaluate((BREAK_OVERLAY) => {
              document.activeElement?.blur()
              if (!BREAK_OVERLAY) window.dispatchEvent(new KeyboardEvent('keydown', { key: 'c', bubbles: true }))
            }, BREAK_OVERLAY)
            await settle()
            const open = await evaluate(() => !!document.querySelector('[role="dialog"]'))
            await check(config, 'new-ticket-overlay', open ? undefined : ['the New ticket overlay ([role=dialog]) did not open'])
            await evaluate(() => document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
          }

          // The review tour sheet (Demo data pill), one scenario open.
          if (!ONLY || ONLY.test('review-tour')) {
            await go('/')
            await evaluate(async () => {
              ;[...document.querySelectorAll('header button')].find((b) => /^Review tour/.test(b.textContent.trim()))?.click()
              await new Promise((r) => setTimeout(r, 300))
              const sheet = document.querySelector('[role="dialog"]')
              const head = sheet && [...sheet.querySelectorAll('button[aria-expanded="false"]')].find((b) => /^7\. /.test(b.textContent.trim()))
              head?.click()
            })
            await settle()
            const open = await evaluate(() => !!document.querySelector('[role="dialog"] [aria-expanded="true"]'))
            await check(config, 'review-tour', open ? undefined : ['the Review tour sheet did not open'])
            await evaluate(() => document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
          }
          // Mandates, PREVIEW: the Demo data toggle turns it on with a mandate in force (banner on every page, the digest on
          // Today, the mandate on Agents → Mandates). Checked with the banner, then turned off again.
          if (!ONLY || ONLY.test('mandates-preview')) {
            await go('/')
            const turnedOn = await evaluate(async () => {
              const b = [...document.querySelectorAll('header button')].find((x) => x.textContent.trim() === 'Preview: mandates')
              if (!b) return false
              if (b.getAttribute('aria-pressed') !== 'true') b.click()
              for (let i = 0; i < 50 && !document.querySelector('[data-testid="mandate-banner"]'); i++) await new Promise((r) => setTimeout(r, 100))
              return !!document.querySelector('[data-testid="mandate-banner"]')
            })
            for (const [name, path] of [['mandates-preview-today', '/'], ['mandates-preview-agents', '/agents?tab=mandates'], ['mandates-preview-board', '/board']]) {
              await go(path)
              const banner = await evaluate(() => {
                const el = document.querySelector('[data-testid="mandate-banner"]')
                return el ? { h: el.getBoundingClientRect().height, cut: el.scrollWidth > el.clientWidth + 1 } : null
              })
              const problems = !turnedOn ? ['the Preview: mandates toggle did not show the banner'] : !banner ? ['the mandate banner is missing'] : banner.h > 40 ? [`the mandate banner wraps (${banner.h}px high)`] : banner.cut ? ['the mandate banner overflows sideways'] : undefined
              await check(config, name, problems)
            }
            await evaluate(async () => {
              const b = [...document.querySelectorAll('header button')].find((x) => x.textContent.trim() === 'Preview: mandates')
              if (b?.getAttribute('aria-pressed') === 'true') b.click()
              await new Promise((r) => setTimeout(r, 600))
            })
          }

          // The real-host-only pages (quick tasks, the Records tab, the runner card), drawn from fixtures by the dev-only
          // `__layout/<scenario>` route (src/dev/layoutPreview.tsx): the mock app never shows them.
          if (selectsRealHost(ONLY)) {
            const clickText = (sel, rx) =>
              evaluate(async (sel, rx) => {
                const el = [...document.querySelectorAll(sel)].find((b) => new RegExp(rx).test((b.getAttribute('aria-label') || b.textContent).trim()))
                if (!el) return false
                el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 }))
                el.click()
                await new Promise((r) => setTimeout(r, 400))
                return true
              }, sel, rx.source)
            // Click the first element a selector matches (a switch has no text of its own).
            const clickSel = (sel) => clickText(sel, /[\s\S]*/)
            // Fixture answers arrive a moment after the page: wait for the element (up to 8 s) before calling it missing, then let the layout settle.
            const present = async (sel) => {
              const found = await evaluate(async (s) => {
                for (let i = 0; i < 80 && !document.querySelector(s); i++) await new Promise((r) => setTimeout(r, 100))
                return !!document.querySelector(s)
              }, sel)
              if (found) await settle()
              return found
            }
            const need = async (list) => {
              const out = []
              for (const [what, sel] of list) if (!(await present(sel))) out.push(`${what} did not show (${sel})`)
              return out.length ? out : undefined
            }
            const escape = () => evaluate(() => document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
            const QUICK_UI = [['the add form', '[aria-label="Add a quick task"]']]
            for (const [name, scenario, shown] of [
              ['real-host-quick', 'quick-normal', QUICK_UI],
              ['real-host-quick-many', 'quick-many', QUICK_UI],
              ['real-host-records', 'records-ok', [['the export warning', '[data-testid="records-export-warning"]'], ['the private-remote line', '[data-testid="records-private-line"]']]],
              ['real-host-records-hidden-url', 'records-hidden-url', [['the export warning', '[data-testid="records-export-warning"]'], ['the blocked line', '[data-testid="records-export-blocked"]']]],
              ['real-host-records-first-commit', 'records-first', [['the no-repository line', '[data-testid="records-not-configured"]']]],
              ['real-host-addons', 'addons-mixed', [['the terminals row', '[data-testid="addon-terminals"]'], ['the switch', '[role="switch"]']]],
              ['real-host-runner-on', 'runner-on', [['the dashboard-execution line', '[data-testid="runner-execution-line"]']]],
              ['real-host-runner-off', 'runner-off', [['the runner card', '[data-testid="workspace-card-runner"]']]],
              // M7a: the real-host terminal dock (fixture runs and a fixture socket; nothing reaches a host).
              ['real-host-terminal-dock', 'terminal-dock', [['the real dock', '[data-real-dock]'], ['the terminal', '[data-real-terminal] .xterm']]],
              ['real-host-terminal-preview', 'terminal-preview', [['the preview dialog', '[data-term-preview]']]],
              ['real-host-terminal-readonly', 'terminal-readonly', [['the read-only banner', '[data-lease="reading"]']]],
              ['real-host-terminal-take', 'terminal-take', [['the Take control banner', '[data-lease="busy"]']]],
              ['real-host-terminal-exited', 'terminal-exited', [['the output-gone line', '[data-output-gone]']]],
              ['real-host-terminal-stop', 'terminal-stop', [['the cleanup line', '[data-cleanup-doubt]']]],
              // M7b: core's Start agent dialog (fixture preview, runner and grant answers; nothing reaches a host).
              ['real-host-agent-dialog', 'agent-dialog', [['the dialog', '[data-start-agent]'], ['the first-time notice', '[data-testid="agent-risk-full"]']]],
              ['real-host-agent-dialog-acked', 'agent-dialog-acked', [['the dialog', '[data-start-agent]'], ['the details line', '[data-testid="agent-details"] summary']]],
              ['real-host-agent-model', 'agent-model', [['the dialog', '[data-start-agent]'], ['the model line', '[data-testid="agent-model-line"]']]],
              ['real-host-addons-publish', 'addons-publish', [['the settings chevron', '[data-testid="addon-publish-expand"]']]],
              ['real-host-addons-publish-open', 'addons-publish-open', [['the publish box', '[data-testid="publish-server"]'], ['the connection check', '[data-testid="publish-connection"]']]],
              ['real-host-addons-publish-unset', 'addons-publish-unset', [['the publish row', '[data-testid="addon-publish"]']]],
              ['real-host-agent-blocked', 'agent-blocked', [['the runner-off line', '[data-testid="agent-runner-off"]'], ['the enable command', '[data-testid="runner-enable-command"]']]],
              ['real-host-agent-grant', 'agent-grant', [['the access length', '[data-testid="agent-access"]'], ['the sign-and-start button', '[data-testid="agent-start"]']]],
              // Pair a phone (proposed pair/1): Settings → Devices from the fixture pairing api.
              ['real-host-pair-unavailable', 'pair-unavailable', [['the not-available line', '[data-testid="pair-unavailable"]']]],
              ['real-host-pair-unavailable-click', 'pair-unavailable-click', [['the not-available line', '[data-testid="pair-unavailable"]']]],
              ['real-host-pair-blocked-convenience', 'pair-blocked-convenience', [['the convenience explanation', '[data-testid="pair-blocked-convenience"]']]],
              ['real-host-pair-offer', 'pair-offer', [['the warnings', '[data-testid="pair-warnings"]'], ['the device list', '[data-testid="device-row"]']]],
              ['real-host-pair-pending', 'pair-pending', [['the code entry', '#pair-sas'], ['the label', '[data-testid="pair-label"]']]],
              ['real-host-pair-cert', 'pair-cert', [['the cert code', '[data-testid="pair-cert-code"]'], ['the primary command', '[data-testid="pair-primary-command"]']]],
              ['real-host-pair-failed', 'pair-failed', [['the remove-the-device line', '[data-testid="pair-needs-remove"]']]],
              ['real-host-pair-devices', 'pair-devices', [['the device list', '[data-testid="device-row"]']]],
              // Core H12d-2: reader levels on Settings → Devices, and every click-to-sign refusal sentence.
              ['real-host-readers-devices', 'readers-devices', [['the reader rows', '[data-testid="reader-row"]'], ['a device below the level', '[data-testid="reader-row"][data-below="true"]'], ['the required level', '[data-testid="readers-required"]']]],
              ['real-host-convenience-refusals', 'convenience-refusals', [['the refusals', '[data-testid="refusal-gallery"] [data-testid="click-problem"]'], ['the devices by label', '[data-testid="refusal-devices"]'], ['readers_old', '[data-testid="signer-readers-old"]']]],
              // Core D1 (#582): the dev sandbox host. The badge sits in the topbar of every page.
              ['real-host-dev-badge', 'dev-badge', [['the DEV SANDBOX badge', '[data-testid="dev-badge"]'], ['the sandbox id', '[data-testid="dev-badge-sandbox"]']]],
              ['real-host-dev-badge-long', 'dev-badge-long', [['the DEV SANDBOX badge', '[data-testid="dev-badge"]'], ['the long workspace name', '[data-testid="workspace-card-signing"], h2']]],
              ['real-host-dev-alarms', 'dev-alarms', [['the alarms', '[data-testid="host-alarms"] [data-testid="host-alarm"]']]],
              ['real-host-dev-refused', 'dev-refused', [['the refusals', '[data-testid="dev-refused-gallery"] [data-testid="click-problem"]']]],
              ['real-host-signing-dev-host', 'signing-dev-host', [['the signing card', '[data-testid="workspace-card-signing"]'], ['the dev-host explanation', '[data-testid="signing-dev-host"]'], ['the badge', '[data-testid="dev-badge"]']]],
              // Drop (proposed drop/1): Workspace → Drop from the fixture drop api.
              ['real-host-drop-unavailable', 'drop-unavailable', [['the not-available line', '[data-testid="drop-unavailable"]']]],
              ['real-host-drop-list', 'drop-list', [['the rows', '[data-testid="drop-row"]'], ['the upload zone', '[data-testid="drop-zone"]'], ['a disabled-download reason', '[data-testid="drop-download-reason"]'], ['the paging', '[data-testid="drop-next"]']]],
              ['real-host-drop-long-names', 'drop-long-names', [['the names', '[data-testid="drop-name"]'], ['the hidden name', '[data-testid="drop-name-hidden"]'], ['the agent mark', '[data-testid="drop-agent"]']]],
              ['real-host-drop-upload-progress', 'drop-upload-progress', [['the progress', '[data-testid="drop-progress"]'], ['Cancel', '[data-testid="drop-cancel"]']]],
              ['real-host-drop-errors', 'drop-errors', [['the error texts', '[data-testid="drop-error-gallery"] [data-testid="drop-error"]']]],
              ['real-host-drop-versions', 'drop-versions', [['the version list', '[data-testid="drop-version"]'], ['the cap line', '[data-testid="drop-version-cap"]']]],
              ['real-host-drop-phone', 'drop-phone', [['the shared-from line', '[data-testid="drop-shared"]'], ['the gestureless note', '[data-testid="drop-gestureless"]'], ['the pending-others line', '[data-testid="drop-pending-others"]'], ['a hand-off line', '[data-testid="drop-handoff"]']]],
            ]) {
              if (ONLY && !ONLY.test(name)) continue
              await go(`/__layout/${scenario}`)
              await check(config, name, await need(shown))
            }
            // The signing card (#578, #580): fixture card with a long name, prefix and device; the forms are opened by their buttons.
            const typeInto = (sel, value) =>
              evaluate(async (s, v) => {
                const input = document.querySelector(s)
                if (!input) return false
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, v)
                input.dispatchEvent(new Event('input', { bubbles: true }))
                await new Promise((r) => setTimeout(r, 200))
                return true
              }, sel, value)
            const CARD = [['the signing card', '[data-testid="workspace-card-signing"]']]
            if (!ONLY || ONLY.test('real-host-signing-on')) {
              await go('/__layout/signing-on')
              const opened = (await present('[data-testid="workspace-card-signing"]')) && (await clickSel('[data-testid="workspace-card-signing"] button[role="switch"]'))
              await settle()
              await check(config, 'real-host-signing-on', opened ? await need([...CARD, ['the conversion text', '[data-testid="signing-conversion"]'], ['the password field', '#signing-passphrase[type="password"]']]) : ['the One click switch is missing'])
            }
            if (!ONLY || ONLY.test('real-host-signing-off')) {
              await go('/__layout/signing-off')
              const opened = (await present('[data-testid="workspace-card-signing"]')) && (await clickSel('[data-testid="workspace-card-signing"] button[role="switch"]'))
              await settle()
              await check(config, 'real-host-signing-off', opened ? await need([...CARD, ['the off explanation', '[data-testid="signing-off-explain"]'], ['the new passphrase field', '#signing-new-passphrase[type="password"]']]) : ['the One click switch is missing'])
            }
            if (!ONLY || ONLY.test('real-host-signing-terminal-on')) {
              await go('/__layout/signing-terminal-on')
              await check(config, 'real-host-signing-terminal-on', await need([...CARD, ['the terminal-on view', '[data-testid="signing-terminal-on"] code'], ['the copy button', '[data-testid="signing-terminal-on"] button']]))
            }
            if (!ONLY || ONLY.test('real-host-signing-unsafe')) {
              await go('/__layout/signing-unsafe')
              await check(config, 'real-host-signing-unsafe', await need([...CARD, ['the refused-prefix description', '[data-testid="signing-command-unsafe"]']]))
            }
            if (!ONLY || ONLY.test('real-host-signing-unknown')) {
              await go('/__layout/signing-unknown')
              const opened = (await present('[data-testid="workspace-card-signing"]')) && (await clickSel('[data-testid="workspace-card-signing"] button[role="switch"]'))
              const typed = opened && (await typeInto('#signing-passphrase', 'placeholder'))
              const sent = typed && (await clickText('button', /^Turn on$/))
              await settle()
              await check(config, 'real-host-signing-unknown', sent ? await need([...CARD, ['the result-unknown line', '[data-testid="signing-error"]']]) : ['the One click form did not open or send'])
            }
            // Pair a phone: Create pairing link, then Show QR and link: the QR, Copy link and the typed fallback at 720 px.
            if (!ONLY || ONLY.test('real-host-pair-link')) {
              await go('/__layout/pair-offer')
              const created = (await present('[data-testid="pair-before"]')) && (await clickText('button', /^Create pairing link$/))
              const shown = created && (await present('[data-testid="pair-waiting"]')) && (await clickText('button', /^Show QR and link$/))
              await settle()
              await check(config, 'real-host-pair-link', shown ? await need([['the QR', '[data-testid="pair-qr"]'], ['the typed fallback', '[data-testid="pair-fallback"]']]) : ['Create pairing link or Show QR and link is missing'])
            }
            // Drop: the Delete confirmation open at 720 px.
            if (!ONLY || ONLY.test('real-host-drop-delete')) {
              await go('/__layout/drop-list')
              const opened = (await present('[data-testid="drop-row"]')) && (await clickText('button', /^Delete$/))
              await settle()
              await check(config, 'real-host-drop-delete', opened ? await need([['the confirmation', '[data-testid="drop-confirm"]']]) : ['the Delete button is missing'])
            }
            // Removal is CLI only: the "Remove this device" disclosure open with its command.
            if (!ONLY || ONLY.test('real-host-pair-remove-command')) {
              await go('/__layout/pair-devices')
              const opened = (await present('[data-testid="device-row"]')) && (await clickText('summary', /^Remove this device$/))
              await settle()
              await check(config, 'real-host-pair-remove-command', opened ? await need([['the remove command', '[data-testid="device-remove-command"]']]) : ['the Remove this device disclosure is missing'])
            }
            // The "Make a normal ticket" hand-off, opened from the row menu.
            if (!ONLY || ONLY.test('real-host-quick-normal-ticket')) {
              await go('/__layout/quick-normal')
              await check(config, 'real-host-quick-normal-ticket', (await present('button[aria-label="More for DEMO-0101"]')) ? [] : ['the row menu is missing'])
            }
            // The add flow: a title with a long URL, Add, the first of the two signatures open.
            if (!ONLY || ONLY.test('real-host-quick-add')) {
              await go('/__layout/quick-add')
              await evaluate(async (title) => {
                const input = document.querySelector('#quick-title')
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, title)
                input.dispatchEvent(new Event('input', { bubbles: true }))
                await new Promise((r) => setTimeout(r, 200))
              }, 'Fix ' + 'https://git.example.test/' + 'long-path/'.repeat(8))
              const added = await clickText('button', /^Sign and (create|start) task$/)
              await settle()
              await check(config, 'real-host-quick-add', added ? await need([['the first signature dialog', '[role="dialog"]']]) : ['the Add button is missing'])
              await escape()
            }
            // A failed push: Commit and push, confirmed, and the failure line with the commit that did land.
            if (!ONLY || ONLY.test('real-host-records-push-mismatch')) {
              await go('/__layout/records-push-mismatch')
              await present('[data-testid="records-export-warning"]')
              const asked = await clickText('button', /^Commit and push$/)
              await present('[data-testid="records-push-mismatch"]')
              await check(config, 'real-host-records-push-mismatch', asked ? await need([['the changed destination', '[role="dialog"] [data-testid="records-push-mismatch"]']]) : ['the Commit and push button is missing'])
              await escape()
            }
            if (!ONLY || ONLY.test('real-host-records-push')) {
              await go('/__layout/records-push-failed')
              await present('[data-testid="records-export-warning"]')
              const asked = await clickText('button', /^Commit and push$/)
              await settle()
              const confirmDialog = asked ? await need([['the push confirmation', '[role="dialog"] [data-testid="records-export-warning"]']]) : ['the Commit and push button is missing']
              await check(config, 'real-host-records-push-confirm', confirmDialog)
              const confirmed = await evaluate(async () => {
                const b = [...document.querySelectorAll('[role="dialog"] button')].find((x) => x.textContent.trim() === 'Commit and push')
                b?.click()
                for (let i = 0; i < 30 && !document.querySelector('[data-testid="records-failure"]'); i++) await new Promise((r) => setTimeout(r, 100))
                return !!document.querySelector('[data-testid="records-failure"]')
              })
              await check(config, 'real-host-records-push-failed', confirmed ? undefined : ['the failed-push line did not show'])
            }
          }
          shotsTaken = true
        }
      }
    }
  }
  close()
  console.log(`\n${checked} pages checked, ${failures.length} failing (overflow, cut-off controls or a step that did not open), ${warned} clipping warnings.`)
  if (failures.length) process.exitCode = 1
}

main().catch((e) => {
  console.error(e)
  process.exit(2)
})
