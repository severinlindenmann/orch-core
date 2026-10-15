// Agent grant rules shared by the Agents page and the shell (part of the API contract).
import type { GrantInfo, Role, Workspace } from './types'
import { atLeast } from './permissions'
import { fmtClock, fmtDateTime, nowMs } from '@/lib/time'

/** The longest grant an owner or maintainer signs. */
export const GRANT_MAX_HOURS = 24
/** The workspace default grant length when the workspace sets none: also the longest grant a member signs. */
export const DEFAULT_GRANT_HOURS = 8

/** The workspace's default grant length (hours). */
export const grantDefaultHours = (ws: Pick<Workspace, 'grant_hours'> | undefined): number => {
  const h = ws?.grant_hours
  // Clamped to a whole number of hours from 1 to the longest grant: a bad setting never widens a member's grant.
  return typeof h === 'number' && Number.isFinite(h) ? Math.min(GRANT_MAX_HOURS, Math.max(1, Math.floor(h))) : DEFAULT_GRANT_HOURS
}

/**
 * What a person may sign for themselves (owner decision 2026-10-10, item 5). Owners and maintainers: all tickets in the
 * workspace, up to 24 h. Members: the tickets they may work on (core checks each claim: the ticket is visible to them
 * and a member may act on it), up to the workspace default. Viewers: nothing (null). The host applies the same rule.
 */
export function grantTerms(role: Role | undefined, ws: Pick<Workspace, 'grant_hours'> | undefined): { scope: 'all' | 'workable'; maxHours: number; defaultHours: number } | null {
  if (!atLeast(role, 'member')) return null
  const def = grantDefaultHours(ws)
  return atLeast(role, 'maintainer') ? { scope: 'all', maxHours: GRANT_MAX_HOURS, defaultHours: def } : { scope: 'workable', maxHours: def, defaultHours: def }
}

/** The scope as the signing dialog says it, for the person who signs. */
export const scopeCover = (scope: GrantInfo['scope']): string =>
  scope === 'all' ? 'all tickets in this workspace' : scope === 'workable' ? 'the tickets you may work on in this workspace' : 'CI only'

export type GrantState = 'active' | 'expired' | 'revoked'

export const grantState = (g: GrantInfo, now: number): GrantState => (g.revoked ? 'revoked' : Date.parse(g.until) <= now ? 'expired' : 'active')

/** The person's active (unrevoked, unexpired) ticket grant (`all` or `workable`) in a workspace; the one that lasts longest if several. */
export function activeGrantOf(grants: GrantInfo[], person: string | undefined, now: number): GrantInfo | undefined {
  return grants
    .filter((g) => g.person === person && (g.scope === 'all' || g.scope === 'workable') && grantState(g, now) === 'active')
    .sort((a, b) => b.until.localeCompare(a.until))[0]
}

const SCOPE_WORDS: Record<GrantInfo['scope'], string> = { all: 'all tickets', workable: 'tickets they may work on', ci: 'CI only' }

/**
 * A grant as people name it: "Mara's grant (all tickets · until 17:00)". The id (gr_…) is for Details only.
 * `name` is the person's display name; a grant that ends on another day says the date ("until 10 Oct 09:00").
 */
export function grantLabel(g: Pick<GrantInfo, 'scope' | 'until'>, name: string, now: number = nowMs()): string {
  const sameDay = new Date(now).toISOString().slice(0, 10) === g.until.slice(0, 10)
  return `${name}'s grant (${SCOPE_WORDS[g.scope] ?? g.scope} · until ${sameDay ? fmtClock(g.until) : fmtDateTime(g.until)})`
}
