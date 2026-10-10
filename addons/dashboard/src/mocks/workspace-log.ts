// Pure reducers: workspace state and grants are derived from seed + workspace events.
import { addonStatus } from '@/api/addons'
import type { CodeReviewApplies, GateName, GrantInfo, Role, SavedView, ViewParams, Workspace, WorkspaceEvent } from '@/api/types'

export function foldWorkspace(seed: Workspace, events: WorkspaceEvent[]): Workspace {
  const ws: Workspace = structuredClone(seed)
  for (const e of events) {
    switch (e.type) {
      case 'member.added': {
        const person = String(e.person)
        if (!ws.members.some((m) => m.person === person))
          ws.members.push({ person, name: typeof e.name === 'string' ? e.name : person, role: e.role as Role, devices: 0, last_seen: null })
        break
      }
      case 'member.role_changed': {
        const m = ws.members.find((x) => x.person === e.person)
        if (m) m.role = e.role as Role
        break
      }
      case 'member.removed':
        ws.members = ws.members.filter((m) => m.person !== e.person)
        break
      case 'gate.policy_set': {
        const gate = e.gate as GateName
        const { approvers, count, applies } = e as { approvers?: string; count?: number; not?: string; applies?: CodeReviewApplies }
        const prev = ws.gates[gate]
        if (prev)
          ws.gates[gate] = {
            approvers: approvers ?? prev.approvers,
            count: count ?? prev.count,
            ...('not' in e ? (e.not ? { not: String(e.not) } : {}) : prev.not ? { not: prev.not } : {}),
            // The code review gate also says where it applies; it never admits an assignee.
            ...(gate === 'code' ? { not: 'assignees', applies: applies ?? prev.applies ?? 'off' } : {}),
          }
        break
      }
      case 'addon.installed': {
        const name = String(e.name)
        ws.addons[name] ??= {
          enabled: false,
          status: 'needs_grant',
          installed: true,
          granted: null,
          version: String(e.version ?? ''),
          package_sha256: String(e.package_sha256 ?? ''),
          capabilities: Array.isArray(e.capabilities) ? (e.capabilities as string[]) : [],
        }
        break
      }
      case 'addon.granted': {
        const a = ws.addons[String(e.name)]
        if (!a) break
        a.granted = {
          version: String(e.version),
          capabilities: Array.isArray(e.capabilities) ? (e.capabilities as string[]) : [],
          package_sha256: String(e.package_sha256),
          at: e.at,
          by: e.actor.id,
        }
        a.status = addonStatus(a)
        break
      }
      case 'addon.updated': {
        const a = ws.addons[String(e.name)]
        if (!a) break
        a.version = String(e.version)
        a.package_sha256 = String(e.package_sha256 ?? '')
        a.capabilities = Array.isArray(e.capabilities) ? (e.capabilities as string[]) : []
        a.status = addonStatus(a) // a new version needs a new grant
        break
      }
      case 'addon.enabled':
      case 'addon.disabled': {
        const a = ws.addons[String(e.name)]
        if (!a) break
        a.enabled = e.type === 'addon.enabled'
        a.status = addonStatus(a)
        break
      }
      case 'addon.uninstalled':
        delete ws.addons[String(e.name)]
        break
      case 'workspace.renamed':
        ws.name = String(e.name)
        break
      // Relay & devices (simulated): the link switch and each member's device count.
      case 'relay.connected':
      case 'relay.stopped':
        ws.relay = e.type === 'relay.connected' ? 'on' : 'off'
        break
      case 'device.paired':
      case 'device.removed': {
        const m = ws.members.find((x) => x.person === e.person)
        if (m) m.devices = Math.max(0, (m.devices ?? 0) + (e.type === 'device.paired' ? 1 : -1))
        break
      }
      case 'epoch.rotated':
        break
      // Recorded in the log but not part of Workspace state (later tasks read them directly).
      case 'addon.settings_saved':
      case 'grant.issued':
      case 'grant.revoked':
      case 'agent.started':
      case 'agent.stopped':
      case 'view.saved':
      case 'view.deleted':
      case 'ticket.discarded':
        break
      default:
        break // unknown types are ignored
    }
  }
  return ws
}

export function foldGrants(seed: GrantInfo[], events: WorkspaceEvent[]): GrantInfo[] {
  const grants = structuredClone(seed)
  for (const e of events) {
    if (e.type === 'grant.issued') {
      const id = String(e.grant)
      if (grants.some((g) => g.id === id)) continue
      grants.push({
        id,
        person: String(e.person),
        scope: (e.scope as GrantInfo['scope']) ?? 'all',
        issued_at: e.at,
        until: String(e.until),
        revoked: null,
        sessions: Array.isArray(e.sessions) ? (e.sessions as string[]) : [],
      })
    } else if (e.type === 'grant.revoked') {
      const g = grants.find((x) => x.id === e.grant)
      if (g && !g.revoked) g.revoked = { at: e.at, by: e.actor.id }
    } else if (e.type === 'agent.started') {
      // A session started under a grant is registered on it, so revoking the grant stops it.
      const g = grants.find((x) => x.id === e.grant)
      const session = String(e.session)
      if (g && !g.sessions.includes(session)) g.sessions.push(session)
    }
  }
  return grants
}

/** Saved views: the seeds folded with view.saved / view.deleted events. */
export function foldViews(seed: SavedView[], events: WorkspaceEvent[]): SavedView[] {
  let views = structuredClone(seed)
  for (const e of events) {
    if (e.type === 'view.saved') {
      views.push({ id: String(e.view), name: String(e.name), owner: e.actor.id, shared: e.shared === true, params: (e.params ?? {}) as ViewParams })
    } else if (e.type === 'view.deleted') {
      views = views.filter((v) => v.id !== e.view)
    }
  }
  return views
}
