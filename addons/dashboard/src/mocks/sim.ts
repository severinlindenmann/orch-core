// Scripted event playback for the mock: each script is a chain of timed steps run against the store.
import type { MockStore } from './store'

export interface SimStep {
  /** Delay after the previous step (or after `play` for the first one). */
  afterMs: number
  run: (store: MockStore) => void
}

type Clock = { setTimeout: typeof setTimeout; clearTimeout: typeof clearTimeout }

// Resolve the globals at call time so fake timers installed later are honoured.
const defaultClock: Clock = {
  setTimeout: ((...a: Parameters<typeof setTimeout>) => globalThis.setTimeout(...a)) as typeof setTimeout,
  clearTimeout: ((...a: Parameters<typeof clearTimeout>) => globalThis.clearTimeout(...a)) as typeof clearTimeout,
}

export class Simulator {
  private timers = new Map<string, ReturnType<typeof setTimeout>>()

  constructor(
    private store: MockStore,
    private clock: Clock = defaultClock,
  ) {}

  /** Start a script; replaces a running script with the same id. */
  play(id: string, steps: SimStep[]): void {
    this.stop(id)
    const next = (i: number) => {
      if (i >= steps.length) {
        this.timers.delete(id)
        return
      }
      this.timers.set(
        id,
        this.clock.setTimeout(() => {
          steps[i].run(this.store)
          next(i + 1)
        }, steps[i].afterMs),
      )
    }
    next(0)
  }

  stop(id: string): void {
    const t = this.timers.get(id)
    if (t !== undefined) this.clock.clearTimeout(t)
    this.timers.delete(id)
  }

  stopAll(): void {
    for (const id of [...this.timers.keys()]) this.stop(id)
  }

  running(): string[] {
    return [...this.timers.keys()]
  }
}
