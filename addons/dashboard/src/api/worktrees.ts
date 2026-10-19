// Worktrees of a real host (H11d, host contract section 12.7): one git worktree per ticket and linked repo. Real host
// only (the mockup keeps its own Worktrees addon page). The routes are the dashboard server's typed ones; nothing here
// names a host op. Creating and removing are dashboard-direct writes (no signature): the host decides who may. Every
// string from the host (branch, folder, title) is untrusted: the page shows it through visible.tsx.

import { queryOptions } from '@tanstack/react-query'
import { call, newIdem } from './runs'
import { ApiError } from './types'

export { newIdem }

/** What the host's reader saw (section 12.7). `unknown` is a value this dashboard does not know. */
export type WorktreeState = 'ok' | 'unpinned' | 'stale_branch' | 'missing' | 'moved' | 'unknown'

export interface Worktree {
  ticket: string | null
  title: string | null
  repo: string | null
  branch: string | null
  path: string | null
  state: WorktreeState
  /** Changed files; null is unknown (not checked or unreadable), never zero. */
  dirty: number | null
  checked: boolean
  created_at: string | null
  by: 'person' | 'agent' | null
  mine: boolean
}

export interface WorktreeList {
  available: boolean
  rows: Worktree[]
}

const w = (ws: string) => `/api/w/${encodeURIComponent(ws)}`

export const worktreesApi = {
  list: (ws: string, ticket?: string) => call<WorktreeList>('GET', `${w(ws)}/worktrees${ticket ? `?ticket=${encodeURIComponent(ticket)}` : ''}`),
  create: (ws: string, body: { ticket: string; repo?: string; base?: 'default' | 'current' }, idem: string) =>
    call<{ worktree: Worktree | null }>('POST', `${w(ws)}/worktrees/create`, { ...body, idem }),
  remove: (ws: string, body: { ticket: string; repo?: string }, idem: string) => call<{ removed: boolean }>('POST', `${w(ws)}/worktrees/remove`, { ...body, idem }),
}

export const worktreeQueries = {
  list: (ws: string) => queryOptions({ queryKey: ['worktrees', ws], queryFn: () => worktreesApi.list(ws), retry: false }),
  ofTicket: (ws: string, ticket: string) => queryOptions({ queryKey: ['worktrees', ws, ticket], queryFn: () => worktreesApi.list(ws, ticket), retry: false }),
}

/** The state in plain words, and whether the worktree can still be used as it is. */
export const STATE_TEXT: Record<WorktreeState, { label: string; detail: string; usable: boolean }> = {
  ok: { label: 'Ready', detail: 'The folder is there and on the ticket’s branch.', usable: true },
  missing: { label: 'Folder is gone', detail: 'The folder was deleted outside orch. Remove it here, then create it again.', usable: false },
  moved: { label: 'Folder was replaced', detail: 'The folder is not the one orch made. Remove it here, then create it again.', usable: false },
  stale_branch: { label: 'Different branch', detail: 'The ticket now uses another branch than this folder has. Remove it here, then create it again.', usable: false },
  unpinned: { label: 'Not usable yet', detail: 'The folder was made, but orch could not take it over safely. Remove it here, then create it again.', usable: false },
  unknown: { label: 'Unknown', detail: 'This dashboard does not know this state.', usable: false },
}

export const SHARED_COPY =
  'No separate worktree: work on this ticket happens in the repository’s shared working copy, where other tickets and you work in the same folder.'

/** Changed files in plain words; unknown is never written as zero. */
export function changesText(w: Pick<Worktree, 'dirty' | 'checked' | 'state'>): string {
  if (w.dirty === null) return w.checked && w.state === 'ok' ? 'changes could not be read' : 'not checked'
  return w.dirty === 0 ? 'no unsaved changes' : `${w.dirty} changed file${w.dirty === 1 ? '' : 's'}`
}

const CODE_TEXT: Record<string, string> = {
  'worktree.no_branch': 'This ticket has no branch for that repository yet. An agent or you set the branch first, then try again.',
  'worktree.exists': 'This ticket already has a worktree for that repository.',
  'worktree.none': 'This ticket has no worktree for that repository.',
  'worktree.dirty': 'The worktree has changes that are not saved in git (or orch cannot check). Commit them first, then remove it.',
  'worktree.in_use': 'An agent run is still attached to this worktree. Stopping agent access does not end a run that already started: open the agents dock at the bottom of the page, press Stop on that run, then remove the worktree.',
  'worktree.branch_in_use': 'That branch is already used by another ticket’s worktree.',
  'worktree.unpinned': 'The folder was made but orch could not take it over safely. Remove it and create it again.',
  'runner.disabled': 'Running commands from the dashboard is switched off for this workspace on this computer, or the Worktrees add-on is not switched on.',
  'runner.stale': 'The helper that runs git is from an older orch version. Restart it in your terminal: orch host enable.',
  'runner.unavailable': 'The helper that runs git is not connected right now. Try again in a moment.',
  'rate.limited': 'Too many worktree changes in a minute. Wait a little and try again.',
  'lock.busy': 'Another worktree change is running on this repository. Try again in a moment.',
  'op.not_allowed': 'You are not allowed to change worktrees for this ticket.',
  'role.denied': 'Your role in this workspace cannot change worktrees.',
  'outcome.unknown': 'The answer got lost. Reload to see whether it worked before you try again.',
  'quota.host': 'This workspace has reached its limit of worktrees. Remove one you no longer need.',
  'observe.unavailable': 'Git cannot be read for this repository right now.',
  invalid: 'That is not a valid choice.',
}

export function worktreeError(e: unknown): string {
  if (!(e instanceof ApiError)) return 'The dashboard server did not answer.'
  return CODE_TEXT[e.code] ?? 'The worktree change did not work. Reload and try again.'
}
