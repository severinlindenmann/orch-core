// Pure workspace rules shared by the mock and the UI (part of the API contract).
import type { Workspace } from './types'

/** Does the host of this workspace answer? (Absent or `ok`; the mock always does.) */
export const isRunning = (w: Pick<Workspace, 'host_state'>): boolean => !w.host_state || w.host_state === 'ok'

/** The workspaces whose host answers: every page works on these. */
export const running = <W extends Pick<Workspace, 'host_state'>>(workspaces: readonly W[]): W[] => workspaces.filter(isRunning)

/**
 * WS8: the LAST 8 hex of a workspace id (stream A's fragment, like KEY/FRAG). The one short form shown everywhere (switcher, hand-off
 * line, the signer's confirmation word) and the `?ws=ab12cd34` hint an address adds when two workspaces share a prefix. A link made
 * with the first 8 (an older form) names nothing here, so it asks which workspace.
 */
export const uuid8 = (id: string): string => id.replace(/-/g, '').slice(-8).toLowerCase()

/** Do these workspaces share the prefix (compared the way addresses are: regardless of case)? */
export const sharesPrefix = (a: Pick<Workspace, 'prefix'>, b: Pick<Workspace, 'prefix'>): boolean => !!a.prefix && a.prefix.toUpperCase() === b.prefix.toUpperCase()

/** The workspace an address names: its prefix, and `ws` (the last 8 hex of the UUID) when two share the prefix. */
export type Named<W> = { found: W } | { ambiguous: W[] } | { missing: true }
export function workspaceByAddress<W extends Pick<Workspace, 'id' | 'prefix'>>(workspaces: readonly W[], prefix: string, hint?: string | null): Named<W> {
  const same = workspaces.filter((w) => w.prefix.toUpperCase() === prefix.toUpperCase())
  if (same.length === 0) return { missing: true }
  const hinted = hint ? same.filter((w) => uuid8(w.id) === hint.toLowerCase()) : []
  if (hinted.length === 1) return { found: hinted[0] }
  // A hint that names no workspace here is not guessed away: with one candidate it is a link to something that is gone.
  if (same.length === 1) return hint ? { missing: true } : { found: same[0] }
  return { ambiguous: same }
}

/** The address hint for a workspace: its `uuid8` when another workspace shares its prefix, else nothing. */
export const addressHint = (w: Pick<Workspace, 'id' | 'prefix'>, workspaces: readonly Pick<Workspace, 'id' | 'prefix'>[]): string | null =>
  workspaces.some((o) => o.id !== w.id && sharesPrefix(o, w)) ? uuid8(w.id) : null

/**
 * The workspace a ticket key names, by its key prefix (`DEMO-0043` -> DEMO). Undefined when no workspace has that prefix.
 * When two workspaces share the prefix the key alone cannot tell them apart: the one the person is in (`prefer`) wins if
 * it is one of them, so a ticket opened from a page of a workspace is that workspace's own ticket; otherwise the first.
 */
export function workspaceOfTicket<W extends Pick<Workspace, 'prefix'> & { id?: string }>(key: string, workspaces: readonly W[], prefer: string | null | undefined = preferred): W | undefined {
  const dash = key.lastIndexOf('-')
  if (dash <= 0) return undefined
  const prefix = key.slice(0, dash)
  const same = workspaces.filter((w) => w.prefix === prefix)
  return same.find((w) => prefer && w.id === prefer) ?? same[0]
}

let preferred: string | null = null
/** The workspace the person is in (set by the workspace provider): breaks the tie between workspaces that share a prefix. */
export function preferWorkspace(id: string | null) {
  preferred = id
}
