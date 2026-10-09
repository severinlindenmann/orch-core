import { useCallback, useLayoutEffect, useState } from 'react'

/**
 * An element's width in px, kept current: attach the returned ref. 0 until measured (and always where there is no
 * layout, as in jsdom), so layout rules can treat 0 as "show everything".
 */
export function useElementWidth<T extends HTMLElement>(): [(el: T | null) => void, number] {
  const [el, setEl] = useState<T | null>(null)
  const [w, setW] = useState(0)
  useLayoutEffect(() => {
    if (!el) return
    const measure = () => setW(Math.round(el.getBoundingClientRect().width))
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [el])
  return [useCallback((node: T | null) => setEl(node), []), w]
}
