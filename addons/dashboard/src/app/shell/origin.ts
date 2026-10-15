// Where a ticket was opened from (B m3): the ticket page's breadcrumb leads back there, not always to the Board.

export interface PageOrigin {
  /** The page's full address-bar href (path with its workspace, and search), so filters come back too. */
  href: string
  label: string
}

const CORE: Record<string, string> = { '/': 'Today', '/board': 'Board', '/tickets': 'Tickets', '/artifacts': 'Artifacts', '/agents': 'Agents' }

/** The default when a ticket is the first page opened (or was opened from somewhere without a name). */
export const BOARD_ORIGIN: PageOrigin = { href: '/board', label: 'Board' }

/**
 * The origin a page makes for the tickets opened from it, or `undefined` for pages that keep the earlier origin (a
 * ticket page: moving between tickets keeps the way back). Core pages have fixed names; other pages (addons, settings)
 * use their topbar title when it is plain text.
 */
export function originOf(pathname: string, href: string, title: unknown): PageOrigin | null | undefined {
  if (pathname.startsWith('/ticket/')) return undefined
  const label = CORE[pathname] ?? (typeof title === 'string' && title.trim() ? title : null)
  return label ? { href, label } : null
}
