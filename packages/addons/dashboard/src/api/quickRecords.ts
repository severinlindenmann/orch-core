// Quick tasks and records on a real host (M6, host contract 12.8, 12.9, 12.11): the shapes the dashboard server sends,
// and the constant strings the contract wants verbatim in the bundle.
import type { TicketSummary } from './types'

/** Client text, never host text (12.11 "Rules and strings"). Whenever `runner.enabled`, whatever `terminals` says. */
export const DASHBOARD_EXECUTION_LINE =
  'Dashboard execution: agents and terminals started here run as you. Anything that takes over this dashboard, or any agent it starts, can run commands and read your files and keys.'
/** The export warning is this sentence with the exact remote URL between the two parts (the URL is drawn through `Raw`). */
export const EXPORT_WARNING = { before: 'Push publishes every ticket in this workspace to ', after: ', restricted ones included.' } as const
/** The private-remote line, the same way. */
export const PRIVATE_REMOTE = { before: 'orch trusts your word that only you read ', after: '.' } as const

/** What the person must type in a terminal (12.11, G12). #1198 C15: the records steps are buttons now, with a review. */
export const TERMINAL_COMMANDS = {
  autoOn: 'orch records auto on',
  autoOnPush: 'orch records auto on --push',
} as const

export const QUICK_LABEL = 'quick'

export interface QuickCounts {
  /** null: unknown (no linked repo, or the host could not read it). Never zero. */
  commits: number | null
  files: number | null
  /** null: unknown. */
  over: boolean | null
}
export type QuickRow = TicketSummary & { quick: QuickCounts | null }
export interface QuickPage {
  available: boolean
  rows: QuickRow[] | null
  next_page: string | null
  total: number | null
}
export interface QuickParams {
  status?: string[]
  limit?: number
  page?: string
}

export interface RecordsStatus {
  available: boolean
  configured?: boolean | null
  repo?: { state: string | null; problem: string | null; problem_hidden: boolean } | null
  pending?: { events: number; tickets: { key: string; events: number }[]; more: boolean; workspace_events: number } | null
  last?: { commit: string; at: string; by: string; ws_seq: number; pushed_at: string | null } | null
  last_noted?: { by: string; at: string } | null
  auto?: { on: boolean; every_min: number; push: boolean } | null
  remote?: { current_url?: string | null; current_url_hidden?: boolean; current_private?: boolean | null; matches_last?: boolean | null; url: string | null; url_hidden: boolean; private: boolean | null; export: string | null } | null
}
export interface RecordsRun {
  ok: true
  commit?: string
  pushed?: boolean
  url?: string | null
  url_hidden?: boolean
}

/** Whether Claude Code may open this workspace's folders without asking (the host's own answer, in plain words). */
export interface ClaudeTrust {
  available: boolean
  on: boolean
  /** The state of Claude Code's own settings file: "missing" = Claude Code was never started here. */
  config?: 'ready' | 'missing' | 'odd'
  offered: boolean
  why_not: string
  parent_config: string
  /** A Claude Code start asks for the consent in its sign card: not given yet, and the host can take it (no "trust anyway"). */
  agent_needs_consent?: boolean
  /** Why the consent cannot be given for a start ("" when it can, or is given already). */
  agent_why_not?: string
}

/** What `runner` says on `/api/workspaces`, `/api/me` and the identity (host `whoami.runner`, cap `run/1`); null: the host does not say. */
export interface RunnerInfo {
  connected: boolean
  enabled: boolean
  terminals: boolean
}

/** Status groups of the page. `waiting` is a host status of quick.list. */
export const ACTIVE_STATUSES = ['backlog', 'open', 'in-progress', 'waiting', 'testing']
export const DONE_STATUSES = ['done', 'closed']
