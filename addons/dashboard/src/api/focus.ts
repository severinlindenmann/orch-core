// Focus mode phase 2 (focus/1): the dashboard server's typed routes. Nothing here names a host op.
// Transcript text is untrusted (it is whatever the agent printed or read): the page draws it as plain text.

import { call } from './runs'

export interface ChatItem {
  id: string
  role: 'user' | 'assistant' | 'tool' | 'system'
  kind: 'text' | 'tool' | 'result' | 'question' | 'status'
  text: string | null
  ts: string | null
  truncated?: boolean
  tool?: { name: string | null; call: string | null }
  files?: string[]
  options?: string[]
  error?: boolean
}

export interface Transcript {
  run: string
  harness: 'claude-code' | 'codex' | null
  available: boolean
  reason: string | null
  items: ChatItem[]
  /** The cursor for the next read (`o:<offset>`); null while there is no transcript. */
  next: string | null
  eof: boolean
}

export interface DelegationChild {
  id: string
  parent_run: string | null
  title: string
  spawned_by: 'agent' | 'human' | 'promoted'
  harness: 'claude-code' | 'codex' | 'shell' | null
  model: string | null
  depth: number
  state: 'running' | 'done' | 'failed'
  started_at: string | null
  ended_at: string | null
  /** The run id of a child that is a run of its own (human requested); null for a child inside the parent process. */
  run: string | null
  /** Where the child's chat is read: `run.transcript {run, child?}`. */
  transcript: { run: string | null; child: string | null }
  files: string[]
}

export interface DelegationSession {
  run: string
  session: string | null
  harness: 'claude-code' | 'codex' | 'shell' | null
  model: string | null
  state: string | null
  policy: { delegate?: boolean; max_children?: number; models?: string[] } | null
  files: string[]
  visibility: { available: boolean; reason: string | null }
  children: DelegationChild[]
}

export interface Delegations { ticket: string; sessions: DelegationSession[]; supported: boolean }

export interface TranscriptArgs { after?: string | null; limit?: number; child?: string }

const w = (ws: string) => `/api/w/${encodeURIComponent(ws)}`

export const focusApi = {
  delegations: (ws: string, ticket: string) =>
    call<Delegations>('GET', `${w(ws)}/tickets/${encodeURIComponent(ticket)}/delegations`),
  transcript: (ws: string, run: string, args: TranscriptArgs = {}) => {
    const q = new URLSearchParams()
    if (args.after) q.set('after', args.after)
    if (args.limit) q.set('limit', String(args.limit))
    if (args.child) q.set('child', args.child)
    const qs = q.toString()
    return call<Transcript>('GET', `${w(ws)}/runs/${encodeURIComponent(run)}/transcript${qs ? `?${qs}` : ''}`)
  },
}

export const focusQueries = {
  /** The accumulated transcript of one run (or one child of it): the page keeps the cursor and appends each read. */
  transcriptKey: (ws: string, run: string, child?: string) => ['focus', 'transcript', ws, run, child ?? null] as const,
}

/** Items already shown are not added twice (a read can overlap the previous one after a reconnect). */
export function appendItems(old: ChatItem[], next: ChatItem[]): ChatItem[] {
  if (!next.length) return old
  const seen = new Set(old.map((i) => i.id))
  const fresh = next.filter((i) => !seen.has(i.id))
  return fresh.length ? [...old, ...fresh] : old
}

// P2-5: the live diff of a ticket's worktree (uncommitted changes) and the authors the delegation list names.
export interface WorktreeDiffFile {
  path: string
  /** M changed, A added, D deleted, ? untracked */
  status: 'M' | 'A' | 'D' | '?'
  added: number
  removed: number
  binary: boolean
  too_large: boolean
}

export interface WorktreeDiff {
  ticket: string
  repo: string | null
  available: boolean
  reason: string | null
  path_label: string | null
  branch: string | null
  head: string | null
  checked_at: string | null
  truncated: boolean
  files: WorktreeDiffFile[]
  unified: string
}

/** The part of `GET /tickets/{key}/delegations` the diff needs: which session or child touched which file. */
export interface WorktreeDiffAuthors {
  sessions: { files?: string[]; children?: { title?: string | null; files?: string[] }[] }[]
}

export const worktreeDiffApi = {
  get: (ws: string, ticket: string, repo?: string) =>
    call<WorktreeDiff>('GET', `${w(ws)}/tickets/${encodeURIComponent(ticket)}/worktree-diff${repo ? `?repo=${encodeURIComponent(repo)}` : ''}`),
  authors: (ws: string, ticket: string) =>
    call<WorktreeDiffAuthors>('GET', `${w(ws)}/tickets/${encodeURIComponent(ticket)}/delegations`),
}

export const worktreeDiffQueries = {
  diffKey: (ws: string, ticket: string) => ['focus', 'worktree-diff', ws, ticket] as const,
  authorsKey: (ws: string, ticket: string) => ['focus', 'worktree-diff-authors', ws, ticket] as const,
}
