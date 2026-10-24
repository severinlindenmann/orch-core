// Shares of a real host (the Publish add-on): share files of a ticket with a link. Real host only (the mockup keeps
// its own Publish addon page). The routes are the dashboard server's typed ones; nothing here names a host op.
//
// THE LINK. `sharesApi.send` is the only call whose answer holds a link, and its result goes to the one component that
// shows it, in component state. It is never put in a React Query cache or mutation cache (no `useMutation`, no
// `queryOptions` for it), never in localStorage/sessionStorage, never logged, never part of an error. The list
// (`shareQueries.list`) has no link field at all: the host keeps no link.

import { queryOptions } from '@tanstack/react-query'
import { call, newIdem } from './runs'
import { ApiError } from './types'

export { newIdem }

export type ShareKind = 'secret' | 'once'
export type ShareState = 'live' | 'sending' | 'revoked' | 'expired' | 'used' | 'unknown'

export interface StagedFile {
  name: string
  size: number
  sha256: string
}

export interface Share {
  id: string
  ticket: string | null
  title: string | null
  kind: ShareKind | 'unknown'
  state: ShareState
  expires_at: string | null
  /** 0: no limit (a secret link). */
  view_limit: number | null
  views: number | null
  files: string[]
  created_at: string | null
  server: string | null
}

/** A copy made aside, waiting for a person's signature. Nothing has left the computer. */
export interface Staged {
  staged_id: string
  ticket: string | null
  kind: ShareKind | 'unknown'
  days: number | null
  views: number | null
  origin: 'agent' | 'person' | null
  files: StagedFile[]
  created_at: string | null
}

export type SharesData =
  | { available: false }
  | {
      available: true
      configured: boolean
      server: string | null
      share: string | null
      workspace: string | null
      owner: string | null
      /** The server answered with its own numbers; false: these are this computer's records. */
      live: boolean
      shares: Share[]
      staged: Staged[]
    }

export interface Sent {
  share_id: string | null
  ticket: string | null
  kind: ShareKind | 'unknown'
  /** The link. Shown once, kept nowhere. */
  link: string
  expires_at: string | null
  view_limit: number | null
}

export const DAYS = [1, 3, 7, 30] as const
export const VIEWS = [1, 3, 10] as const
export type Days = (typeof DAYS)[number]
export type Views = (typeof VIEWS)[number]

const w = (ws: string) => `/api/w/${encodeURIComponent(ws)}`

export const sharesApi = {
  list: (ws: string, ticket?: string) => call<SharesData>('GET', `${w(ws)}/shares${ticket ? `?ticket=${encodeURIComponent(ticket)}` : ''}`),
  prepare: (ws: string, body: { ticket: string; artifacts: string[]; kind: ShareKind; days: Days; views?: Views }, idem: string) =>
    call<{ staged: Staged; server: string | null; share: string | null }>('POST', `${w(ws)}/shares/prepare`, { ...body, idem }),
  /** The one answer with a link. Do not store the result anywhere but the component that shows it. */
  send: (ws: string, staged_id: string, idem: string) => call<Sent>('POST', `${w(ws)}/shares/send`, { staged_id, idem }),
  revoke: (ws: string, share_id: string, idem: string) => call<{ revoked: boolean }>('POST', `${w(ws)}/shares/revoke`, { share_id, idem }),
  discard: (ws: string, staged_id: string, idem: string) => call<{ discarded: boolean }>('POST', `${w(ws)}/shares/discard`, { staged_id, idem }),
  register: (ws: string, idem: string) => call<{ registered: boolean; result: 'registered' | 'unchanged' }>('POST', `${w(ws)}/shares/register`, { idem }),
}

export const shareQueries = {
  list: (ws: string) => queryOptions({ queryKey: ['shares', ws], queryFn: () => sharesApi.list(ws), retry: false }),
  ofTicket: (ws: string, ticket: string) => queryOptions({ queryKey: ['shares', ws, ticket], queryFn: () => sharesApi.list(ws, ticket), retry: false }),
}

export const kindText = (kind: Share['kind'], limit?: number | null): string =>
  kind === 'secret' ? 'Anyone with the link' : kind === 'once' ? `Limited-view link · up to ${limit ?? 1} view${(limit ?? 1) === 1 ? '' : 's'}` : 'Unknown'

export const STATE_TEXT: Record<ShareState, string> = {
  live: 'Active',
  sending: 'Sending',
  revoked: 'Stopped',
  expired: 'Expired',
  used: 'Used up',
  unknown: 'Unknown',
}

/** Is a share past its time, or a show-once link already opened as often as allowed, although the records still say live? */
export const effectiveState = (s: Pick<Share, 'state' | 'expires_at'> & Partial<Pick<Share, 'kind' | 'views' | 'view_limit'>>, now = Date.now()): ShareState => {
  if (s.state !== 'live') return s.state
  if (s.expires_at && Date.parse(s.expires_at) <= now) return 'expired'
  if (s.kind === 'once' && s.view_limit && s.views != null && s.views >= s.view_limit) return 'used'
  return s.state
}

export const viewsText = (s: Pick<Share, 'kind' | 'views' | 'view_limit'>): string =>
  s.views === null ? 'not known' : s.kind === 'once' && s.view_limit ? `${s.views} of ${s.view_limit}` : String(s.views)

