import { lazy, type ComponentType } from 'react'

const failedLoads = new Set<() => void>()

/**
 * React.lazy caches a rejected import for good. Call this when the page boundary is reloaded: every page whose
 * chunk failed to load gets a fresh lazy component, so the next render fetches the chunk again.
 */
export function retryFailedPageLoads() {
  for (const renew of [...failedLoads]) renew()
}

/** Lazy page component from a module with a named export, so each page is its own chunk. */
export function lazyPage<M extends Record<K, ComponentType<any>>, K extends keyof M>(load: () => Promise<M>, name: K): M[K] { // eslint-disable-line @typescript-eslint/no-explicit-any
  const make = (): ComponentType<any> => // eslint-disable-line @typescript-eslint/no-explicit-any
    lazy(async () => {
      try {
        return { default: (await load())[name] }
      } catch (e) {
        failedLoads.add(renew)
        throw e
      }
    })
  let Current = make()
  const renew = () => {
    failedLoads.delete(renew)
    Current = make()
  }
  return function LazyPage(props: object) {
    return <Current {...props} />
  } as M[K]
}
