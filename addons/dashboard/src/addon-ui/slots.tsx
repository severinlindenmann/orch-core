// SlotRegistry: reads /api/addons and hands each surface (nav, today card, ticket panel, board lane...) the
// contributions of enabled addons, with bindings resolved against the slot context.
import { useQueries, useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { AddonContribution, AddonManifest, AddonSlot, TicketDocument, TicketSummary, Workspace } from '@/api/types'
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
}

/** Pure selection (used by the hook and by tests). `workspace` filters by per-workspace enablement. */
export function selectContributions(addons: AddonManifest[], slot: AddonSlot, ctx: SlotContext = {}): ResolvedContribution[] {
  const out: ResolvedContribution[] = []
  for (const a of addons) {
    if (!a.enabled) continue
    if (ctx.workspace && !ctx.workspace.addons[a.name]?.enabled) continue
    for (const c of a.contributions as AddonContribution[]) {
      if (c.slot !== slot) continue
      if (c.when && (getPath(ctx, c.when) ?? null) === null) continue
      out.push({ addon: a.name, addonTitle: a.title, slot, id: c.id, title: c.title, icon: c.icon, node: resolveBindings(c.node, ctx) })
    }
  }
  return out
}

export function useAddons() {
  return useQuery({ queryKey: ['addons'], queryFn: api.getAddons, staleTime: 30_000 })
}

/** Contributions for a slot in the current workspace. Pass the ticket in `ctx` for ticket-bound slots. */
export function useSlot(name: AddonSlot, ctx: Omit<SlotContext, 'workspace' | 'addon'> = {}): ResolvedContribution[] {
  const { data = [] } = useAddons()
  const { workspace } = useWorkspace()
  const states = useAddonStates(workspace?.id, data.filter((a) => a.enabled && a.contributions.some((c) => c.slot === name)).map((a) => a.name))
  const out: ResolvedContribution[] = []
  for (const a of data) {
    out.push(...selectContributions([a], name, { ...ctx, workspace, addon: states[a.name] }))
  }
  return out
}

/** Fetches the per-workspace state of each named addon (query key ['addon-state', ws, name]). */
export function useAddonStates(ws: string | undefined, names: string[]): Record<string, Record<string, unknown> | undefined> {
  const results = useQueries({
    queries: names.map((name) => ({
      queryKey: ['addon-state', ws, name],
      queryFn: () => api.getAddonState(ws as string, name),
      enabled: !!ws,
      retry: false,
    })),
  })
  return Object.fromEntries(names.map((n, i) => [n, results[i]?.data]))
}
