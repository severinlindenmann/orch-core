/** What each capability lets an addon do, in plain words (tooltip text and the grant prompt). */
export const CAPABILITY_EXPLAINERS: Record<string, string> = {
  network: 'talks to the internet',
  serve_http: 'serves pages on your machine',
  pty: 'opens terminals; never for agents',
  spawn_agent: 'starts agent sessions',
  launch: 'changes how agents start',
}

export const explain = (cap: string) => CAPABILITY_EXPLAINERS[cap] ?? 'an unknown capability'

/** Capabilities in `next` that `prev` does not have: the diff a person signs when updating. */
export const addedCapabilities = (prev: string[], next: string[]) => next.filter((c) => !prev.includes(c))
export const removedCapabilities = (prev: string[], next: string[]) => prev.filter((c) => !next.includes(c))
