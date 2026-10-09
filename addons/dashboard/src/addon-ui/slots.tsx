// SlotRegistry: reads the addon packages (/api/addons) and hands each surface (nav, today card, ticket panel, board
// lane...) the contributions of the addons active in the workspace, with bindings resolved against the slot context.
import { useQueries, useQuery } from '@tanstack/react-query'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import type { AddonContribution, AddonPackage, AddonSlot, TicketDocument, TicketSummary, Workspace } from '@/api/types'
import { useWorkspace } from '@/app/workspace'
import { getPath, resolveBindings } from './bindings'

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
}

export interface AddonStateWait {
  status: 'pending' | 'error'
  retry: () => void
}

const ADDON_BINDING = /"\$ref":"addon[.]|\$\{addon[.]/

/** Does this contribution read the addon's own state (a `{"$ref":"addon..."}`, `${addon...}` or a `when` on it)? */
export function bindsAddonState(c: Pick<AddonContribution, 'node' | 'when'>): boolean {
  return !!c.when?.startsWith('addon.') || ADDON_BINDING.test(JSON.stringify(c.node ?? null))
}

/** Pure selection (used by the hook and by tests). Only addons active in `ctx.workspace` contribute. */
export function selectContributions(addons: AddonPackage[], slot: AddonSlot, ctx: SlotContext = {}, waiting?: AddonStateWait): ResolvedContribution[] {
  const out: ResolvedContribution[] = []
  if (!ctx.workspace) return out // deny by default: no workspace, no per-workspace enablement to check
  for (const a of addons) {
    if (!addonActive(ctx.workspace, a.name)) continue
    for (const c of a.contributions as AddonContribution[]) {
      if (c.slot !== slot) continue
      const base = { addon: a.name, addonTitle: a.title, slot, id: c.id, title: c.title, icon: c.icon }
      // Its state is not here yet (or failed): neither `when` nor the bindings can be judged, so core waits.
      if (waiting && bindsAddonState(c)) {
        out.push({ ...base, node: null, waiting })
        continue
      }
      if (c.when && (getPath(ctx, c.when) ?? null) === null) continue
      out.push({ ...base, node: resolveBindings(c.node, ctx) })
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
  return useQuery({ queryKey: ['addons'], queryFn: api.getAddons, staleTime: 30_000 })
}

/**
 * Contributions for a slot in the current workspace. Pass the ticket in `ctx` for ticket-bound slots: the addon
 * state is then asked for that ticket only (`?ticket=`), which keeps per-ticket payloads small.
 */
export function useSlot(name: AddonSlot, ctx: Omit<SlotContext, 'workspace' | 'addon'> = {}): ResolvedContribution[] {
  const { data = [] } = useAddons()
  const { workspace } = useWorkspace()
  const names = data.filter((a) => addonActive(workspace, a.name) && a.contributions.some((c) => c.slot === name)).map((a) => a.name)
  const states = useAddonStateEntries(workspace?.id, names, ctx.ticket?.key)
  const out: ResolvedContribution[] = []
  for (const a of data) {
    const st = states[a.name]
    out.push(...selectContributions([a], name, { ...ctx, workspace, addon: st?.data }, st?.waiting))
  }
  return out
}

/** Query key of an addon's state; per-ticket requests sit under the addon's key, so invalidating it covers them. */
export function addonStateKey(ws: string | undefined, name: string, ticket?: string): unknown[] {
  return ticket ? ['addon-state', ws, name, { ticket }] : ['addon-state', ws, name]
}

/** Fetches the per-workspace state of each named addon, with its loading/error marker (see `AddonStateWait`). */
export function useAddonStateEntries(ws: string | undefined, names: string[], ticket?: string): Record<string, AddonStateEntry> {
  const results = useQueries({
    queries: names.map((name) => ({
      queryKey: addonStateKey(ws, name, ticket),
      queryFn: () => api.getAddonState(ws as string, name, ticket),
      enabled: !!ws,
      retry: false,
    })),
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
