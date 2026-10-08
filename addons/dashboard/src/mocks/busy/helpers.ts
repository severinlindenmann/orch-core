// Helpers for the busy-day addon seeds (`seedBusy` of a mock addon module): the generated tickets of a workspace, as
// the addons want to see them. Every addon still filters what it shows with `canSeeTicket`, so hiding keeps working.
import type { Status } from '@/api/types'
import type { MockStore } from '../store'

export interface Brief {
  key: string
  title: string
  status: Status
  type: string
  restricted: boolean
  /** Repository (the package name, e.g. acme-energy-dbt), pull request numbers on it, and the branch. */
  repo: string
  prs: number[]
  branch: string
  updated_at: string
  tasksDone: number
  claimed: boolean
}

/** Generated tickets (number 100 and up) of a workspace, in key order. */
export function briefs(store: MockStore, ws: string): Brief[] {
  const out: Brief[] = []
  for (const key of store.ticketKeys(ws).sort()) {
    if (Number(key.slice(key.lastIndexOf('-') + 1)) < 100) continue
    const t = store.ticket(key)
    if (!t) continue
    const repo = t.links.repos[0] ?? ''
    out.push({
      key,
      title: t.title,
      status: t.status,
      type: t.type,
      restricted: t.restricted,
      repo,
      prs: t.links.prs.map((p) => Number(p.url.split('/').pop())),
      branch: t.links.branches[repo] ?? `feat/${key}`,
      updated_at: t.updated_at,
      tasksDone: t.tasks_state.filter((x) => x.state === 'done').length,
      claimed: !!t.claim,
    })
  }
  return out
}

export const isDemo = (store: MockStore, ws: string) => store.workspaces.find((w) => w.id === ws)?.prefix === 'DEMO'
/** How much of the full busy seed a workspace gets: DEMO all of it, the others a third. */
export const scaleOf = (store: MockStore, ws: string) => (isDemo(store, ws) ? 1 : 0.3)
export const scaled = (n: number, scale: number) => Math.max(1, Math.round(n * scale))

/** A deterministic short token (mock only). */
export const tokenOf = (n: number, salt = 7): string => {
  const chars = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
  let x = (n + 1) * 2654435761 + salt
  let out = ''
  for (let i = 0; i < 10; i++) {
    x = (x * 1103515245 + 12345) % 2147483648
    out += chars[x % chars.length]
  }
  return out
}

export const dayIso = (daysAgo: number, hour = 9): string => new Date(Date.UTC(2026, 9, 9 - daysAgo, hour, 0)).toISOString().replace(/\.\d{3}Z$/, 'Z')
