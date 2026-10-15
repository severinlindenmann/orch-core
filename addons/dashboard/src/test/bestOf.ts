/** Warm once, then keep the best of five: scheduling stalls must not relax the query budget. */
export async function bestOfFive(run: () => unknown | Promise<unknown>): Promise<number> {
  await run()
  let best = Infinity
  for (let i = 0; i < 5; i++) {
    const start = performance.now()
    await run()
    best = Math.min(best, performance.now() - start)
  }
  return best
}
