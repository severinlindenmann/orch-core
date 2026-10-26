import { test } from 'node:test'
import assert from 'node:assert/strict'
import { waitForDevTools, waitForTarget } from './layout-startup.mjs'

test('partial port file and an unready endpoint retry without using port 80', async () => {
  const files = ['', '1234\n', '1234\n/devtools/browser/test\n']
  const urls = []
  let attempts = 0
  const page = { type: 'page', webSocketDebuggerUrl: 'ws://127.0.0.1:1234/devtools/page/test' }
  const result = await waitForDevTools('unused', {
    read: () => files.shift() ?? '1234\n/devtools/browser/test\n',
    pause: async () => {},
    fetcher: async (url) => {
      urls.push(url)
      if (++attempts === 1) throw new Error('not ready')
      return { ok: true, json: async () => [page] }
    },
  })
  assert.equal(result, page)
  assert.deepEqual(urls, ['http://127.0.0.1:1234/json/list', 'http://127.0.0.1:1234/json/list'])
})

test('invalid ports never fetch and report a clear timeout', async () => {
  for (const content of ['\n/path\n', '0\n/path\n', '65536\n/path\n', 'junk\n/path\n']) {
    await assert.rejects(waitForDevTools('unused', {
      read: () => content, pause: async () => {},
      fetcher: async () => assert.fail('must not fetch an invalid port'),
    }), /Chrome DevTools.*ready/)
  }
})

test('target readiness retries failed HTTP responses and connection errors', async () => {
  let calls = 0
  await waitForTarget('http://127.0.0.1:5201', {
    pause: async () => {},
    fetcher: async () => {
      if (++calls === 1) throw new Error('not listening')
      return { ok: calls === 3 }
    },
  })
  assert.equal(calls, 3)
  await assert.rejects(waitForTarget('http://127.0.0.1:5201', {
    pause: async () => {}, fetcher: async () => ({ ok: false }),
  }), /No dev server at http:\/\/127.0.0.1:5201/)
})

// Exercise the actual launch lifecycle with fake Chrome/CDP; no listener is started.
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { EventEmitter } from 'node:events'
const guard = readFileSync(new URL('./layout-guard.mjs', import.meta.url), 'utf8')
const launchSource = guard.slice(guard.indexOf('async function launch()'), guard.indexOf('// ------------------------------------------------------------------ page helpers'))
for (const failure of ['endpoint', 'websocket', 'SIGTERM', 'SIGINT', 'close']) {
  test(`Chrome is killed and handlers removed on ${failure}`, async () => {
    const proc = new EventEmitter()
    proc.exitCode = null
    proc.signalCode = null
    let killed = 0
    proc.kill = () => { killed++ }
    const signals = new EventEmitter()
    signals.exit = () => {}
    const launch = runInNewContext(`${launchSource}; launch`, {
      findChrome: () => '/fake/chrome', mkdtempSync: () => '/fake/profile', tmpdir: () => '/tmp',
      join: (...parts) => parts.join('/'), spawn: () => proc, rmSync: () => {}, process: signals,
      setTimeout, clearTimeout, Map, Error,
      waitForDevTools: async () => {
        if (failure === 'endpoint') throw new Error('endpoint failure')
        return { webSocketDebuggerUrl: 'ws://fake' }
      },
      WebSocket: class {
        constructor() { queueMicrotask(() => failure === 'websocket' ? this.onerror(new Error('websocket failure')) : this.onopen()) }
        close() {}
      },
    })
    if (failure === 'endpoint' || failure === 'websocket') await assert.rejects(launch(), /failure/)
    else {
      const browser = await launch()
      if (failure === 'close') browser.close()
      else signals.emit(failure)
      browser.close() // cleanup is idempotent
    }
    assert.equal(killed, 1)
    assert.equal(signals.listenerCount('SIGTERM'), 0)
    assert.equal(signals.listenerCount('SIGINT'), 0)
    assert.equal(signals.listenerCount('exit'), 0)
  })
}
