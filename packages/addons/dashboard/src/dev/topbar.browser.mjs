// Real-browser regression check: run against `npm run dev` with Node 24+.
// node src/dev/topbar.browser.mjs http://localhost:5204
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { waitForDevTools } from '../../scripts/layout-startup.mjs'

const profile = mkdtempSync(join(tmpdir(), 'b8-topbar-'))
const chrome = spawn(process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run', 'about:blank'], { stdio: 'ignore' })
let socket
try {
  const target = await waitForDevTools(join(profile, 'DevToolsActivePort'), { alive: () => chrome.exitCode === null })
  socket = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject })
  let serial = 0
  const pending = new Map()
  socket.onmessage = ({ data }) => {
    const message = JSON.parse(data)
    if (pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id)
      pending.delete(message.id)
      message.error ? reject(message.error) : resolve(message.result)
    }
  }
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    pending.set(++serial, { resolve, reject })
    socket.send(JSON.stringify({ id: serial, method, params }))
  })
  const evaluate = async (expression) => {
    const answer = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (answer.exceptionDetails) throw new Error(answer.exceptionDetails.exception?.description)
    return answer.result.value
  }
  await send('Page.navigate', { url: process.argv[2] ?? 'http://localhost:5204' })
  for (let n = 0; n < 100 && !await evaluate('!!window.__orchRouter && !!document.querySelector("header")'); n++) await sleep(100)
  await evaluate(`(async () => {
    const { api } = await import('/src/api/client.ts')
    const { agentsApi } = await import('/src/api/agents.ts')
    const { setFeatures } = await import('/src/api/features.ts')
    const { nowMs } = await import('/src/lib/time.ts')
    const until = new Date(nowMs() + 3600000).toISOString()
    agentsApi.runner = async () => ({ runner: { enabled: true, connected: true, grant: { id: 'test-grant', scope: 'workable', until } } })
    api.listHostGrants = async (ws) => [{ id: 'test-grant', person: (await api.getMe(ws)).person, issued_at: new Date(nowMs() - 7 * 3600000).toISOString(), expires_at: until, revoked: null }]
    setFeatures({ features: ['agent_start', 'actions', 'do:grant.revoke'] })
    await window.__orchRouter.navigate({ to: '/agents' })
  })()`)
  for (let n = 0; n < 100 && !await evaluate('!!document.querySelector(".agent-access")'); n++) await sleep(100)
  for (const width of [1920, 1440, 1280, 1024]) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false })
    await sleep(200)
    const result = await evaluate(`(() => {
      const header = document.querySelector('header.topbar')
      const pill = header.querySelector('[aria-label="Agent access"]')
      return { height: header.getBoundingClientRect().height, fits: header.scrollWidth <= header.clientWidth, pill: !!pill, time: !!pill?.querySelector('time'), compact: getComputedStyle(pill.querySelector('.agent-access-detail')).display === 'none' }
    })()`)
    assert.equal(result.pill, true)
    assert.equal(result.time, true)
    assert.equal(result.height, 48, JSON.stringify({ width, ...result }))
    assert.equal(result.fits, true, JSON.stringify({ width, ...result }))
    if (width === 1024) assert.equal(result.compact, true)
    console.log(`Topbar ${width}px: one 48px row, controls fit, expiry visible`)
  }
} finally {
  socket?.close()
  chrome.kill('SIGKILL')
  rmSync(profile, { recursive: true, force: true })
}
