// Agent grant rules shared by the Agents page and the shell (part of the API contract).
import type { GrantInfo } from './types'

export type GrantState = 'active' | 'expired' | 'revoked'

export const grantState = (g: GrantInfo, now: number): GrantState => (g.revoked ? 'revoked' : Date.parse(g.until) <= now ? 'expired' : 'active')

/** The person's active (unrevoked, unexpired) `all` grant in a workspace; the one that lasts longest if several. */
export function activeGrantOf(grants: GrantInfo[], person: string | undefined, now: number): GrantInfo | undefined {
  return grants
    .filter((g) => g.person === person && g.scope === 'all' && grantState(g, now) === 'active')
    .sort((a, b) => b.until.localeCompare(a.until))[0]
}
