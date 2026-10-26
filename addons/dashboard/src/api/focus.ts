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

export interface TranscriptArgs { after?: string | null; limit?: number; child?: string }

const w = (ws: string) => `/api/w/${encodeURIComponent(ws)}`

export const focusApi = {
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
