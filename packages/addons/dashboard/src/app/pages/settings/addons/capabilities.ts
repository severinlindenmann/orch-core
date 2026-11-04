/** What each capability lets an addon do, in plain words (tooltip text and the grant prompt). */
export const CAPABILITY_EXPLAINERS: Record<string, string> = {
  network: 'talks to the internet',
  serve_http: 'serves pages on your machine',
  pty: 'opens terminals; never for agents',
  spawn_agent: 'starts agent sessions',
  launch: 'changes how agents start',
  git_push: "pushes a ticket's own branch and merges into allowed targets",
}

export const explain = (cap: string) => CAPABILITY_EXPLAINERS[cap] ?? 'an unknown capability'

/** Capabilities in `next` that `prev` does not have: the diff a person signs when updating. */
export const addedCapabilities = (prev: string[], next: string[]) => next.filter((c) => !prev.includes(c))
export const removedCapabilities = (prev: string[], next: string[]) => prev.filter((c) => !next.includes(c))

/**
 * addon-api 1.2: one wake event in words, the same sentence core shows in `orch addon grant`, its signing prompt and the
 * hub hand-off (core's `addons/event_words.py`). The title is the addon's own text: callers pass it through `plain`.
 */
export const eventLine = (ev: { title: string; interval_s: number }, caps: string[], show: (s: string) => string = (s) => s) =>
  `${show(ev.title)}: runs in the background every ${Math.max(Math.floor(ev.interval_s / 60), 1)} min while a follow-up waits for it; the addon decides when the run is finished; it can reach the internet (orch does not limit where)${caps.includes('credential') ? '; it uses your stored key' : ''}`
