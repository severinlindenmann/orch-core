// Pure addon-state rules shared by the mock and the UI (part of the API contract).
import type { ActionMeta, AddonGrant, AddonStatus, AddonUpdate, InstalledAddon, Workspace, WorkspaceAddon } from './types'

/** Does this grant cover exactly the installed package: same version and hash, and every installed capability? */
export function grantCovers(a: Pick<WorkspaceAddon, 'version' | 'package_sha256' | 'capabilities'>, g: AddonGrant | null): g is AddonGrant {
  return !!g && g.version === a.version && g.package_sha256 === a.package_sha256 && a.capabilities.every((c) => g.capabilities.includes(c))
}

/** needs_grant unless the grant covers the installed version, package and capabilities; otherwise active or disabled. */
export function addonStatus(a: Pick<WorkspaceAddon, 'version' | 'package_sha256' | 'capabilities' | 'granted' | 'enabled'>): AddonStatus {
  if (!grantCovers(a, a.granted)) return 'needs_grant'
  return a.enabled ? 'active' : 'disabled'
}

/** Is this addon live in the workspace? Enabled and granted for its installed package. */
export function addonActive(workspace: Pick<Workspace, 'addons'> | undefined, name: string): boolean {
  const a = workspace?.addons[name]
  return !!a && a.enabled && a.status !== 'needs_grant'
}

/** The package's newer version, unless this workspace already runs it. */
export function pendingUpdate(a: Pick<InstalledAddon, 'update' | 'ws'>): AddonUpdate | null {
  return a.update && a.update.version !== a.ws.version ? a.update : null
}

/** Equal as sets: order and duplicates do not matter. */
export const sameSet = (a: string[], b: string[]) => a.every((x) => b.includes(x)) && b.every((x) => a.includes(x))

/** The actions a package lets viewers run (manifest `actions` with minRole 'viewer'), with their display names. Signed with the grant. */
export function viewerActions(pkg: { actions?: Record<string, ActionMeta> }): { id: string; label: string }[] {
  return Object.entries(pkg.actions ?? {})
    .filter(([, m]) => m.minRole === 'viewer')
    .map(([id, m]) => ({ id, label: m.label ?? id }))
}
