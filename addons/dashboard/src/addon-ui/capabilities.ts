// Capability gates for what reaches beyond pure rendering: the terminal node needs `pty`, starting an agent `spawn_agent`.
import { grantCovers } from '@/api/addons'
import type { AddonPackage, WorkspaceAddon } from '@/api/types'

/**
 * May this addon render a terminal node in this workspace? The addon must declare `pty`, and its state in THIS
 * workspace must be enabled with a grant that covers the installed version and package hash and includes `pty`.
 * Deny by default (unknown package, not installed here, no or stale grant).
 */
export function canUsePty(pkg: AddonPackage | undefined, installed: WorkspaceAddon | undefined): boolean {
  return holds(pkg, installed, 'pty')
}

/** May this addon ask core to start an agent here? Same rule as `pty`, for `spawn_agent`. */
export function canSpawnAgent(pkg: AddonPackage | undefined, installed: WorkspaceAddon | undefined): boolean {
  return holds(pkg, installed, 'spawn_agent')
}

function holds(pkg: AddonPackage | undefined, installed: WorkspaceAddon | undefined, cap: string): boolean {
  if (!pkg || !installed || !installed.enabled || !installed.capabilities.includes(cap)) return false
  return grantCovers(installed, installed.granted) && installed.granted.capabilities.includes(cap)
}
