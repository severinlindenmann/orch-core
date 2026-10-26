// Used by the one action hook (useRunAddonAction: addon nodes, board lanes, the palette): what core lets an action carry
// in, and what core opens out.

import { openDockOn } from '@/app/terminal/dock/request'

/**
 * The workspace and `ticket` come from core's render context only, and `confirmed` only from core's own confirmation
 * dialog (starting an agent), with the `launch` choice core validated there; addon-authored args may never set them.
 */
export const RESERVED_KEYS: readonly string[] = ['ws', 'ticket', 'confirmed', 'launch']

export function withoutReservedKeys(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return Object.fromEntries(Object.entries(extra).filter(([k]) => !RESERVED_KEYS.includes(k)))
}

/** Opens an action result's `url` in a new tab, but only an https one (no javascript:, data:, http:). */
export function openResultUrl(res: { url?: string; terminal?: string }): void {
  if (res.url && /^https:\/\//i.test(res.url)) window.open(res.url, '_blank', 'noopener,noreferrer')
  // A session the action opened: the dock opens on it (it only ever shows the terminals view's own sessions).
  if (typeof res.terminal === 'string' && res.terminal) openDockOn(res.terminal)
}
