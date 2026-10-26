// What the dock shows depends on where you are: a ticket page (its sessions) or anywhere else (the workspace's).

import { useRouterState } from '@tanstack/react-router'
import { sessionName } from '@/api/harnesses'
import { SHORTCUT_DEFS } from '@/api/shortcuts'
import type { TerminalSessionView } from '@/api/terminals'

export const DOCK_ADDON = 'terminals'
/** The dock's shortcut, from the shortcut registry (bound by DockArea, which owns the key). */
export const DOCK_KEYS = SHORTCUT_DEFS.find((d) => d.id === 'terminal.dock')?.keys ?? 'Ctrl+`'

/** The ticket key when the current page is a ticket page. */
export function useDockTicket(): string | undefined {
  return useRouterState({ select: (s) => /^\/ticket\/([^/?#]+)/.exec(s.location.pathname)?.[1] })
}

/**
 * Running and ended sessions for this context: the ticket's on a ticket page, else every one. `sessions` is the
 * terminals addon's view, already cut to what this viewer may see (own shells, agent mirrors of visible tickets).
 */
export function sessionsIn(sessions: TerminalSessionView[], ticket: string | undefined) {
  const here = ticket ? sessions.filter((s) => s.ticket === ticket) : sessions
  return { running: here.filter((s) => s.status === 'running'), ended: here.filter((s) => s.status === 'stopped') }
}

/** What the dock had open in one scope (workspace + ticket): the active window and the transcripts opened as tabs. */
export interface DockSelection {
  selected: string | null
  opened: string[]
}
/** Kept by DockArea (above the lazy dock), so collapse and navigation do not lose it. Keyed by `scopeKey`. */
export type DockMemory = Map<string, DockSelection>
export const scopeKey = (ws: string | undefined, ticket: string | undefined) => `${ws ?? ''}:${ticket ?? '*'}`

/** A session's name in the dock: purpose-based ("Claude · Agent"), with the ticket key outside a ticket's scope. */
export function dockName(s: TerminalSessionView, ticket: string | undefined): string {
  const name = sessionName(s)
  return !ticket && s.ticket ? `${s.ticket} ${name}` : name
}
