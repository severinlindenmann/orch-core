import type { AddonActionResult, AddonDecision } from '@/api/types'
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

export type AddonActionFn = (ctx: AddonCtx) => AddonActionResult

/**
 * An action. Who may run it is NOT decided here: the package manifest declares `actions[id].minRole` (default member)
 * and the store enforces it before this runs.
 */
export type AddonAction = AddonActionFn

export interface MockAddon {
  name: string
  /** Initial state per workspace (seed). */
  seed(ws: string, store: MockStore): Record<string, unknown>
  /** Optional derived fields merged into GET .../state (e.g. counts). */
  view?(state: Record<string, unknown>, ctx: Omit<AddonCtx, 'body' | 'state'>): Record<string, unknown>
  /**
   * Open decisions for this workspace. Default (hook omitted): the package's decisions minus the ids in
   * `state.decided`. Implement it for decisions that appear and disappear with state (e.g. only while a build is failed).
   * `pkg` is the package's declared decisions; never mutate it.
   */
  decisions?(state: Record<string, unknown>, pkg: AddonDecision[]): AddonDecision[]
  actions: Record<string, AddonAction>
}

/**
 * May this viewer see data about ticket `key` in this workspace? A module's view() and its ticket-scoped actions use
 * this for every per-ticket row, map entry, title or branch name (the store cannot tell which strings are ticket keys).
 * A key that is not a ticket of this workspace is never shown, whoever asks.
 */
export function canSeeTicket(c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>, key: string | null | undefined): boolean {
  return !!key && c.store.workspaceOf(key)?.id === c.ws && c.store.isVisible(key, c.viewer)
}

/** Record a decision as made (the default `decisions` filter hides ids listed in `state.decided`). */
export function markDecided(state: Record<string, unknown>, id: string): void {
  const done = (state.decided as string[] | undefined) ?? []
  if (!done.includes(id)) state.decided = [...done, id]
}

/** Open decisions of an addon in a workspace, from its mock module (or the default rule). */
export function openDecisions(addon: MockAddon | undefined, state: Record<string, unknown>, pkg: AddonDecision[]): AddonDecision[] {
  if (addon?.decisions) return addon.decisions(state, pkg)
  const done = (state.decided as string[] | undefined) ?? []
  return pkg.filter((d) => !done.includes(d.id))
}

const registry = new Map<string, MockAddon>()

export function registerAddon(a: MockAddon): void {
  registry.set(a.name, a)
}

export function getAddon(name: string): MockAddon | undefined {
  return registry.get(name)
}
