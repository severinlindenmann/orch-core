// Simulated network latency for the mock host (G4). One server answers a burst of requests together: requests that
// start within BURST_MS of the first share its one delay and resolve in the same moment, as a page's parallel queries
// do against a real host. The default is a small fixed delay (BASE_MS), long enough that the dashboard's loading
// states still run (a page that needs two round trips shows its skeleton) without making the mock look slower than
// the local host. Dev switches, kept for the browser tab (sessionStorage):
//   ?jitter=1      the old behaviour: every request on its own, 120-300 ms (to test that nothing jumps); ?jitter=0 ends it
//   ?latency=<ms>  another fixed delay per burst (0 for none); ?latency= without a value goes back to the default

export const BASE_MS = 120
export const BURST_MS = 16
export const JITTER = { min: 120, spread: 180 } as const

export interface LatencySettings {
  /** Delay per burst, ms. */
  base: number
  /** Each request waits on its own, JITTER.min + random * JITTER.spread ms. */
  jitter: boolean
}

const JITTER_KEY = 'orch.mock.jitter'
const LATENCY_KEY = 'orch.mock.latency'

/** Reads the dev switches from the address (and remembers them for the tab). */
export function readLatencySettings(search: string = typeof location === 'undefined' ? '' : location.search, store: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | undefined = safeSession()): LatencySettings {
  const q = new URLSearchParams(search)
  try {
    if (q.has('jitter')) {
      if (q.get('jitter') === '1') store?.setItem(JITTER_KEY, '1')
      else store?.removeItem(JITTER_KEY)
    }
    if (q.has('latency')) {
      const v = q.get('latency')
      if (v && Number.isFinite(Number(v)) && Number(v) >= 0) store?.setItem(LATENCY_KEY, String(Math.min(5000, Number(v))))
      else store?.removeItem(LATENCY_KEY)
    }
  } catch {
    /* storage unavailable: the switch lasts for this load only */
  }
  const stored = store?.getItem(LATENCY_KEY)
  const fromQuery = q.get('latency')
  const base = Number(fromQuery && Number.isFinite(Number(fromQuery)) ? fromQuery : (stored ?? BASE_MS))
  return { base: Number.isFinite(base) && base >= 0 ? base : BASE_MS, jitter: q.has('jitter') ? q.get('jitter') === '1' : store?.getItem(JITTER_KEY) === '1' }
}

function safeSession(): Storage | undefined {
  try {
    return typeof sessionStorage === 'undefined' ? undefined : sessionStorage
  } catch {
    return undefined
  }
}

/**
 * A delay function for the mock handler: call it once per request and await the promise. Requests within BURST_MS of
 * a burst's first one share its delay (see the top of this file); with `jitter` each gets its own random delay.
 */
export function createLatency(settings: LatencySettings, clock: { now: () => number; random: () => number } = { now: () => performance.now(), random: Math.random }) {
  let burst: { at: number; done: Promise<void> } | null = null
  const sleep = (ms: number) => new Promise<void>((res) => setTimeout(res, ms))
  return (): Promise<void> => {
    if (settings.jitter) return sleep(JITTER.min + clock.random() * JITTER.spread)
    const now = clock.now()
    if (burst && now - burst.at <= BURST_MS) return burst.done
    burst = { at: now, done: sleep(settings.base) }
    return burst.done
  }
}
