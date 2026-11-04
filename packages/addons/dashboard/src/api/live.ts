// Focus mode "live agents + jobs" (prototype, display only). The mock host serves a simulated ticket (mocks/addons/focus/live.ts);
// a real host has no such route yet, so the live screens are drawn only on the mock. Nothing here is signed or trusted.

import { queryOptions } from '@tanstack/react-query'
import { isRealHost } from './features'
import { liveAct, liveSnapshot, liveWorkLog, type LiveAction } from '@/mocks/addons/focus/live'

export type LiveState = 'working' | 'blocked' | 'done' | 'failed' | 'queued'
export type MilestoneIcon = 'ask' | 'helper' | 'edit' | 'search' | 'retry' | 'run' | 'done' | 'wait' | 'start'

export interface Milestone {
  id: string
  at: string
  icon: MilestoneIcon
  text: string
  tone?: 'ok' | 'past' | 'live' | 'warn'
  right?: string
  command?: string
  output?: string
}

export interface LiveCache { lastActivity: string; ttl: number; tokens: number; missUsd: number }

export interface LiveAgent {
  id: string
  kind: 'main' | 'helper' | 'task'
  tool: 'claude' | 'codex' | 'local'
  name: string
  parent: string | null
  depth: number
  state: LiveState
  stateText: string
  now: string
  /** Start of the running timer (ISO), else null. */
  since: string | null
  model: string
  account: string
  accountKey: string | null
  tokens: { read: number; reused: number; written: number }
  usd: number
  /** Conversation space used, percent. */
  space: number
  cache: LiveCache | null
  milestones: Milestone[]
  chat: { from: 'you' | 'agent'; text: string }[]
  terminal: string
  request: string | null
}

export interface LiveCommand {
  id: string
  agent: string
  command: string
  status: 'running' | 'waiting' | 'ok' | 'failed'
  duration: string
  since: string | null
  output: string
  redacted: boolean
}

export interface LiveJob {
  id: string
  parent: string
  name: string
  source: string
  via: 'shell' | 'skill'
  cmd: string
  state: 'working' | 'done' | 'failed'
  progress: { done: number; total: number; unit: string }
  steps: { name: string; state: 'ok' | 'running' | 'queued' | 'failed'; time: string }[]
  metrics: { label: string; value: string }[]
  link: { label: string; url: string } | null
  images: { name: string; src: string | null }[]
  table: { title: string; cols: string[]; rows: string[][] } | null
  log: string
  summary: string
  now: string
  since: string
  ignored: boolean
  fixAsked: boolean
}

export interface LiveRequest {
  id: string
  agent: string
  title: string
  changes: string[]
  command: string
  askedBy: string
}

export interface LiveAccount { key: string; label: string; pct: number; reset: string }

export interface LiveSnapshot {
  ticket: string
  title: string
  agents: LiveAgent[]
  jobs: LiveJob[]
  commands: LiveCommand[]
  requests: LiveRequest[]
  accounts: LiveAccount[]
  events: Record<string, unknown>[]
}

export interface WorkRow {
  time: string
  icon: MilestoneIcon | 'job' | 'you'
  lead: string
  text: string
  tags: ('job' | 'you' | 'bad')[]
  status?: { tone: 'ok' | 'bad'; text: string }
  right?: string
  link?: string
  thumbs?: number
  table?: string[][]
}

export interface WorkSession {
  id: string
  day: string
  when: string
  live: boolean
  who: string
  tool: 'claude' | 'codex' | 'local'
  account: string
  what: string
  chips: string[]
  rows: WorkRow[]
  actions: string[]
}

export interface WorkLog { ticket: string; sessions: WorkSession[]; days: number; usd: number }

export const liveApi = {
  snapshot: (_ws: string, ticket: string): Promise<LiveSnapshot | null> => Promise.resolve(isRealHost() ? null : liveSnapshot(ticket)),
  work: (_ws: string, ticket: string): Promise<WorkLog | null> => Promise.resolve(isRealHost() ? null : liveWorkLog(ticket)),
  act: (_ws: string, ticket: string, action: LiveAction): Promise<void> => { if (!isRealHost()) liveAct(ticket, action); return Promise.resolve() },
}
export type { LiveAction }

export const liveQueries = {
  snapshotKey: (ws: string, ticket: string) => ['focus', 'live', ws, ticket] as const,
  snapshot: (ws: string, ticket: string) => queryOptions({
    queryKey: ['focus', 'live', ws, ticket] as const,
    queryFn: () => liveApi.snapshot(ws, ticket),
    // The mock answers a new picture on every poll; a real host has none.
    refetchInterval: isRealHost() ? false : 2000, staleTime: 0, retry: false,
  }),
  work: (ws: string, ticket: string) => queryOptions({ queryKey: ['focus', 'work', ws, ticket] as const, queryFn: () => liveApi.work(ws, ticket), retry: false, refetchInterval: isRealHost() ? false : 4000 }),
}
