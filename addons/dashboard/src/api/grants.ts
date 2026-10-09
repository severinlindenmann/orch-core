// Agent grant rules shared by the Agents page and the shell (part of the API contract).
import type { GrantInfo } from './types'
import { fmtClock, fmtDateTime, nowMs } from '@/lib/time'

export type GrantState = 'active' | 'expired' | 'revoked'

export const grantState = (g: GrantInfo, now: number): GrantState => (g.revoked ? 'revoked' : Date.parse(g.until) <= now ? 'expired' : 'active')

/** The person's active (unrevoked, unexpired) `all` grant in a workspace; the one that lasts longest if several. */
export function activeGrantOf(grants: GrantInfo[], person: string | undefined, now: number): GrantInfo | undefined {
  return grants
    .filter((g) => g.person === person && g.scope === 'all' && grantState(g, now) === 'active')
    .sort((a, b) => b.until.localeCompare(a.until))[0]
}

const SCOPE_WORDS: Record<GrantInfo['scope'], string> = { all: 'all tickets', ci: 'CI only' }

/**
 * A grant as people name it: "Mara's grant (all tickets · until 17:00)". The id (gr_…) is for Details only.
 * `name` is the person's display name; a grant that ends on another day says the date ("until 10 Oct 09:00").
 */
export function grantLabel(g: Pick<GrantInfo, 'scope' | 'until'>, name: string, now: number = nowMs()): string {
  const sameDay = new Date(now).toISOString().slice(0, 10) === g.until.slice(0, 10)
  return `${name}'s grant (${SCOPE_WORDS[g.scope] ?? g.scope} · until ${sameDay ? fmtClock(g.until) : fmtDateTime(g.until)})`
}
