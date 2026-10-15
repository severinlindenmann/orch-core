// Versioned localStorage persistence for the mock store. Anything that is not a v2 payload is discarded.
import type { BodySections, OrchEvent, TicketDefinition, WorkspaceEvent } from '@/api/types'

export const STORAGE_KEY = 'orch-mock-v2'
/** Earlier storage keys; removed on load so stale v1 state never lingers. */
const OLD_KEYS = ['orch-mock', 'orch.dashboard.mock.v1']

export interface PersistedV2 {
  v: 2
  ticketEvents: Record<string, OrchEvent[]> // appended after the seed, per ticket key
  created: Record<string, { ws: string; def: TicketDefinition; body: BodySections }>
  wsEvents: Record<string, WorkspaceEvent[]> // appended after the seed, per workspace id
  addonState: Record<string, Record<string, unknown>> // `${ws}/${addon}` -> state
  /** The `stateVersion` each addon's saved state was written with (addon name -> version). Absent in older storage = 1. */
  addonVersions?: Record<string, number>
  viewer?: string
  /** The demo dataset ('busy' = the generated busy day). Absent in older storage = 'normal'. */
  dataset?: 'normal' | 'busy'
}

/** null when absent, unparsable, or not v2. */
export function loadPersisted(): PersistedV2 | null {
  try {
    const ls = globalThis.localStorage
    if (!ls) return null
    for (const k of OLD_KEYS) ls.removeItem(k)
    const raw = ls.getItem(STORAGE_KEY)
    if (!raw) return null
    const p = JSON.parse(raw) as Partial<PersistedV2> | null
    if (!p || p.v !== 2) return null
    return {
      v: 2,
      ticketEvents: p.ticketEvents ?? {},
      created: p.created ?? {},
      wsEvents: p.wsEvents ?? {},
      addonState: p.addonState ?? {},
      addonVersions: p.addonVersions ?? {},
      viewer: p.viewer,
      dataset: p.dataset === 'busy' ? 'busy' : 'normal',
    }
  } catch {
    return null
  }
}

/** Swallows storage errors (the viewer sandbox may block storage). */
export function savePersisted(p: PersistedV2): void {
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(p))
  } catch {
    /* ignore */
  }
}

export function clearPersisted(): void {
  try {
    globalThis.localStorage?.removeItem(STORAGE_KEY)
  } catch {
    /* ignore */
  }
}
