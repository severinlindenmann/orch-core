// The Theme choice (#421): System / Light / Dark, stored per browser. public/theme-boot.js applies the same rules before
// first paint (it is a classic script because the CSP allows no inline script); keep the two in step.
import { useSyncExternalStore } from 'react'

export type ThemeChoice = 'system' | 'light' | 'dark'
export type Resolved = 'light' | 'dark'
export const THEME_KEY = 'orch.theme'

export function storedChoice(): ThemeChoice {
  try {
    const v = localStorage.getItem(THEME_KEY)
    return v === 'light' || v === 'dark' ? v : 'system'
  } catch {
    return 'system'
  }
}

function prefersLight(): boolean {
  try {
    return window.matchMedia('(prefers-color-scheme: light)').matches
  } catch {
    return false
  }
}

export function resolve(choice: ThemeChoice): Resolved {
  return choice === 'system' ? (prefersLight() ? 'light' : 'dark') : choice
}

const listeners = new Set<() => void>()

/** Sets data-theme (an explicit choice only; System leaves it to the media query), the .dark class and color-scheme. */
export function applyChoice(choice: ThemeChoice = storedChoice()): Resolved {
  const root = document.documentElement
  const r = resolve(choice)
  if (choice === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', choice)
  root.classList.toggle('dark', r === 'dark')
  root.style.colorScheme = r
  return r
}

/** Stores the choice (when storage works) and applies it at once. */
export function setChoice(choice: ThemeChoice): void {
  try {
    if (choice === 'system') localStorage.removeItem(THEME_KEY)
    else localStorage.setItem(THEME_KEY, choice)
  } catch {
    /* private window or blocked storage: it still applies for this page */
  }
  applyChoice(choice)
  listeners.forEach((l) => l())
}

let watching = false
/** While the choice is System, a change of the browser's own setting re-applies it. */
export function watchSystem(): void {
  if (watching) return
  watching = true
  try {
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
      if (storedChoice() === 'system') {
        applyChoice('system')
        listeners.forEach((l) => l())
      }
    })
  } catch {
    /* no matchMedia: dark stays */
  }
}

const subscribe = (l: () => void) => {
  listeners.add(l)
  return () => void listeners.delete(l)
}
export const useThemeChoice = (): ThemeChoice => useSyncExternalStore(subscribe, storedChoice, () => 'system')
export const useResolvedTheme = (): Resolved => useSyncExternalStore(subscribe, () => resolve(storedChoice()), () => 'dark')
