// What the dock shows depends on where you are: a ticket page (its sessions) or anywhere else (the workspace's).

import { useRouterState } from '@tanstack/react-router'
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
