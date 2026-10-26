import { lazy, useState, type ComponentProps, type ComponentType } from 'react'

/** A lazily loaded component that can be loaded ahead of its first render (route loaders, hover intent). */
export type Preloadable<C> = C & { preload: () => Promise<void> }

/**
 * Like React.lazy, with `preload()`. Once the chunk is in, the component renders at once (no Suspense round, so no
 * fallback frame and no box that changes size). Before that it suspends like React.lazy. A failed preload is not
 * cached (the next preload fetches the chunk again); a render that failed shows its error boundary.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function lazyWithPreload<C extends ComponentType<any>>(load: () => Promise<{ default: C }>): Preloadable<C> {
  let Loaded: C | undefined
  let pending: Promise<void> | undefined
  const preload = () =>
    (pending ??= load().then(
      (m) => {
        Loaded = m.default
      },
      (e: unknown) => {
        pending = undefined
        throw e
      },
    ))
  const Lazy = lazy(async () => {
    await preload()
    return { default: Loaded! }
  })
  function LazyWithPreload(props: ComponentProps<C>) {
    // Chosen once per instance: switching from the lazy wrapper to the loaded component would remount it.
    const [Comp] = useState(() => (Loaded ?? Lazy) as ComponentType<ComponentProps<C>>)
    return <Comp {...props} />
  }
  return Object.assign(LazyWithPreload, { preload }) as unknown as Preloadable<C>
}
