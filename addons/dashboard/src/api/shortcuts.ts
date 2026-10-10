// The one list of keyboard shortcuts, as data. The app binds them (src/app/shell/shortcuts.ts) and the guide's
// "Keyboard shortcuts" page is generated from this list, so the page cannot go stale. Pure data: the mock imports it.

export interface ShortcutDef {
  id: string
  /** Space-separated keys: `c`, or the two-key sequence `g b`. */
  keys: string
  label: string
}

export const SHORTCUT_DEFS: ShortcutDef[] = [
  { id: 'new-ticket', keys: 'c', label: 'New ticket' },
  { id: 'go.today', keys: 'g t', label: 'Go to Today' },
  { id: 'go.board', keys: 'g b', label: 'Go to Board' },
  { id: 'go.tickets', keys: 'g l', label: 'Go to Tickets' },
  { id: 'go.agents', keys: 'g a', label: 'Go to Agents' },
  // Owned by the Artifacts page (no shell binding): only while the focus is in its results.
  { id: 'artifacts.next', keys: 'j', label: 'Artifacts: move to the next artifact (the preview follows when it is beside the list)' },
  { id: 'artifacts.previous', keys: 'k', label: 'Artifacts: move to the previous artifact (the preview follows when it is beside the list)' },
  { id: 'help', keys: '?', label: 'Open the help for this page' },
  { id: 'sidebar', keys: '[', label: 'Collapse or expand the sidebar' },
  { id: 'terminal.dock', keys: 'Ctrl+`', label: 'Open or collapse the terminal dock (when the Terminals addon is on)' },
]

/** Shown in the guide as one row; the app binds ⌘1–⌘9 (Ctrl+1–9 elsewhere) one by one. */
export const WORKSPACE_SWITCH = { keys: 'Cmd+1 to Cmd+9', label: 'Switch to workspace 1 to 9 (Ctrl+1 to Ctrl+9 on Windows and Linux)' }
