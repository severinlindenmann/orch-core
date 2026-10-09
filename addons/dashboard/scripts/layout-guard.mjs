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
// --shots <dir> (a PNG per page of the first configuration), --prefix <name> (screenshot file prefix),
// --chrome <path> (or CHROME_PATH). Needs the dev build: it moves between routes through `window.__orchRouter` (main.tsx). jsdom cannot lay out, so this runs against the real dev server.

import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'

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
  const profile = mkdtempSync(join(tmpdir(), 'layout-guard-'))
  const proc = spawn(findChrome(), [
    '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check',
    '--hide-scrollbars=false', '--force-device-scale-factor=1', 'about:blank',
  ], { stdio: 'ignore' })
  const portFile = join(profile, 'DevToolsActivePort')
  for (let i = 0; i < 100 && !existsSync(portFile); i++) await sleep(100)
  const [port] = readFileSync(portFile, 'utf8').split('\n')
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
  const page = targets.find((t) => t.type === 'page')
  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((ok, fail) => {
    ws.onopen = ok
    ws.onerror = fail
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
  const close = () => {
    ws.close()
    proc.kill()
    try {
      rmSync(profile, { recursive: true, force: true })
    } catch {
      /* a locked profile is left for the OS to clean */
    }
  }
  return { send, close }
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
      for (let i = 0; i < 80 && still(); i++) await new Promise((r) => setTimeout(r, 100))
      let last = ''
      for (let i = 0; i < 20; i++) {
        await new Promise((r) => setTimeout(r, 150))
        const now = `${document.body.scrollHeight}:${document.querySelectorAll('*').length}`
        if (now === last) break
        last = now
      }
    })
  return { evaluate, settle }
}

/** In the page: everything that scrolls sideways where it should not. */
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
  const main = document.querySelector('main#main')
  return { problems: out, pageWidth: main ? Math.round(main.getBoundingClientRect().width) : 0 }
}

// ------------------------------------------------------------------ the run

/** The dev server must answer first (it may still be starting). */
async function waitForServer() {
  for (let i = 0; i < 60; i++) {
    try {
      if ((await fetch(BASE)).ok) return
    } catch {
      /* not up yet */
    }
    await sleep(500)
  }
  throw new Error(`No dev server at ${BASE}: start it with \`npx vite --port 5201 --strictPort --host 127.0.0.1\` or pass --url`)
}

async function main() {
  await waitForServer()
  const { send, close } = await launch()
  const { evaluate, settle } = makePage(send)
  await send('Page.enable')
  await send('Runtime.enable')
  const failures = []
  let checked = 0
  let shotsTaken = false

  const shoot = async (name) => {
    if (!SHOTS || shotsTaken) return
    mkdirSync(SHOTS, { recursive: true })
    const { data } = await send('Page.captureScreenshot', { format: 'png' })
    writeFileSync(join(SHOTS, `${PREFIX}-${name}.png`), Buffer.from(data, 'base64'))
  }

  const go = async (path) => {
    await evaluate((p) => window.__orchRouter.history.push(p), path)
    await settle()
  }

  const check = async (config, name) => {
    checked++
    const { problems, pageWidth } = await evaluate(findOverflow, ALLOWED)
    const status = problems.length ? 'FAIL' : 'ok'
    console.log(`  ${status.padEnd(4)} ${name} (page ${pageWidth}px)`)
    for (const p of problems) console.log(`         ${p}`)
    if (problems.length) failures.push({ config, name, problems })
    await shoot(name)
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
                localStorage.setItem('orch.sidebar', sidebar === 'rail' ? 'narrow' : 'wide')
                localStorage.setItem('orch.sidebar.docked', sidebar === 'rail' ? 'narrow' : 'wide')
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
            const hrefs = [...new Set([...document.querySelectorAll('a[href^="/addon/"]')].map((a) => a.getAttribute('href')))]
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
            ['new-ticket-page', '/tickets/new'],
            ...addonPages.map((h) => [`addon-${h.split('/').slice(2).join('-')}`, h]),
            ...['general', 'members', 'gates', 'relay', 'addons', 'skills', 'connections'].map((t) => [`settings-${t}`, `/settings/${t}`]),
            ...addonNames.map((n) => [`settings-addon-${n}`, `/settings/addon/${n}`]),
          ]
          for (const [name, path] of routes) {
            if (ONLY && !ONLY.test(name)) continue
            await go(path)
            await check(config, name)
          }

          // The ticket page, tab by tab.
          if (!ONLY || ONLY.test('ticket-')) {
            await go('/ticket/DEMO-0043')
            const tabs = await evaluate(() => [...document.querySelectorAll('main [role="tablist"]:first-of-type [role="tab"]')].map((t) => t.textContent.trim()))
            for (let i = 0; i < tabs.length; i++) {
              const name = `ticket-${tabs[i].toLowerCase().replace(/[^a-z]+.*$/, '')}`
              if (ONLY && !ONLY.test(name)) continue
              await evaluate(async (n) => {
                const tab = document.querySelectorAll('main [role="tablist"]:first-of-type [role="tab"]')[n]
                tab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 }))
                tab.focus()
                tab.click()
              }, i)
              await settle()
              await check(config, name)
            }
          }

          // The New ticket overlay over a page.
          if (!ONLY || ONLY.test('new-ticket-overlay')) {
            await go('/tickets')
            await evaluate(() => {
              document.activeElement?.blur()
              window.dispatchEvent(new KeyboardEvent('keydown', { key: 'c', bubbles: true }))
            })
            await settle()
            await check(config, 'new-ticket-overlay')
            await evaluate(() => document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
          }
          shotsTaken = true
        }
      }
    }
  }
  close()
  console.log(`\n${checked} pages checked, ${failures.length} with horizontal overflow.`)
  if (failures.length) process.exit(1)
}

main().catch((e) => {
  console.error(e)
  process.exit(2)
})
