// Seeded PRNG for the busy-day generator (mulberry32). No Math.random, no Date.now: the same seed gives the same data.
export interface Rng {
  /** [0, 1) */
  next(): number
  /** Integer in [lo, hi]. */
  int(lo: number, hi: number): number
  pick<T>(items: readonly T[]): T
  chance(p: number): boolean
  /** A new array in random order. */
  shuffle<T>(items: readonly T[]): T[]
  /** `n` distinct items. */
  sample<T>(items: readonly T[], n: number): T[]
  /** An independent stream derived from this one and a label, so adding data in one area never shifts another. */
  fork(label: string): Rng
}

function hashLabel(label: string): number {
  let h = 2166136261
  for (let i = 0; i < label.length; i++) h = Math.imul(h ^ label.charCodeAt(i), 16777619)
  return h >>> 0
}

export function makeRng(seed: number): Rng {
  let a = seed >>> 0
  const next = () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
  const rng: Rng = {
    next,
    int: (lo, hi) => lo + Math.floor(next() * (hi - lo + 1)),
    pick: (items) => items[Math.floor(next() * items.length)],
    chance: (p) => next() < p,
    shuffle(items) {
      const out = [...items]
      for (let i = out.length - 1; i > 0; i--) {
        const j = Math.floor(next() * (i + 1))
        ;[out[i], out[j]] = [out[j], out[i]]
      }
      return out
    },
    sample: (items, n) => rng.shuffle(items).slice(0, n),
    fork: (label) => makeRng((seed ^ hashLabel(label)) >>> 0),
  }
  return rng
}
