import { useEffect } from 'react'
import { useRouter } from '@tanstack/react-router'
import { useWorkspace } from '../workspace'

export interface Shortcut {
  id: string
  /** Space-separated keys: `c`, or the two-key sequence `g b`. */
  keys: string
  label: string
  /** Absent when another component owns the key (`[` is handled by the sidebar). */
  run?: (go: (to: string) => void) => void
}

const isMac = typeof navigator !== 'undefined' && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent)

/** ⌘1–⌘9 (Ctrl+1–9 elsewhere) switch to the n-th workspace; bound in `useShortcuts`, shown by the palette. */
const WORKSPACE_SHORTCUTS: Shortcut[] = Array.from({ length: 9 }, (_, i) => ({
  id: `workspace.${i + 1}`,
  keys: `${isMac ? '⌘' : 'Ctrl+'}${i + 1}`,
  label: `Switch to workspace ${i + 1}`,
}))

/** The one list of keyboard shortcuts: the shell binds them, the palette shows them, the shortcuts guide can list them. */
export const SHORTCUTS: Shortcut[] = [
  { id: 'new-ticket', keys: 'c', label: 'New ticket', run: (go) => go('/tickets/new') },
  { id: 'go.today', keys: 'g t', label: 'Go to Today', run: (go) => go('/') },
  { id: 'go.board', keys: 'g b', label: 'Go to Board', run: (go) => go('/board') },
  { id: 'go.tickets', keys: 'g l', label: 'Go to Tickets', run: (go) => go('/tickets') },
  { id: 'go.agents', keys: 'g a', label: 'Go to Agents', run: (go) => go('/agents') },
  ...WORKSPACE_SHORTCUTS,
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
  const { workspaces, switchWorkspace } = useWorkspace()
  useEffect(() => {
    const go = (to: string) => void router.navigate({ to } as never)
    let armed: number | null = null
    const disarm = () => {
      if (armed !== null) window.clearTimeout(armed)
      armed = null
    }
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && /^[1-9]$/.test(e.key)) {
        const target = workspaces[Number(e.key) - 1]
        if (target && !e.defaultPrevented && !keyboardBusy(e.target)) {
          e.preventDefault()
          switchWorkspace(target.id)
        }
        return
      }
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
  }, [router, workspaces, switchWorkspace])
}
