import { readFileSync } from 'node:fs'
import { setTimeout as sleep } from 'node:timers/promises'

export async function waitForTarget(url, { fetcher = fetch, pause = sleep } = {}) {
  for (let i = 0; i < 60; i++) {
    try {
      if ((await fetcher(url, { signal: AbortSignal.timeout(1000) })).ok) return
    } catch { /* server may still be starting */ }
    await pause(500)
  }
  throw new Error(`No dev server at ${url}: start the target server or pass --url`)
}

export async function waitForDevTools(portFile, { read = (path) => readFileSync(path, 'utf8'), fetcher = fetch, pause = sleep, alive = () => true } = {}) {
  for (let i = 0; i < 100; i++) {
    if (!alive()) throw new Error('Chrome exited before its DevTools endpoint was ready')
    try {
      const [port, browser] = read(portFile).split('\n')
      if (/^[0-9]+$/.test(port) && Number(port) > 0 && Number(port) <= 65535 && browser?.trim()) {
        const response = await fetcher(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(1000) })
        if (response.ok) {
          const targets = await response.json()
          const page = targets.find((target) => target.type === 'page' && typeof target.webSocketDebuggerUrl === 'string')
          if (page) return page
        }
      }
    } catch { /* the file can be absent/partial and the endpoint not listening yet */ }
    await pause(100)
  }
  throw new Error('Chrome DevTools endpoint did not become ready (missing port file or page target)')
}
