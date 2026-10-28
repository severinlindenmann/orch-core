// Agent grant rules shared by the Agents page and the shell (part of the API contract).
import type { GrantInfo, Role, Workspace } from './types'
import { atLeast } from './permissions'
import { fmtClock, fmtDateTime, nowMs, sameLocalDay } from '@/lib/time'
import { usableGrant } from '@/app/pages/new-ticket/startAfterCreate'

/** The longest grant an owner or maintainer signs. */
export const GRANT_MAX_HOURS = 168
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
 * workspace, up to 168 h (7 days). Members: the tickets they may work on (core checks each claim: the ticket is visible to them
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
  const sameDay = sameLocalDay(now, g.until)
  return `${name}'s grant (${SCOPE_WORDS[g.scope] ?? g.scope} · until ${sameDay ? fmtClock(g.until) : fmtDateTime(g.until)})`
}

/** B8 (owner decision D): the lengths the settings control offers, in hours: 8 h · 1 day · 3 days · 7 days. */
export const ACCESS_LENGTHS: ReadonlyArray<{ hours: number; label: string }> = [
  { hours: 8, label: '8 h' },
  { hours: 24, label: '1 day' },
  { hours: 72, label: '3 days' },
  { hours: 168, label: '7 days' },
]

/** A length as people say it: whole days as "3 days", else "N h". */
export const accessLength = (hours: number): string =>
  hours >= 24 && hours % 24 === 0 ? `${hours / 24} day${hours === 24 ? '' : 's'}` : `${hours} h`

/**
 * The reminder before an agent grant ends: shown when under 24 h remain, or, for a grant shorter than a day, when under 25% of
 * its length remains. Display only: the end time is the signed `until`, and the host enforces it.
 */
export function accessReminder(g: Pick<GrantInfo, 'issued_at' | 'until'>, now: number): boolean {
  const end = Date.parse(g.until)
  const start = Date.parse(g.issued_at)
  if (!Number.isFinite(end) || !Number.isFinite(start) || end <= now) return false
  const total = end - start
  const left = end - now
  return total >= 24 * 3_600_000 ? left < 24 * 3_600_000 : left < total * 0.25
}

/**
 * What the header says about agent access: enough left to start agents (`ok`, 60 min or more), ending soon (15 to 60 min,
 * still startable) or `missing` (none, not startable, or under 15 min left). Display only: the host enforces.
 */
export function accessLine(
  grant: { until: string; startable?: boolean; startable_until?: string | null } | null | undefined,
  now: number,
): { kind: 'ok' } | { kind: 'ending'; until: string } | { kind: 'missing' } {
  if (!grant) return { kind: 'missing' }
  const left = Date.parse(grant.until) - now
  if (!Number.isFinite(left) || left <= 0 || grant.startable === false) return { kind: 'missing' }
  if (grant.startable_until && Date.parse(grant.startable_until) <= now) return { kind: 'missing' }
  if (left < 15 * 60_000) return { kind: 'missing' }
  if (!usableGrant({ grant } as Parameters<typeof usableGrant>[0], now)) return { kind: 'missing' }
  return left < 60 * 60_000 ? { kind: 'ending', until: grant.until } : { kind: 'ok' }
}

/**
 * The lengths a runner grant or an Extend offers (hours): 8 h, 1, 3 and 7 days, capped at the workspace maximum
 * (`grant_hours`). An Extend never shortens the access it replaces, so lengths shorter than `remainingMs` are hidden.
 * A workspace that allows less than 8 h offers its own maximum.
 */
export function runnerLengths(cap: number, remainingMs = 0): Array<readonly [number, string]> {
  const all: Array<readonly [number, string]> = [[8, '8 hours'], [24, '1 day'], [72, '3 days'], [168, '7 days']]
  const within = all.filter(([h]) => h <= cap)
  const offered = within.length > 0 ? within : [[cap, `${cap} ${cap === 1 ? 'hour' : 'hours'}`] as const]
  return offered.filter(([h]) => h * 3_600_000 >= remainingMs)
}

/** D2: reuse only valid access with at least 15 minutes remaining, by host time. */
export function reusableAccess(runner: import('./runs').RunnerInfo | null | undefined) {
  if (!runner?.now || !runner.grant || runner.grant.startable !== true) return null
  return Date.parse(runner.grant.until) - Date.parse(runner.now) >= 900_000 ? runner.grant : null
}
