// Quick tasks and records on a real host (M6, host contract 12.8, 12.9, 12.11): the shapes the dashboard server sends,
// the constant strings the contract wants verbatim in the bundle, and the one local setting (the quick limits).
import type { TicketSummary } from './types'

/** Client text, never host text (12.11 "Rules and strings"). Whenever `runner.enabled`, whatever `terminals` says. */
export const DASHBOARD_EXECUTION_LINE =
  'Dashboard execution: agents and terminals started here run as you. Anything that takes over this dashboard, or any agent it starts, can run commands and read your files and keys.'
/** The export warning is this sentence with the exact remote URL between the two parts (the URL is drawn through `Raw`). */
export const EXPORT_WARNING = { before: 'Push publishes every ticket in this workspace to ', after: ', restricted ones included.' } as const
/** The private-remote line, the same way. */
export const PRIVATE_REMOTE = { before: 'orch trusts your word that only you read ', after: '.' } as const

/** What the person must type in a terminal: the dashboard has no button for these (12.11, G12). */
export const TERMINAL_COMMANDS = {
  runnerEnable: 'orch runner enable',
  remotePrivate: 'orch records remote private',
  autoOn: 'orch records auto on',
  autoOnPush: 'orch records auto on --push',
  pushInitial: 'orch records push --initial',
} as const

/** Starting a quick task is ONE review and ONE confirmation (B11): said in the UI, in plain words. */
export const ONE_SIGNATURE = 'One signature starts the task: it creates it, approves its requirements and plan, and only if you tick the box lets agents work. You still give the final verdict.'
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
  max_commits?: number
  max_files?: number
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

/** What `runner` says on `/api/workspaces`, `/api/me` and the identity (host `whoami.runner`, cap `run/1`); null: the host does not say. */
export interface RunnerInfo {
  connected: boolean
  enabled: boolean
  terminals: boolean
}

// ---- the quick limits: a local setting of this browser, per workspace (12.11: "limits are a local setting")
export interface QuickLimits {
  max_commits: number | null
  max_files: number | null
}
const KEY = (ws: string) => `orch.quick.limits.${ws}`
export const LIMIT_RANGE = { max_commits: { min: 1, max: 100 }, max_files: { min: 1, max: 1000 } } as const

const inRange = (v: unknown, r: { min: number; max: number }): v is number => typeof v === 'number' && Number.isInteger(v) && v >= r.min && v <= r.max

/** Unset limits are left to the host's defaults. Storage that is missing or damaged reads as "not set". */
export function readQuickLimits(ws: string): QuickLimits {
  try {
    const v: unknown = JSON.parse(localStorage.getItem(KEY(ws)) ?? 'null')
    if (v && typeof v === 'object') {
      const o = v as Record<string, unknown>
      return { max_commits: inRange(o.max_commits, LIMIT_RANGE.max_commits) ? o.max_commits : null, max_files: inRange(o.max_files, LIMIT_RANGE.max_files) ? o.max_files : null }
    }
  } catch {
    /* storage unavailable or not JSON */
  }
  return { max_commits: null, max_files: null }
}

/** Returns false when the browser would not keep it (the choice then lasts for this page only). */
export function writeQuickLimits(ws: string, limits: QuickLimits): boolean {
  try {
    if (limits.max_commits === null && limits.max_files === null) localStorage.removeItem(KEY(ws))
    else localStorage.setItem(KEY(ws), JSON.stringify(limits))
    return true
  } catch {
    return false
  }
}

/** Status groups of the page. `waiting` is a host status of quick.list. */
export const ACTIVE_STATUSES = ['backlog', 'open', 'in-progress', 'waiting', 'testing']
export const DONE_STATUSES = ['done', 'closed']
