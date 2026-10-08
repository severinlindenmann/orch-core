import { lazy, type ComponentType } from 'react'

/** Lazy page component from a module with a named export, so each page is its own chunk. */
export function lazyPage<M extends Record<K, ComponentType<any>>, K extends keyof M>(load: () => Promise<M>, name: K) { // eslint-disable-line @typescript-eslint/no-explicit-any
  return lazy(async () => ({ default: (await load())[name] }))
}
