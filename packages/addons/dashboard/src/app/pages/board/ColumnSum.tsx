import { useMemo } from 'react'
import { selectContributions, useAddons } from '@/addon-ui'
import { useAddonStates } from '@/addon-ui/slots'
import { addonActive } from '@/api/addons'
import { estimateOn, notEstimatedText, pointsText, rollup } from '@/api/estimate'
import type { TicketSummary } from '@/api/types'
import { useWorkspace } from '@/app/workspace'

export interface ColumnSumItem {
  addon: string
  id: string
  title: string
  unit: string
  total: number
}

/** "13 pts", "1 pt". */
export const withUnit = (total: number, unit: string) => `${total} ${unit === 'pt' && total !== 1 ? 'pts' : unit}`

const num = (v: unknown) => (v === null || v === undefined || v === '' || !Number.isFinite(Number(v)) ? null : Number(v))

/**
 * Core feature: a column sums a board.card_field value when it is numeric. A stat node may carry a separate numeric
 * `sum` (a t-shirt size shows "M" and adds 3); otherwise its `value` is used. Only addons active here take part.
 */
export function useColumnSums(tickets: TicketSummary[]): ColumnSumItem[] {
  const { workspace } = useWorkspace()
  const { data: addons = [] } = useAddons()
  const names = addons.filter((a) => addonActive(workspace, a.name) && a.contributions.some((c) => c.slot === 'board.card_field')).map((a) => a.name)
  const states = useAddonStates(workspace?.id, names)
  return useMemo(() => {
    const totals = new Map<string, ColumnSumItem>()
    for (const a of addons) {
      if (!names.includes(a.name)) continue
      for (const t of tickets) {
        const data = t.addons?.[a.name]
        if (!data || Object.keys(data).length === 0) continue
        for (const c of selectContributions([a], 'board.card_field', { ticket: t, workspace, addon: states[a.name] })) {
          const node = c.node as { type?: string; label?: string; value?: unknown; sum?: unknown }
          if (node?.type !== 'stat') continue
          const n = num(node.sum ?? node.value)
          if (n === null) continue
          const k = `${c.addon}/${c.id}`
          const cur = totals.get(k) ?? { addon: c.addon, id: c.id, title: c.title, unit: node.label ?? '', total: 0 }
          cur.total += n
          totals.set(k, cur)
        }
      }
    }
    return [...totals.values()]
  }, [addons, names.join(','), tickets, workspace, states]) // eslint-disable-line react-hooks/exhaustive-deps
}

/**
 * Estimate on a real host: the sum of the cards' points and, in words, how many cards have none ("n not estimated"),
 * so a total never reads as complete when it is not. `showMissing` (an epic's lane) also shows a lane whose children
 * have no points at all.
 */
function EstimateSum({ tickets, showMissing }: { tickets: TicketSummary[]; showMissing: boolean }) {
  const r = rollup(tickets)
  if (r.estimated === 0 && !(showMissing && r.notEstimated > 0)) return null
  const missing = r.notEstimated > 0 ? notEstimatedText(r.notEstimated) : ''
  return (
    <span aria-label={`Sum of points${missing ? `, ${missing}` : ''}`} title={[pointsText(r.total), missing].filter(Boolean).join(' · ')} className="inline-flex items-center gap-1 font-mono text-[11px] text-text-muted" data-testid="estimate-sum">
      <span className="whitespace-nowrap">{r.total} {r.total === 1 ? 'pt' : 'pts'}</span>
      {missing && <span className="font-sans text-text-faint" data-testid="estimate-missing"> · {missing}</span>}
    </span>
  )
}

export function ColumnSums({ tickets, showMissing = false }: { tickets: TicketSummary[]; showMissing?: boolean }) {
  const sums = useColumnSums(tickets)
  const { workspace } = useWorkspace()
  return (
    <>
      {estimateOn(workspace) && <EstimateSum tickets={tickets} showMissing={showMissing} />}
      {sums.map((s) => (
        <span key={`${s.addon}/${s.id}`} aria-label={`Sum of ${s.title}`} title={withUnit(s.total, s.unit)} className="inline-flex items-center gap-1 shrink-0 whitespace-nowrap font-mono text-[11px] text-text-muted">
          {s.total}
          <span className="inline"> {s.unit === 'pt' && s.total !== 1 ? 'pts' : s.unit}</span>
        </span>
      ))}
    </>
  )
}
