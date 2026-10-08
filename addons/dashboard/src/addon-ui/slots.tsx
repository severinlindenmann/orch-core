// SlotRegistry: reads /api/addons and hands each surface (nav, today card, ticket panel, board lane...) the
// contributions of enabled addons, with bindings resolved against the slot context.
import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { AddonContribution, AddonManifest, AddonSlot, TicketDocument, TicketSummary, Workspace } from '@/api/types'
import { useWorkspace } from '@/app/workspace'
import { getPath, resolveBindings } from './bindings'

export interface SlotContext {
  ticket?: TicketDocument | TicketSummary
  workspace?: Workspace
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
export function useSlot(name: AddonSlot, ctx: Omit<SlotContext, 'workspace'> = {}): ResolvedContribution[] {
  const { data = [] } = useAddons()
  const { workspace } = useWorkspace()
  return selectContributions(data, name, { ...ctx, workspace })
}
