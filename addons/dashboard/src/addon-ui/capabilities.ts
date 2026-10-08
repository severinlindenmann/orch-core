// Capability gate for node types that reach beyond pure rendering (today: the terminal node needs `pty`).
import type { AddonManifest } from '@/api/types'

/**
 * May this addon render a terminal node? It must declare `pty`, be enabled, and the owner's grant for the
 * current version must include `pty`. Deny by default (undefined manifest, no grant, grant for another version).
 */
export function canUsePty(manifest: AddonManifest | undefined): boolean {
  if (!manifest || !manifest.enabled || !manifest.capabilities.includes('pty')) return false
  const g = manifest.granted
  return !!g && g.version === manifest.version && g.capabilities.includes('pty')
}
