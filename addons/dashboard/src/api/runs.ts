// Runs and terminals of a real host (M7a, contract v1.6 section 12.3/12.6). Real host only: the mockup keeps its
// terminals addon. The routes are the dashboard server's typed ones; nothing here names a host op or a socket path.
// Every string from the host (label, command line, path, message) is untrusted: the page shows it through visible.tsx.

import { queryOptions, type QueryClient } from '@tanstack/react-query'
import { clearSession, getToken } from './bootstrap'
import { createFetchTransport } from './fetchTransport'
import { ApiError, type ApiErrorBody } from './types'

export interface RunExit {
  code: number | null
  signal: string | null
  at: string
  reason: 'exited' | 'stopped' | 'spawn_failed' | 'runner_restarted' | 'lost'
  cleanup: 'complete' | 'incomplete' | 'unknown' | null
}

export interface Run {
  run: string
  rev: number
  kind: 'agent' | 'shell'
  ticket: string | null
  harness: string
  mode: string | null
  model: string | null
  as_person: boolean
  state: 'starting' | 'running' | 'exited' | 'lost'
  started_at: string
  viewers: number
  /** Someone (any tab or device of this person) holds the write lease. */
  writer: boolean
  cols: number
  rows: number
  /** Until when an attach can replay the ring; null: running, or nothing kept. */
  replay_until: string | null
  label: string | null
  label_hidden: boolean
  cwd: { kind?: string | null; repo?: string | null; ticket?: string | null }
  exit: RunExit | null
}

export interface RunnerInfo {
  connected: boolean
  enabled: boolean
  terminals: boolean
}

export interface RunList {
  runs: Run[]
  runner: RunnerInfo | null
  truncated: boolean
}

export interface TermPreview {
  spec: string
  display: string | null
  display_hidden: boolean
  cwd_path: string | null
  cwd_path_hidden: boolean
  notice: string | null
  as_person: boolean
  login_shell: boolean
  env: string[]
  blocked: { code: string | null; message: string | null } | null
  warnings: string[]
}

export interface TermArgs {
  harness?: 'shell'
  ticket?: string
  label?: string
}

export type RunItem = { t: 'run'; run: Run } | { t: 'gone'; run: string }

async function call<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
  const token = getToken()
  const result = await createFetchTransport('', () => token).request(method, path, body)
  if (result.status === 401) {
    clearSession(true, token)
    throw new ApiError(401, { code: 'session.required', message: 'Run orch dashboard in your terminal', retryable: false })
  }
  if (result.status >= 400) {
    const err = result.json as ApiErrorBody | null
    throw new ApiError(result.status, err?.error ?? { code: 'host.unavailable', message: 'Host not available', retryable: result.status >= 500 }, err?.exit)
  }
  return result.json as T
}

const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
/** One idem per confirmed preview (`a_` + a ULID-shaped random id); a retry of the same click reuses it. */
export function newIdem(random: (n: number) => Uint8Array = (n) => crypto.getRandomValues(new Uint8Array(n))): string {
  const bytes = random(25)
  const tail = Array.from(bytes, (b) => CROCKFORD[b & 31]).join('')
  return `a_${CROCKFORD[bytes[0] & 7]}${tail.slice(1)}`
}

const w = (ws: string) => `/api/w/${encodeURIComponent(ws)}`

export const runsApi = {
  list: (ws: string) => call<RunList>('GET', `${w(ws)}/runs`),
  preview: (ws: string, args: TermArgs) => call<TermPreview>('POST', `${w(ws)}/terminals/preview`, args),
  open: (ws: string, args: TermArgs, spec: string, idem: string, size?: { cols: number; rows: number }) =>
    call<{ run: Run }>('POST', `${w(ws)}/terminals/open`, { ...args, spec, idem, ...(size ?? {}) }),
  stop: (ws: string, run: string) => call<{ state: 'exited' | 'stopping' | null; cleanup: RunExit['cleanup'] }>('POST', `${w(ws)}/runs/${encodeURIComponent(run)}/stop`, {}),
  /** Step 1 of the lazy attach: a browser ticket. The server asks nothing of the host until the WebSocket presents it. */
  ticket: (ws: string, run: string, mode: 'read' | 'write', take = false, from?: number) =>
    call<{ ticket: string; expires_in_ms: number }>('POST', `${w(ws)}/runs/${encodeURIComponent(run)}/attach`, { mode, ...(take ? { take } : {}), ...(from !== undefined ? { from } : {}) }),
}

export const runQueries = {
  list: (ws: string) => queryOptions({ queryKey: ['runs', ws], queryFn: () => runsApi.list(ws) }),
}

/** A `runs` item of the live stream: merged into the list by `rev` (an older revision never wins); `gone` removes. */
export function mergeRunItems(client: QueryClient, ws: string, items: RunItem[]) {
  client.setQueryData<RunList>(['runs', ws], (list) => {
    if (!list) return list
    let runs = list.runs
    for (const item of items) {
      if (item.t === 'gone') runs = runs.filter((r) => r.run !== item.run)
      else {
        const at = runs.findIndex((r) => r.run === item.run.run)
        if (at < 0) runs = [item.run, ...runs]
        else if (runs[at].rev <= item.run.rev) runs = runs.map((r, i) => (i === at ? item.run : r))
      }
    }
    return { ...list, runs }
  })
}

/** "Output no longer kept": an ended run whose ring is gone (or was never kept). The dock does not try to attach. */
export function outputGone(run: Run, now = Date.now()): boolean {
  if (run.state === 'running' || run.state === 'starting') return false
  return run.replay_until === null || Date.parse(run.replay_until) <= now
}

/** "Some processes may still be running" after a stop whose cleanup was not complete. */
export const cleanupDoubt = (cleanup: RunExit['cleanup'] | undefined) => cleanup === 'incomplete' || cleanup === 'unknown'
