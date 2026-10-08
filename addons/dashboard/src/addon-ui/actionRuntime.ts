// Shared by every surface that runs an addon action (AddonNode, the board lane): what core lets an action carry
// in, and what core opens out. One place, so the two cannot drift.

/**
 * The workspace and `ticket` come from core's render context only, and `confirmed` only from core's own confirmation
 * dialog (starting an agent); addon-authored args may never set them.
 */
export function withoutReservedKeys(extra: Record<string, unknown> = {}): Record<string, unknown> {
  const { ws: _ws, ticket: _ticket, confirmed: _confirmed, ...rest } = extra
  return rest
}

/** Opens an action result's `url` in a new tab, but only an https one (no javascript:, data:, http:). */
export function openResultUrl(res: { url?: string }): void {
  if (res.url && /^https:\/\//i.test(res.url)) window.open(res.url, '_blank', 'noopener,noreferrer')
}
