import { manifestFor, viewerActions } from '@/api/addons'
import type { MockStore } from '@/mocks/store'

/** Install, grant and enable a catalog addon in a workspace as its owner (for tests that start from the catalog). */
export function installAndGrant(store: MockStore, ws: string, name: string): void {
  const actor = { kind: 'person', id: 'p_sev' } as const
  const r = store.addonOp(ws, name, { op: 'install' }, actor)
  if (!r.ok) throw new Error(`install ${name}: ${r.message}`)
  const a = store.workspaceAddons(ws).find((x) => x.name === name)!
  const g = store.addonOp(
    ws,
    name,
    { op: 'grant', version: a.ws.version, package_sha256: a.ws.package_sha256, capabilities: a.ws.capabilities, viewer_actions: viewerActions(manifestFor(a, a.ws.version)).map((x) => x.id) },
    actor,
  )
  if (!g.ok) throw new Error(`grant ${name}: ${g.message}`)
  const e = store.addonOp(ws, name, { op: 'enable' }, actor)
  if (!e.ok) throw new Error(`enable ${name}: ${e.message}`)
}
