// What this build and this host can do, from `GET /api/session` (design 7, "Feature gating"). The mock transport lists
// everything (`null`), so the mockup and its tests are unchanged. A production page asks `hasFeature` before it draws a
// control: hiding is a courtesy, the server and the host still refuse what is not allowed.

let enabled: ReadonlySet<string> | null = null
let perWorkspace: Readonly<Record<string, readonly string[]>> = {}

export interface SessionFeatures {
  features: readonly string[]
  workspaces?: Readonly<Record<string, readonly string[]>>
}

/** Production start-up (and tests): the features the server lists. `null` lists everything. */
export function setFeatures(session: SessionFeatures | null) {
  enabled = session ? new Set(session.features) : null
  perWorkspace = session?.workspaces ?? {}
}

/** Is `name` on? With a workspace id, the features of that workspace's host (a host that is too old lacks some). */
export function hasFeature(name: string, workspace?: string): boolean {
  if (enabled === null) return true
  if (workspace && perWorkspace[workspace]) return perWorkspace[workspace].includes(name)
  return enabled.has(name)
}

/** True on a real host: not the in-process mock. */
export const isRealHost = () => enabled !== null

/**
 * Anything that signs or changes data. The server lists "actions" once it serves hand-off signing (M3); until then every
 * production page is read only, and controls that would sign or mutate are not drawn.
 */
export const canAct = (workspace?: string) => hasFeature('actions', workspace)
