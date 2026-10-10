// Pure addon-state rules shared by the mock and the UI (part of the API contract).
import type { ActionMeta, AddonGrant, AddonPackage, AddonStatus, AddonUpdate, InstalledAddon, Workspace, WorkspaceAddon } from './types'

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

/**
 * The manifest the workspace actually runs: the update's when the installed version is the update's version,
 * otherwise the package's own. Who may run what (and what the owner signed) is read from here, never from `pkg.actions` directly.
 */
export function manifestFor(pkg: Pick<AddonPackage, 'actions' | 'update'>, installedVersion: string): { actions?: Record<string, ActionMeta> } {
  const u = pkg.update
  return u && u.version === installedVersion && u.actions ? { actions: u.actions } : { actions: pkg.actions }
}

/** A package name: lower case, digits and dashes, starting with a letter, at most 40 characters. */
export const PACKAGE_NAME = /^[a-z][a-z0-9-]{0,39}$/
/** An arg key an addon may send with an action (node args, signed args). */
export const ARG_KEY = /^[A-Za-z][A-Za-z0-9_]{0,31}$/
const HIDDEN_CHAR = new RegExp('[\\p{Cc}\\p{Cf}\\u2028\\u2029]', 'u')

/** At most this many terms on one decision (as for signed args). */
export const MAX_DECISION_TERMS = 12
/**
 * Are these a decision's terms core can show and sign exactly (null/undefined: none)? At most 12, keys like arg keys,
 * values plain strings or finite numbers. Anything else fails closed: core does not offer the decision at all.
 */
export function validTerms(terms: unknown): boolean {
  if (terms === undefined) return true
  if (!terms || typeof terms !== 'object' || Array.isArray(terms)) return false
  const entries = Object.entries(terms as Record<string, unknown>)
  return entries.length > 0 && entries.length <= MAX_DECISION_TERMS && entries.every(([k, v]) => ARG_KEY.test(k) && (typeof v === 'string' || (typeof v === 'number' && Number.isFinite(v))))
}
/** Do the terms a person signed equal the decision's terms now (same keys, same values)? */
export function sameTerms(signed: unknown, now: Record<string, string | number> | undefined): boolean {
  if (!now) return signed === undefined
  if (!signed || typeof signed !== 'object' || Array.isArray(signed)) return false
  const a = Object.entries(signed as Record<string, unknown>)
  return a.length === Object.keys(now).length && a.every(([k, v]) => Object.hasOwn(now, k) && String(v) === String(now[k]) && typeof v === typeof now[k])
}

/**
 * Why core will not install or show this package's name and title in its own lines (null when it can). Core writes
 * the addon as "Title (id)" in titles and covers, so a title must not carry the characters that sentence uses
 * (parentheses, the middle dot, a colon), invisible characters, or more than 40 characters.
 */
export function manifestProblem(pkg: { name: string; title: string }): string | null {
  if (!PACKAGE_NAME.test(pkg.name)) return 'Its package name must be lower case letters, digits and dashes (starting with a letter, at most 40).'
  if (pkg.title.length === 0 || pkg.title.length > 40) return 'Its title must be 1 to 40 characters.'
  if (/[():·]/.test(pkg.title)) return 'Its title must not contain parentheses, a colon or a middle dot.'
  if (HIDDEN_CHAR.test(pkg.title)) return 'Its title contains invisible or control characters.'
  return null
}
