import type { AddonActionResult } from '@/api/types'
import type { MockStore } from '../store'

export interface AddonCtx {
  store: MockStore
  ws: string // workspace id
  viewer: string
  ticket?: string
  body: Record<string, unknown>
  /** This addon's state in this workspace (mutable; saved after the action). */
  state: Record<string, unknown>
}

export interface MockAddon {
  name: string
  /** Initial state per workspace (seed). */
  seed(ws: string, store: MockStore): Record<string, unknown>
  /** Optional derived fields merged into GET .../state (e.g. counts). */
  view?(state: Record<string, unknown>, ctx: Omit<AddonCtx, 'body' | 'state'>): Record<string, unknown>
  actions: Record<string, (ctx: AddonCtx) => AddonActionResult>
}

const registry = new Map<string, MockAddon>()

export function registerAddon(a: MockAddon): void {
  registry.set(a.name, a)
}

export function getAddon(name: string): MockAddon | undefined {
  return registry.get(name)
}
