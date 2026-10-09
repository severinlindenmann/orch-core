// Shapes shared by the terminals mock and the terminal view (part of the API contract). Pure types.

import type { HarnessId } from './harnesses'

/** Everything the fake shell may read, supplied live by the host at command time. */
export interface ShellCtx {
  user: string
  cwd: string // "~/energy"
  branch: string
  /** 'agent' shells are mirrors: they never take input, and `orch approve` is refused with human_only. */
  owner: 'person' | 'agent'
  now: string // ISO
  cursor: number
  /** The grant the session works under (the viewer's own for a person's shell); null when there is none. */
  grant: { id: string; scope: string; until: string } | null
  claim: { agent: string; session: string; for: string; expires: string } | null
  ticket: {
    key: string
    title: string
    status: string
    current_state: string
    next_task: { id: string; text: string } | null
    /** Whose turn it is, and why (the dashboard's own rule). */
    move: { who: string; why: string }
    gates: { name: string; state: string }[]
    questions: { open: number; total: number }
    tasks: { done: number; total: number; doing: string | null }
  } | null
}

/** One session as the host shows it to the current viewer. */
export interface TerminalSessionView {
  id: string
  label: string
  kind: 'person' | 'agent'
  /** person id, or "agent:<id>". */
  owner: string
  ticket: string | null
  started: string
  status: 'running' | 'stopped'
  /** May this viewer type? Only the owner of a running person's shell, and only as a member or above. */
  interactive: boolean
  ctx: ShellCtx
  /** Commands replayed on open (agent mirrors and stopped sessions). Person shells start empty. */
  transcript: string[]
  /** What runs in the session: a shell, Claude Code, Codex (src/api/harnesses.ts). */
  harness: HarnessId
  /** The command the session runs (harnessCommand). */
  command: string
  /** Started with the ticket's context loaded; false is a fresh window (an empty context window). */
  context: boolean
  /** An ended session's summary (what a resume is seeded with); null when none was recorded. */
  summary: string | null
  /** Set when this session resumed an earlier one: its title and the summary it was seeded with. */
  resumedFrom: { id: string; label: string; summary: string | null } | null
}
