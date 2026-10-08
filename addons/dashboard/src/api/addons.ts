// Pure addon-state rules shared by the mock and the UI (part of the API contract).
import type { AddonGrant, AddonStatus, Workspace } from './types'

/** needs_grant whenever there is no grant for the installed version; otherwise active or disabled. */
export function addonStatus(version: string, granted: AddonGrant | null, enabled: boolean): AddonStatus {
  if (granted?.version !== version) return 'needs_grant'
  return enabled ? 'active' : 'disabled'
}

/** Is this addon live in the workspace? Enabled and granted for its installed version. */
export function addonActive(workspace: Pick<Workspace, 'addons'> | undefined, name: string): boolean {
  const a = workspace?.addons[name]
  return !!a && a.enabled && a.status !== 'needs_grant'
}
