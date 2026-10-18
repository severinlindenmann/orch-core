// File Explorer of a real host (issue #491): read only, owners only. The routes are the dashboard server's typed ones;
// nothing here names a host op, a git argument or an absolute path. A folder is a root id (`ws`, `repo:<name>`) and a
// relative path. Every name, subject and file text is untrusted: the page shows it as text (visible.tsx, React escaping),
// markdown through the sanitising renderer, images only as the `data:` URL of an allow-listed type.

import { queryOptions } from '@tanstack/react-query'
import { call } from './runs'

export interface Root {
  id: string
  label: string
  kind: 'workspace' | 'repo'
  repo: boolean
}
export interface ExplorerInfo {
  available: boolean
  roots: Root[]
  search: { rg: boolean; git: boolean }
}
export interface Entry {
  name: string
  /** The name with invisible characters made visible. */
  label: string
  kind: 'dir' | 'file' | 'link' | 'other'
  size?: number
  repo: boolean
  /** Why the entry is locked (shown, never opened), or null. */
  locked: string | null
}
export interface Tree {
  root: string
  path: string
  entries: Entry[]
  more: boolean
}
export type FileKind = 'text' | 'markdown' | 'image' | 'binary' | 'too_large' | 'empty' | 'locked' | 'link' | 'dir' | 'other'
export interface FileView {
  root: string
  path: string
  name: string
  label: string
  kind: FileKind
  rev?: string
  reason?: string
  size?: number
  ext?: string
  text?: string
  language?: string | null
  lines?: number
  hidden_chars?: boolean
  mime?: string
  data?: string
  modified?: string
  history?: boolean
}
export interface TicketChip {
  key: string
  title: string | null
}
export interface Commit {
  sha: string
  short: string
  author: string
  at: string
  subject: string
  tickets: TicketChip[]
}
export interface History {
  repo: boolean
  commits: Commit[]
  more: boolean
}
export interface CommitView extends Commit {
  body: string
  patch: string
  truncated: boolean
  empty: boolean
}
export interface Compare {
  a: string
  b: string
  patch: string
  truncated: boolean
  empty: boolean
}
export interface Hit {
  root: string
  path: string
  label: string
  line: number
  text: string
}
export interface SearchResult {
  engine: 'rg' | 'git' | null
  hits: Hit[]
  more: boolean
}
export interface NameResult {
  files: { root: string; path: string; label: string }[]
  cut: boolean
}
export interface ChangedFile {
  root: string
  path: string
  label: string
  status: 'A' | 'M' | 'D' | 'R' | 'T' | 'C'
  sha: string
  short: string
  via: 'push' | 'task'
  repo: string | null
}

const w = (ws: string) => `/api/w/${encodeURIComponent(ws)}/explorer`
const qs = (o: Record<string, string | number | undefined | null>) => {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== null && v !== '') p.set(k, String(v))
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const explorerApi = {
  info: (ws: string) => call<ExplorerInfo>('GET', w(ws)),
  tree: (ws: string, root: string, path: string) => call<Tree>('GET', `${w(ws)}/tree${qs({ root, path })}`),
  file: (ws: string, root: string, path: string, rev?: string) => call<FileView>('GET', `${w(ws)}/file${qs({ root, path, rev })}`),
  names: (ws: string, q: string, root?: string) => call<NameResult>('GET', `${w(ws)}/names${qs({ q, root })}`),
  search: (ws: string, q: string, root?: string, path?: string) => call<SearchResult>('GET', `${w(ws)}/search${qs({ q, root, path })}`),
  history: (ws: string, root: string, path: string, skip: number, limit = 50) => call<History>('GET', `${w(ws)}/history${qs({ root, path, skip, limit })}`),
  commit: (ws: string, root: string, path: string, sha: string) => call<CommitView>('GET', `${w(ws)}/commit${qs({ root, path, sha })}`),
  compare: (ws: string, root: string, path: string, a: string, b: string) => call<Compare>('GET', `${w(ws)}/compare${qs({ root, path, a, b })}`),
  ticketFiles: (ws: string, key: string) => call<{ files: ChangedFile[]; more: boolean }>('GET', `${w(ws)}/ticket/${encodeURIComponent(key)}`),
}

export const explorerQueries = {
  info: (ws: string) => queryOptions({ queryKey: ['explorer', ws, 'info'], queryFn: () => explorerApi.info(ws), retry: false }),
  tree: (ws: string, root: string, path: string) =>
    queryOptions({ queryKey: ['explorer', ws, 'tree', root, path], queryFn: () => explorerApi.tree(ws, root, path), retry: false, staleTime: 10_000 }),
  file: (ws: string, root: string, path: string, rev?: string) =>
    queryOptions({ queryKey: ['explorer', ws, 'file', root, path, rev ?? ''], queryFn: () => explorerApi.file(ws, root, path, rev), retry: false }),
  names: (ws: string, q: string, root?: string) =>
    queryOptions({ queryKey: ['explorer', ws, 'names', q, root ?? ''], queryFn: () => explorerApi.names(ws, q, root), retry: false }),
  search: (ws: string, q: string, root?: string, path?: string) =>
    queryOptions({ queryKey: ['explorer', ws, 'search', q, root ?? '', path ?? ''], queryFn: () => explorerApi.search(ws, q, root, path), retry: false }),
  commit: (ws: string, root: string, path: string, sha: string) =>
    queryOptions({ queryKey: ['explorer', ws, 'commit', root, path, sha], queryFn: () => explorerApi.commit(ws, root, path, sha), retry: false }),
  compare: (ws: string, root: string, path: string, a: string, b: string) =>
    queryOptions({ queryKey: ['explorer', ws, 'compare', root, path, a, b], queryFn: () => explorerApi.compare(ws, root, path, a, b), retry: false }),
  ticketFiles: (ws: string, key: string) =>
    queryOptions({ queryKey: ['explorer', ws, 'ticket', key], queryFn: () => explorerApi.ticketFiles(ws, key), retry: false, staleTime: 30_000 }),
}

/** The sentence under a locked entry or file, in plain words. */
export const LOCKED_TEXT: Record<string, string> = {
  'Git internals': 'Git’s own files are never shown. Use the History tab of a file instead.',
  "orch's own files": 'orch’s own files (tickets, keys, event logs) are never shown here.',
  Keys: 'Key files are never shown here.',
  'Ticket worktrees': 'Ticket worktree folders are never shown here.',
  'Secrets (.env)': 'Files with secrets (.env) are never shown here.',
}
export const lockedText = (reason: string | null | undefined) => (reason && LOCKED_TEXT[reason]) || 'This file is locked and is never shown here.'

/** `a/b/c.txt` -> `['a','b','c.txt']`; `''` -> `[]`. */
export const partsOf = (path: string) => (path ? path.split('/') : [])
export const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name)
