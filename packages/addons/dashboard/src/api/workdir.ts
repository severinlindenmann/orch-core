// The work folder of a ticket without a codebase (real host; design r3 part C). The routes are the dashboard server's
// typed ones. Reading shows how much is in the folder (never its full path); deleting is the person's own action, a
// dashboard-direct host write with one idem per dialog opening. Every string from the host is untrusted.

import { queryOptions } from '@tanstack/react-query'
import { call, newIdem } from './runs'
import { ApiError } from './types'

export { newIdem }

export interface WorkFolder {
  available: boolean
  exists: boolean
  /** The ticket has a codebase (a repo it links): outputs go there, there is no work folder. */
  codebase: boolean
  /** The folder below the project, e.g. `.orch-work/DEMO-0001` (technical detail). */
  folder?: string | null
  files?: number | null
  bytes?: number | null
  truncated?: boolean
}

const w = (ws: string) => `/api/w/${encodeURIComponent(ws)}`

export const workdirApi = {
  get: (ws: string, ticket: string) => call<WorkFolder>('GET', `${w(ws)}/tickets/${encodeURIComponent(ticket)}/workdir`),
  remove: (ws: string, ticket: string, idem: string) => call<{ deleted: boolean }>('POST', `${w(ws)}/workdir/delete`, { ticket, idem }),
}

export const workdirQueries = {
  ofTicket: (ws: string, ticket: string) => queryOptions({ queryKey: ['workdir', ws, ticket], queryFn: () => workdirApi.get(ws, ticket), retry: false }),
}

export function sizeText(bytes: number): string {
  if (bytes < 1024) return `${bytes} bytes`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** "14 files, 2.1 MB" (a truncated count says "more than"). */
export function folderSummary(f: Pick<WorkFolder, 'files' | 'bytes' | 'truncated'>): string {
  if (f.files == null || f.bytes == null) return 'size not known'
  const files = `${f.truncated ? 'more than ' : ''}${f.files} file${f.files === 1 ? '' : 's'}`
  return `${files}, ${sizeText(f.bytes)}`
}

const CODE_TEXT: Record<string, string> = {
  'worktree.in_use': 'An agent run is still working in this folder. Open the agents dock at the bottom of the page, press Stop on that run, then delete the folder.',
  'op.not_allowed': 'Only an owner, a maintainer, the ticket’s owner or an assignee can delete this folder.',
  'role.denied': 'Your role in this workspace cannot delete this folder.',
  'outcome.unknown': 'The answer got lost. Reload to see whether it worked before you try again.',
  invalid: 'That is not a valid choice.',
}

export function workdirError(e: unknown): string {
  if (!(e instanceof ApiError)) return 'The dashboard server did not answer.'
  return CODE_TEXT[e.code] ?? 'The folder could not be deleted. Reload and try again.'
}
