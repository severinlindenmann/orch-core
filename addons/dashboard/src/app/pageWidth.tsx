// How wide the page may lay itself out. Pages chose their layout from the window width (e.g. the ticket rail from
// 1280 px); with the terminal dock on the right the page has less room, so the shell provides the window width minus
// what the dock takes, and layout hooks read it. Outside the shell (tests of a single component) it is the window width.

import { createContext, useContext, useEffect, useState } from 'react'

/** The width a page should lay out for, as a window width; null outside the shell. */
export const CollapsedDockWidthContext = createContext(0)

export const PageWidthContext = createContext<number | null>(null)

function useWindowWidth(): number {
  const [w, setW] = useState(() => (typeof window === 'undefined' ? 1440 : window.innerWidth))
  useEffect(() => {
    const on = () => setW(window.innerWidth)
    window.addEventListener('resize', on)
    return () => window.removeEventListener('resize', on)
  }, [])
  return w
}

/** The layout width: the shell's (window minus a right-hand dock), else the window's. */
export function usePageWidth(): number {
  const fromShell = useContext(PageWidthContext)
  const win = useWindowWidth()
  return fromShell ?? win
}
