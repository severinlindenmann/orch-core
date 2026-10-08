// Capability gate for node types that reach beyond pure rendering (today: the terminal node needs `pty`).
import { grantCovers } from '@/api/addons'
import type { AddonPackage, WorkspaceAddon } from '@/api/types'

/**
 * May this addon render a terminal node in this workspace? The addon must declare `pty`, and its state in THIS
 * workspace must be enabled with a grant that covers the installed version and package hash and includes `pty`.
 * Deny by default (unknown package, not installed here, no or stale grant).
 */
export function canUsePty(pkg: AddonPackage | undefined, installed: WorkspaceAddon | undefined): boolean {
  if (!pkg || !installed || !installed.enabled || !installed.capabilities.includes('pty')) return false
  return grantCovers(installed, installed.granted) && installed.granted.capabilities.includes('pty')
}
