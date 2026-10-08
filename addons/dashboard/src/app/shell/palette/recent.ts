export interface RecentItem {
  path: string
  label: string
  kind: 'ticket' | 'page'
}

const MAX = 8
const storageKey = (person: string) => `orch.dashboard.recent.${person}`

const PAGES: Record<string, string> = {
  '/': 'Today',
  '/board': 'Board',
  '/tickets': 'Tickets',
  '/tickets/new': 'New ticket',
  '/agents': 'Agents',
  '/settings': 'Settings',
}

/** The recent-list entry for a location, or null for pages that are not worth returning to. */
export function describePath(pathname: string): RecentItem | null {
  const path = pathname.length > 1 ? pathname.replace(/\/$/, '') : pathname
  const ticket = /^\/ticket\/([^/]+)$/.exec(path)
  if (ticket) return { path, label: decodeURIComponent(ticket[1]), kind: 'ticket' }
  const addon = /^\/addon\/([^/]+)\/([^/]+)$/.exec(path)
  if (addon) return { path, label: `${addon[1]} / ${addon[2]}`, kind: 'page' }
  return PAGES[path] ? { path, label: PAGES[path], kind: 'page' } : null
}

export function loadRecent(person: string): RecentItem[] {
  try {
    const raw = JSON.parse(localStorage.getItem(storageKey(person)) ?? '[]') as unknown
    return Array.isArray(raw) ? (raw as RecentItem[]).filter((r) => typeof r?.path === 'string' && typeof r.label === 'string').slice(0, MAX) : []
  } catch {
    return []
  }
}

/** Puts `item` first (no duplicates, at most 8) and stores the list for this viewer. */
export function recordRecent(person: string, item: RecentItem): RecentItem[] {
  const next = [item, ...loadRecent(person).filter((r) => r.path !== item.path)].slice(0, MAX)
  try {
    localStorage.setItem(storageKey(person), JSON.stringify(next))
  } catch {
    /* storage unavailable: the list lasts for this session only */
  }
  return next
}