/** What a ticket's file needs to be shareable: a real file whose name the server takes, within the limits. */
export const LIMITS = { files: 20, file_bytes: 25 * 1024 * 1024, share_bytes: 50 * 1024 * 1024 }
const NAME = /^[A-Za-z0-9][A-Za-z0-9._ -]{0,99}$/

export function unshareable(a: { name: string; kind: string; bytes: number | null; addon?: string; url?: string }): string | null {
  if (a.kind === 'link' || a.url || a.addon) return 'This is a link or an add-on item, not a file.'
  if (!NAME.test(a.name) || a.name.includes('..') || a.name !== a.name.trim()) return 'The name has characters the server does not take (letters, digits, space, dot, dash and underscore only).'
  if (a.bytes !== null && a.bytes > LIMITS.file_bytes) return 'Larger than 25 MB.'
  return null
}

export const fmtSize = (n: number): string => (n < 1024 ? `${n} bytes` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / (1024 * 1024)).toFixed(1)} MB`)

/** The server's reasons in words. `unknown_workspace` names the admin step. */
export const REASON_TEXT: Record<string, string> = {
  unknown_workspace: 'The sharing server has not allowed this workspace yet. Ask the person who runs the server to allow it.',
  blocked: 'The server’s admin has blocked this workspace.',
  owner_not_allowed: 'The server’s admin has not allowed the owner of this workspace.',
  limit: 'The server refused: a size or count limit is reached.',
  conflict: 'The server holds other details for this workspace. Its admin has to reset that.',
  clock: 'This computer’s clock is off. Fix the time and try again.',
  delegation: 'This workspace’s identity could not be read.',
  rejected: 'The server refused this.',
}

const CODE_TEXT: Record<string, string> = {
  'publish.not_configured': 'No publish server is set up yet. Set one up in Settings, Add-ons.',
  'publish.not_approved': 'A signature for exactly these files and this server is missing or too old. Sign it again.',
  'publish.changed': 'The prepared copy changed or is gone. Prepare the share again.',
  'publish.unreachable': 'The publish server could not be reached. Nothing was shared.',
  'publish.no_link': 'The files were shared but no link came back. Revoke this share and share again.',
  'runner.disabled': 'Publish is switched off for this workspace. Turn it on in Settings, Add-ons.',
  'rate.limited': 'Too many publish requests in a minute. Wait a little and try again.',
  'lock.busy': 'This copy is being sent right now. Try again in a moment.',
  'op.not_allowed': 'Only a person does this.',
  'role.denied': 'Sharing is for the ticket owner, owners and maintainers.',
  'quota.host': 'Too many prepared copies are waiting, or they are too large. Discard some.',
  'outcome.unknown': 'The answer got lost. Reload to see whether it worked before you try again. If you lost the link, revoke the share and share again.',
  invalid: 'That is not a valid choice.',
  not_found: 'That share or copy is not there any more.',
}

/** Plain words for a failed call. The error's message is never echoed (it could carry text from elsewhere). */
export function shareError(e: unknown): string {
  if (!(e instanceof ApiError)) return 'The dashboard server did not answer.'
  const reason = (e.detail as { reason?: string } | undefined)?.reason
  if (e.code === 'publish.server_refused') return (reason && REASON_TEXT[reason]) || REASON_TEXT.rejected
  return CODE_TEXT[e.code] ?? 'That did not work. Reload and try again.'
}

export const isUnknownWorkspace = (e: unknown): boolean => e instanceof ApiError && e.code === 'publish.server_refused' && (e.detail as { reason?: string } | undefined)?.reason === 'unknown_workspace'

/** The command the server's admin runs (`orch-publish admin ws allow WORKSPACE OWNER`). Shown under Technical details. */
/** Is this address one that opens only on this computer? */
export const isLoopback = (origin: string | null): boolean => !!origin && /^http:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?$/.test(origin)

/** What the two addresses mean for who can open the links. null: nothing to add (both public, or one is not known). */
export function reachNote(server: string | null, share: string | null): string | null {
  if (!server || !share) return null
  const s = isLoopback(server)
  const l = isLoopback(share)
  const host = (o: string) => o.replace(/^https?:\/\//, '')
  if (s && l) return 'Local test link: opens only on this computer, so nobody else can use it.'
  if (l) return `Links open only on this computer, but files are uploaded to ${host(server)}, which other people can reach.`
  if (s) return `Links open from ${host(share)}, which other people can reach; files are uploaded to this computer only.`
  return null
}

export const adminCommand = (workspace: string | null, owner: string | null): string => `orch-publish admin ws allow ${workspace ?? 'WORKSPACE_ID'} ${owner ?? 'OWNER_ID'}`

/** The accessible name of a Stop sharing button: which files, and when the link ends. */
export function stopName(s: Pick<Share, 'ticket' | 'files' | 'expires_at'>, fmt: (iso: string) => string): string {
  const files = s.files.length ? `${s.files.length} file${s.files.length === 1 ? '' : 's'}: ${s.files.slice(0, 3).join(', ')}${s.files.length > 3 ? ' and more' : ''}` : `the share of ${s.ticket ?? 'this ticket'}`
  return `Stop sharing ${files}${s.expires_at ? `, link ends ${fmt(s.expires_at)}` : ''}`
}
