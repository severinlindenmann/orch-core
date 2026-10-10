// SlotRegistry: reads the addon packages (/api/addons) and hands each surface (nav, today card, ticket panel, board
// lane...) the contributions of the addons active in the workspace, with bindings resolved against the slot context.
import { useQueries, useQuery } from '@tanstack/react-query'
import { addonActive } from '@/api/addons'
import type { AddonContribution, AddonPackage, AddonSlot, TicketDocument, TicketSummary, Workspace } from '@/api/types'
import { useWorkspace } from '@/app/workspace'
import { getPath, nodeBudgetProblem, resolveBindings } from './bindings'
import { queries } from '@/api/queries'

export interface SlotContext {
  ticket?: TicketDocument | TicketSummary
  workspace?: Workspace
  /** This addon's state in the current workspace (bindings read ${addon.shares.length} etc.). */
  addon?: Record<string, unknown>
}

export interface ResolvedContribution {
  addon: string
  addonTitle: string
  slot: AddonSlot
  id: string
  title: string
  icon?: string
  /** Declarative node with bindings resolved. Still untrusted: render it with <AddonNode/>. */
  node: unknown
  /**
   * Set when the contribution reads this addon's state (`addon.*` in its node or `when`) and that state is not loaded:
   * core draws a skeleton ('pending') or a calm error with Retry ('error') instead of the node.
   */
  waiting?: AddonStateWait
  /** The contribution has a `when` that held: it shows by its own condition, not by the ticket carrying addon data. */
  guarded?: boolean
}

export interface AddonStateWait {
  status: 'pending' | 'error'
  retry: () => void
}

const ADDON_BINDING = /"\$ref":"addon[.]|\$\{addon[.]/

/** Does this contribution read the addon's own state (a `{"$ref":"addon..."}`, `${addon...}` or a `when` on it)? */
export function bindsAddonState(c: Pick<AddonContribution, 'node' | 'when'>): boolean {
  if (c.when?.startsWith('addon.')) return true
  // A node over budget is never walked (not even stringified): it is drawn as "could not be shown", needing no state.
  return nodeBudgetProblem(c.node ?? null) === null && ADDON_BINDING.test(JSON.stringify(c.node ?? null))
}

/** What core draws for a contribution it will not walk: no node type, so the renderer shows "could not be shown". */
const OVER_BUDGET = Object.freeze({ type: null, reason: 'over budget' })

/** Pure selection (used by the hook and by tests). Only addons active in `ctx.workspace` contribute. */
export function selectContributions(addons: AddonPackage[], slot: AddonSlot, ctx: SlotContext = {}, waiting?: AddonStateWait): ResolvedContribution[] {
  const out: ResolvedContribution[] = []
  if (!ctx.workspace) return out // deny by default: no workspace, no per-workspace enablement to check
  for (const a of addons) {
    if (!addonActive(ctx.workspace, a.name)) continue
    for (const c of a.contributions as AddonContribution[]) {
      if (c.slot !== slot) continue
      const base = { addon: a.name, addonTitle: a.title, slot, id: c.id, title: c.title, icon: c.icon }
      // Bounded before anything walks it (security review #9): only this contribution fails.
      if (nodeBudgetProblem(c.node ?? null) !== null) {
        out.push({ ...base, node: OVER_BUDGET })
        continue
      }
      // Its state is not here yet (or failed): neither `when` nor the bindings can be judged, so core waits.
      if (waiting && bindsAddonState(c)) {
        out.push({ ...base, node: null, waiting })
        continue
      }
      if (c.when && (getPath(ctx, c.when) ?? null) === null) continue
      out.push({ ...base, node: resolveBindings(c.node, ctx), ...(c.when ? { guarded: true } : {}) })
    }
  }
  return out
}

/** The state of one addon as a slot needs it: the data, and the wait marker while it is not loaded. */
export interface AddonStateEntry {
  data: Record<string, unknown> | undefined
  waiting?: AddonStateWait
}

export function useAddons() {
  return useQuery(queries.addons())
}

/**
 * Contributions for a slot in the current workspace. Pass the ticket in `ctx` for ticket-bound slots.
 * State is fetched only for addons whose contributions here read it (`addon.*`). A `ticket.panel` asks for that
 * ticket only (`?ticket=`, small per-ticket payloads); every other slot (board card fields on each card, lanes,
 * Today) shares the addon's one state query, so a board of 150 cards makes one request per addon, not one per card.
 */
export function useSlot(name: AddonSlot, ctx: Omit<SlotContext, 'workspace' | 'addon'> = {}): ResolvedContribution[] {
  const { data = [] } = useAddons()
  const { workspace } = useWorkspace()
  const names = data
    .filter((a) => addonActive(workspace, a.name) && (a.contributions as AddonContribution[]).some((c) => c.slot === name && bindsAddonState(c)))
    .map((a) => a.name)
  const states = useAddonStateEntries(workspace?.id, names, name === 'ticket.panel' ? ctx.ticket?.key : undefined)
  const out: ResolvedContribution[] = []
  for (const a of data) {
    const st = states[a.name]
    out.push(...selectContributions([a], name, { ...ctx, workspace, addon: st?.data }, st?.waiting))
  }
  return out
}

export { addonStateKey } from '@/api/queries'

/** Fetches the per-workspace state of each named addon, with its loading/error marker (see `AddonStateWait`). */
export function useAddonStateEntries(ws: string | undefined, names: string[], ticket?: string): Record<string, AddonStateEntry> {
  const results = useQueries({
    // Many surfaces read the same state (sidebar, lanes, every card): a newly mounted one reuses it (staleTime in the
    // factory). Changes arrive by invalidation (actions, the live cursor), not by refetch-on-mount.
    queries: names.map((name) => ({ ...queries.addonState(ws as string, name, ticket), enabled: !!ws })),
  })
  return Object.fromEntries(
    names.map((n, i) => {
      const r = results[i]
      const waiting: AddonStateWait | undefined = r?.isSuccess ? undefined : { status: r?.isError ? 'error' : 'pending', retry: () => void r?.refetch() }
      return [n, { data: r?.data, waiting }]
    }),
  )
}

/** Fetches the per-workspace state of each named addon (query key ['addon-state', ws, name]). */
export function useAddonStates(ws: string | undefined, names: string[]): Record<string, Record<string, unknown> | undefined> {
  const entries = useAddonStateEntries(ws, names)
  return Object.fromEntries(names.map((n) => [n, entries[n]?.data]))
}
