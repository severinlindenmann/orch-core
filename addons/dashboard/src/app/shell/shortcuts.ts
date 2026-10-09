import { useEffect } from 'react'
import { useRouter } from '@tanstack/react-router'
import { can } from '@/api/permissions'
import { SHORTCUT_DEFS } from '@/api/shortcuts'
import { useRole } from '../useRole'
import { useWorkspace } from '../workspace'
import { useShellState } from './ShellUi'

export interface Shortcut {
  id: string
  /** Space-separated keys: `c`, or the two-key sequence `g b`. */
  keys: string
  label: string
  /** Absent when another component owns the key. */
  run?: (go: (to: string) => void) => void
}

const isMac = typeof navigator !== 'undefined' && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent)

/** ⌘1–⌘9 (Ctrl+1–9 elsewhere) switch to the n-th workspace; bound in `useShortcuts`, shown by the palette. */
const WORKSPACE_SHORTCUTS: Shortcut[] = Array.from({ length: 9 }, (_, i) => ({
  id: `workspace.${i + 1}`,
  keys: `${isMac ? '⌘' : 'Ctrl+'}${i + 1}`,
  label: `Switch to workspace ${i + 1}`,
}))

/** Who runs a shortcut. A key without one is handled by the component that owns it (`[` by the sidebar, `?` by the help sheet). */
const RUN: Record<string, Shortcut['run']> = {
  'new-ticket': (go) => go('/tickets/new'),
  'go.today': (go) => go('/'),
  'go.board': (go) => go('/board'),
  'go.tickets': (go) => go('/tickets'),
  'go.agents': (go) => go('/agents'),
}

/** The one list of keyboard shortcuts: the shell binds them, the palette shows them, the guide's shortcuts page is generated from it (`src/api/shortcuts.ts`). */
export const SHORTCUTS: Shortcut[] = SHORTCUT_DEFS.flatMap((d): Shortcut[] => {
  const s: Shortcut = { ...d, run: RUN[d.id] }
  return d.id === 'go.agents' ? [s, ...WORKSPACE_SHORTCUTS] : [s]
})

export const keysFor = (id: string) => SHORTCUTS.find((s) => s.id === id)?.keys

const SEQUENCE_MS = 1000

/** Typing in a field, or any open dialog, menu or list, owns the keyboard. */
export function keyboardBusy(target: EventTarget | null): boolean {
  const t = target as HTMLElement | null
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.closest?.('[role="dialog"],[role="alertdialog"],[role="menu"],[role="listbox"]'))) return true
  return !!document.querySelector('[role="dialog"],[role="alertdialog"]')
}

/** Binds SHORTCUTS: single keys and `g x` sequences (second key within 1 s). Mount once, in the shell. */
export function useShortcuts() {
  const router = useRouter()
  const { workspaces, switchWorkspace } = useWorkspace()
  const { setPaletteOpen, setPaletteSeed } = useShellState()
  const role = useRole()
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
        if (hit.id === 'new-ticket' && role && !can(role, 'ticket.create')) {
          // A viewer gets the reason, in the palette, instead of a form they cannot submit.
          setPaletteSeed('New ticket')
          setPaletteOpen(true)
          return
        }
        hit.run!(go)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      disarm()
    }
  }, [router, workspaces, switchWorkspace, role, setPaletteOpen, setPaletteSeed])
}
