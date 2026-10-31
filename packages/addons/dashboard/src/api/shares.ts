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

/** A file picked on this computer: kept aside by the host until Review; nothing is shared. */
export interface Picked {
  upload_id: string
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
      /**
       * Only when `live` is false: no answer (`unreachable`), an answer that refused (`not_allowed`), or no hub (`local`);
       * `hub_left`: the settings came from a hub this workspace is no longer bound to; `hub_moved`: the hub's address moved off them.
       */
      why?: 'unreachable' | 'not_allowed' | 'local' | 'hub_left' | 'hub_moved'
      /** With `why: 'hub_moved'`: the hub's new values (`share` is shown by its host name). */
      hub_moved?: { server: string; share: string }
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
  /** One picked file (P05): its bytes as base64. At most `PICK_MAX_BYTES`. */
  upload: (ws: string, body: { name: string; data: string }, idem: string) => call<Picked>('POST', `${w(ws)}/shares/upload`, { ...body, idem }),
  prepare: (ws: string, body: { ticket: string; artifacts: string[]; uploads?: string[]; /** Files of the ticket's own work folder, by name; the host finds them there. */ workfiles?: string[]; kind: ShareKind; days: Days; views?: Views }, idem: string) =>
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
/** The most a file picked on this computer may hold (the host holds the same limit). */
export const PICK_MAX_BYTES = 1024 * 1024

/** Why a file picked on this computer cannot be added, or null. Checked before any byte is read. */
export function unpickable(file: { name: string; size: number }, taken: readonly string[]): string | null {
  if (!NAME.test(file.name) || file.name.includes('..') || file.name !== file.name.trim()) return 'The name has characters the server does not take (letters, digits, space, dot, dash and underscore only).'
  if (file.size === 0) return 'This file is empty.'
  if (file.size > PICK_MAX_BYTES) return 'Larger than 1 MB. Add it to the ticket first, or pick a smaller file.'
  if (taken.some((n) => n.toLowerCase() === file.name.toLowerCase())) return 'A file with this name is already in the list.'
  return null
}

/** The bytes of a File as base64, in pieces so a large string is never spread into one call. */
export async function fileBase64(file: Blob): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer())
  let bin = ''
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return btoa(bin)
}

export function unshareable(a: { name: string; kind: string; bytes: number | null; addon?: string; url?: string }): string | null {
  if (a.kind === 'link' || a.url || a.addon) return 'This is a link or an add-on item, not a file.'
  if (!NAME.test(a.name) || a.name.includes('..') || a.name !== a.name.trim()) return 'The name has characters the server does not take (letters, digits, space, dot, dash and underscore only).'
  if (a.bytes !== null && a.bytes > LIMITS.file_bytes) return 'Larger than 25 MB.'
  if (a.bytes === 0) return 'The file is empty.'
  return null
}

