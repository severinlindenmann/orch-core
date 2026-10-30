// What this build and this host can do, from `GET /api/session` (design 7, "Feature gating"). The mock transport lists
// everything (`null`) except optional Focus, which follows its workspace grant. A production page asks `hasFeature` before it draws a
// control: hiding is a courtesy, the server and the host still refuse what is not allowed.

import { isRemoteLogin, REMOTE_HIDDEN_FEATURES } from './remoteMode'

let enabled: ReadonlySet<string> | null = null
let perWorkspace: Readonly<Record<string, readonly string[]>> = {}
let login: string | null = null

export interface SessionFeatures {
  features: readonly string[]
  workspaces?: Readonly<Record<string, readonly string[]>>
  /** How this browser session was opened ("local": a launch code from this computer). Absent or unknown: not known. */
  login?: string
}

/** Production start-up (and tests): the features the server lists. `null` uses demo features (Focus still needs its grant). */
export function setFeatures(session: SessionFeatures | null) {
  enabled = session ? new Set(session.features) : null
  perWorkspace = session?.workspaces ?? {}
  login = session?.login ?? null
}

/**
 * Show "Log out" only for a session that is known to come from a hub or a remote sign-in. A local session (the launch
 * link from this computer) has nothing to sign out of; when the server does not say, it is hidden too.
 */
export const showsLogout = (): boolean => enabled !== null && login !== null && login !== 'local' && login !== 'unknown'

/**
 * The features follow the workspace list: a host serves some of them only while an add-on is ON (Usage, Terminals,
 * Publish ...), so turning one on must show its menu entry at once, without a reload. The mock also refreshes optional Focus grants.
 */
export function refreshFeatures(rows: readonly { id: string; features: readonly string[] }[]) {
  perWorkspace = Object.fromEntries(rows.map((r) => [r.id, r.features]))
  if (enabled === null) return
  enabled = new Set(rows.flatMap((r) => r.features))
}

/** Is `name` on? With a workspace id, the features of that workspace's host (a host that is too old lacks some). */
export function hasFeature(name: string, workspace?: string): boolean {
  if (inRemoteSession() && REMOTE_HIDDEN_FEATURES.has(name)) return false // the workspace would refuse every action
  if (enabled === null) {
    // Focus is optional even in the demo; its workspace grant controls the placeholder.
    if (name === 'focus') {
      return workspace ? !!perWorkspace[workspace]?.includes(name) : Object.values(perWorkspace).some((features) => features.includes(name))
    }
    return true
  }
  if (workspace && perWorkspace[workspace]) return perWorkspace[workspace].includes(name)
  return enabled.has(name)
}

/** Is this page running inside a remote session (reached through the remote client)? See `remoteMode.ts`. */
export const inRemoteSession = (): boolean => enabled !== null && isRemoteLogin(login)

/** Was this session opened on this computer (a launch code)? Remote access is changed only from here. */
export const onThisComputer = (): boolean => login === 'local'

/** True on a real host: not the in-process mock. */
export const isRealHost = () => enabled !== null

/**
 * Anything that signs or changes data. The server lists "actions" once it serves hand-off signing (M3); until then every
 * production page is read only, and controls that would sign or mutate are not drawn.
 */
export const canAct = (workspace?: string) => hasFeature('actions', workspace)

/**
 * Does signing go through the terminal hand-off? True on a real host that serves `human.*` (feature `actions`). The mock
 * keeps its simulated Touch ID dialog, so the mockup and its tests are unchanged.
 */
export const handoffMode = (workspace?: string): boolean => isRealHost() && canAct(workspace)
