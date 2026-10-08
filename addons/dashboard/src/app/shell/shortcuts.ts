import { useEffect } from 'react'
import { useRouter } from '@tanstack/react-router'

export interface Shortcut {
  id: string
  /** Space-separated keys: `c`, or the two-key sequence `g b`. */
  keys: string
  label: string
  /** Absent when another component owns the key (`[` is handled by the sidebar). */
  run?: (go: (to: string) => void) => void
}

/** The one list of keyboard shortcuts: the shell binds them, the palette shows them, the shortcuts guide can list them. */
export const SHORTCUTS: Shortcut[] = [
  { id: 'new-ticket', keys: 'c', label: 'New ticket', run: (go) => go('/tickets/new') },
  { id: 'go.today', keys: 'g t', label: 'Go to Today', run: (go) => go('/') },
  { id: 'go.board', keys: 'g b', label: 'Go to Board', run: (go) => go('/board') },
  { id: 'go.tickets', keys: 'g l', label: 'Go to Tickets', run: (go) => go('/tickets') },
  { id: 'go.agents', keys: 'g a', label: 'Go to Agents', run: (go) => go('/agents') },
  { id: 'sidebar', keys: '[', label: 'Collapse or expand the sidebar' },
]

export const keysFor = (id: string) => SHORTCUTS.find((s) => s.id === id)?.keys

const SEQUENCE_MS = 1000

/** Typing in a field, or any open dialog, menu or list, owns the keyboard. */
export function keyboardBusy(target: EventTarget | null): boolean {
  const t = target as HTMLElement | null
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.closest?.('[role="dialog"],[role="menu"],[role="listbox"]'))) return true
  return !!document.querySelector('[role="dialog"]')
}

/** Binds SHORTCUTS: single keys and `g x` sequences (second key within 1 s). Mount once, in the shell. */
export function useShortcuts() {
  const router = useRouter()
  useEffect(() => {
    const go = (to: string) => void router.navigate({ to } as never)
    let armed: number | null = null
    const disarm = () => {
      if (armed !== null) window.clearTimeout(armed)
      armed = null
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey || e.defaultPrevented || e.repeat) return
      if (keyboardBusy(e.target)) {
        disarm()
        return
      }
      if (armed !== null) {
        disarm()
        const hit = SHORTCUTS.find((s) => s.run && s.keys === `g ${e.key}`)
        if (hit) {
          e.preventDefault()
          hit.run!(go)
        }
        return
      }
      if (e.key === 'g') {
        armed = window.setTimeout(disarm, SEQUENCE_MS)
        return
      }
      const hit = SHORTCUTS.find((s) => s.run && s.keys === e.key)
      if (hit) {
        e.preventDefault()
        hit.run!(go)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      disarm()
    }
  }, [router])
}