export const fmtSize = (n: number): string => (n < 1024 ? `${n} bytes` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / (1024 * 1024)).toFixed(1)} MB`)

/** The host's words when the settings came from a hub this workspace is no longer bound to (`hub_left`). The name is the hub's display name when the page knows it. */
export const hubLeftText = (hubName?: string | null): string => `Sharing went through your hub${hubName ? ` ${hubName}` : ''}. Connect again, or set a sharing server.`
const HUB_LEFT_TEXT = hubLeftText()

/** The paused line when the hub has moved: the new share host only, by its A-label name. */
export const hubMovedText = (share: string): string => `Sharing paused: your hub now shares links through ${hostOf(share)}.`

/** The server's reasons in words. `unknown_workspace` names the admin step. */
export const REASON_TEXT: Record<string, string> = {
  unknown_workspace: 'Your hub has not allowed this workspace to share yet. Ask the hub’s admin.',
  blocked: 'The server’s admin has blocked this workspace.',
  owner_not_allowed: 'The server’s admin has not allowed the owner of this workspace.',
  limit: 'The server refused: a size or count limit is reached.',
  conflict: 'The server holds other details for this workspace. Its admin has to reset that.',
  clock: 'This computer’s clock is off. Fix the time and try again.',
  delegation: 'This workspace’s identity could not be read.',
  rejected: 'The server refused this.',
  hub_left: HUB_LEFT_TEXT,
  hub_moved: 'Sharing paused: your hub now shares links through a new address. Use the new address in Settings, Hub.',
}

const CODE_TEXT: Record<string, string> = {
  'publish.not_configured': 'No publish server is set up yet. Set one up on the Add-ons page.',
  'publish.not_approved': 'A signature for exactly these files and this server is missing or too old. Sign it again.',
  'publish.changed': 'The prepared copy changed or is gone. Prepare the share again.',
  'publish.unreachable': 'Sharing is paused: your hub cannot be reached. Your files were not sent.',
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

export const isUnreachable = (e: unknown): boolean => e instanceof ApiError && e.code === 'publish.unreachable'

export const isUnknownWorkspace = (e: unknown): boolean => e instanceof ApiError && e.code === 'publish.server_refused' && (e.detail as { reason?: string } | undefined)?.reason === 'unknown_workspace'

/** The command the server's admin runs (`orch-publish admin ws allow WORKSPACE OWNER`). Shown under Technical details. */
/** Is this address one that opens only on this computer? */
export const isLoopback = (origin: string | null): boolean => !!origin && /^http:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?$/.test(origin)

/**
 * What the two addresses mean for who can open the links. null: nothing to add (both public, or one is not known).
 * "This computer" (and "nobody else") only when `from` says the Publish server is on this computer: a loopback address alone
 * does not say so, a hub can be reached through one.
 */
export function reachNote(server: string | null, share: string | null, from: 'computer' | 'hub' | null = null): string | null {
  if (!server || !share) return null
  const s = isLoopback(server)
  const l = isLoopback(share)
  const here = from === 'computer'
  if (s && l) return here ? 'Local test link: opens only on this computer, so nobody else can use it.' : null
  if (l) return `Links open only ${here ? 'on this computer' : `at ${hostOf(share)}`}, but files are uploaded to ${hostOf(server)}, which other people can reach.`
  if (s) return `Links open from ${hostOf(share)}, which other people can reach; files are uploaded to ${here ? 'this computer only' : hostOf(server)}.`
  return null
}

/** Where files go, by name and never by address: the one plain line every publish review keeps in its main view (full addresses stay in Details). */
export function destinationLine(from: 'computer' | 'hub' | null | undefined, hubName?: string | null, server?: string | null): string | null {
  if (!server) return null // unknown: no line, never a guess
  if (from === 'hub') return hubName ? `Files go to your hub, ${hubName}.` : 'Files go to your hub.'
  // "this computer" only when the Publish server on this computer is the known source; a loopback address alone does not say so (a hub can be reached through one)
  if (from === 'computer') return 'Files go to this computer.'
  return `Files go to ${hostOf(server)}.`
}

/** The host name of an address, parsed (never cut out by pattern). An address that does not parse is shown whole, as given. */
export function hostOf(address: string): string {
  try {
    return new URL(address).hostname || address
  } catch {
    return address
  }
}

/** The same address, compared by origin so a trailing slash does not matter; an address that does not parse is compared as given. */
/** Two addresses name the same http(s) server. Fails closed: an address that does not parse, or has an opaque origin
 * (`new URL('a:b').origin` is the string "null" for every non-http scheme), never matches anything, since this is
 * the gate for a "this computer" or hub-name claim. */
export function sameOrigin(a: string, b: string): boolean {
  const origin = (x: string): string | null => {
    try {
      const o = new URL(x).origin
      return o === 'null' ? null : o
    } catch {
      return null
    }
  }
  const oa = origin(a)
  return oa !== null && oa === origin(b)
}

export const adminCommand = (workspace: string | null, owner: string | null): string => `orch-publish admin ws allow ${workspace ?? 'WORKSPACE_ID'} ${owner ?? 'OWNER_ID'}`

/** The accessible name of a Stop sharing button: which files, and when the link ends. */
export function stopName(s: Pick<Share, 'ticket' | 'files' | 'expires_at'>, fmt: (iso: string) => string): string {
  const files = s.files.length ? `${s.files.length} file${s.files.length === 1 ? '' : 's'}: ${s.files.slice(0, 3).join(', ')}${s.files.length > 3 ? ' and more' : ''}` : `the share of ${s.ticket ?? 'this ticket'}`
  return `Stop sharing ${files}${s.expires_at ? `, link ends ${fmt(s.expires_at)}` : ''}`
}
