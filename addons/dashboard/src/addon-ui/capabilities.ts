// Capability gate for node types that reach beyond pure rendering (today: the terminal node needs `pty`).
import type { AddonManifest } from '@/api/types'

/** A manifest as the API may annotate it with the owner's current grant (Task 11 adds the real field). */
type GrantedManifest = AddonManifest & { granted?: { capabilities: string[] } }

/**
 * May this addon render a terminal node? It must declare `pty`, be enabled, and hold a current grant.
 * No `granted` field yet: enabled first-party addons count as granted. Deny by default (undefined manifest).
 */
export function canUsePty(manifest: AddonManifest | undefined): boolean {
  if (!manifest || !manifest.enabled || !manifest.capabilities.includes('pty')) return false
  const granted = (manifest as GrantedManifest).granted
  if (granted === undefined) return manifest.first_party
  return granted.capabilities.includes('pty')
}
