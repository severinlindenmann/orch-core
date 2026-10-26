import { lazy, useState, type ComponentType } from 'react'

const failedLoads = new Set<() => void>()

/**
 * React.lazy caches a rejected import for good. Call this when the page boundary is reloaded: every page whose
 * chunk failed to load gets a fresh lazy component, so the next render fetches the chunk again.
 */
export function retryFailedPageLoads() {
  for (const renew of [...failedLoads]) renew()
}

/**
 * Lazy page component from a module with a named export, so each page is its own chunk. `preload()` loads the chunk
 * ahead of the first render: the route loaders call it, so the router keeps the old page on screen until the new one
 * can render (no blank frame), and hovering a link loads it early. Once loaded the page renders without a Suspense
 * round. A failed load is retried by the next preload, or by the page boundary's Reload (retryFailedPageLoads).
 */
export function lazyPage<M extends Record<K, ComponentType<any>>, K extends keyof M>(load: () => Promise<M>, name: K): M[K] & { preload: () => Promise<void> } { // eslint-disable-line @typescript-eslint/no-explicit-any
  let Loaded: ComponentType<any> | undefined // eslint-disable-line @typescript-eslint/no-explicit-any
  let pending: Promise<void> | undefined
  const preload = () =>
    (pending ??= load().then(
      (m) => {
        Loaded = m[name]
      },
      (e: unknown) => {
        pending = undefined
        failedLoads.add(renew)
        throw e
      },
    ))
  const make = (): ComponentType<any> => // eslint-disable-line @typescript-eslint/no-explicit-any
    lazy(async () => {
      await preload()
      return { default: Loaded! }
    })
  let Current = make()
  const renew = () => {
    failedLoads.delete(renew)
    Current = make()
  }
  function LazyPage(props: object) {
    // Chosen once per instance: switching from the lazy wrapper to the loaded page would remount it. After a failed
    // load the boundary's Reload remounts the page, which then picks the renewed lazy component.
    const [Comp] = useState(() => Loaded ?? Current)
    return <Comp {...props} />
  }
  return Object.assign(LazyPage, { preload }) as unknown as M[K] & { preload: () => Promise<void> }
}
