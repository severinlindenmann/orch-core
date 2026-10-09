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
    process.exit(ok ? 0 : 1)
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
          if (!ONLY || ONLY.source.includes('ticket-')) {
            await go('/ticket/DEMO-0043')
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
          shotsTaken = true
        }
      }
    }
  }
  close()
  console.log(`\n${checked} pages checked, ${failures.length} failing (overflow, cut-off controls or a step that did not open), ${warned} clipping warnings.`)
  if (failures.length) process.exit(1)
}

main().catch((e) => {
  console.error(e)
  process.exit(2)
})
