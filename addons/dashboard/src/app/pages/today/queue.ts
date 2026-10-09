import { countAttention } from '@/api/attention'
// Today's attention queue as pure data: which group an item belongs to, its stable order, folding of repeated addon
// asks, and the buffer that keeps new arrivals out of the list until the person asks for them.
import type { AddonDecision, NeedsYouItem } from '@/api/types'

export type GroupId = 'questions' | 'approvals' | 'verdicts' | 'addons'

export const GROUPS: { id: GroupId; label: string }[] = [
  { id: 'questions', label: 'Questions' },
  { id: 'approvals', label: 'Approvals' },
  { id: 'verdicts', label: 'Verdicts' },
  { id: 'addons', label: 'From addons' },
]

export type CoreEntry = { id: string; group: Exclude<GroupId, 'addons'>; blocking: boolean; since: string; item: NeedsYouItem }
export type DecisionEntry = { id: string; group: 'addons'; blocking: false; since?: undefined; decision: AddonDecision }
export type Entry = CoreEntry | DecisionEntry

export type Row = { kind: 'one'; id: string; entry: Entry } | { kind: 'fold'; id: string; addon: string; action: string; entries: DecisionEntry[] }

export interface Group {
  id: GroupId
  label: string
  /** Items in the group (a fold counts each of its decisions). */
  count: number
  /** The oldest `since` in the group; addon decisions carry none. */
  oldest?: string
  rows: Row[]
}

/** Fewest open decisions with the same addon and action that fold into one row. */
export const FOLD_MIN = 3

export const itemId = (i: NeedsYouItem) => (i.kind === 'verdict' ? `verdict:${i.ticket}` : `${i.kind}:${i.ticket}:${i.ref}`)
export const decisionId = (d: AddonDecision) => `addon:${d.id}`

// Kinds Today has no row for (handoff, expiring) are left out, as before.
const GROUP_OF: Partial<Record<NeedsYouItem['kind'], CoreEntry['group']>> = { question: 'questions', approval: 'approvals', verdict: 'verdicts' }

export function toEntries(items: NeedsYouItem[], decisions: AddonDecision[]): Entry[] {
  return [
    ...items.flatMap((item): CoreEntry[] => {
      const group = GROUP_OF[item.kind]
      return group ? [{ id: itemId(item), group, blocking: !!item.blocking, since: item.since, item }] : []
    }),
    ...decisions.map((decision): DecisionEntry => ({ id: decisionId(decision), group: 'addons', blocking: false, decision })),
  ]
}

/** Most recent first, then blocking and id for ties. Live arrivals keep their place until accepted. */
export function compareEntries(a: Entry, b: Entry): number {
  const sa = a.since ? Date.parse(a.since) : Infinity
  const sb = b.since ? Date.parse(b.since) : Infinity
  if (sa !== sb) return sa > sb ? -1 : 1
  if (a.blocking !== b.blocking) return a.blocking ? -1 : 1
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0
}

export const sortEntries = (entries: Entry[]) => [...entries].sort(compareEntries)

/** The order the person sees until the next load or "Show new": every current entry, sorted. */
export const acceptOrder = (entries: Entry[], fresh: ReadonlySet<string> = new Set()) => sortEntries(entries).sort((a, b) => Number(fresh.has(b.id)) - Number(fresh.has(a.id))).map((e) => e.id)

/**
 * Splits the current entries into what is on screen (accepted ids, in the accepted order, resolved ones gone)
 * and what arrived since (`fresh`, shown only as "N new").
 */
export function reconcile(order: string[], entries: Entry[]): { shown: Entry[]; fresh: Entry[] } {
  const byId = new Map(entries.map((e) => [e.id, e]))
  const accepted = new Set(order)
  return {
    shown: order.flatMap((id) => byId.get(id) ?? []),
    fresh: sortEntries(entries.filter((e) => !accepted.has(e.id))),
  }
}

/**
 * Drops ids that are no longer open, so an item resolved and later reopened arrives as new (behind "N new") instead of
 * silently in its old place. Returns the same array when nothing is dropped.
 */
export function pruneOrder(order: string[], entries: Entry[]): string[] {
  const open = new Set(entries.map((e) => e.id))
  return order.every((id) => open.has(id)) ? order : order.filter((id) => open.has(id))
}

/** Groups in fixed order, empty ones left out. Entries keep the order they come in. */
export function buildGroups(entries: Entry[]): Group[] {
  return GROUPS.flatMap(({ id, label }) => {
    const members = entries.filter((e) => e.group === id)
    if (members.length === 0) return []
    const oldest = members.reduce<string | undefined>((o, e) => (e.since && (!o || Date.parse(e.since) < Date.parse(o)) ? e.since : o), undefined)
    return [{ id, label, count: countAttention(members.flatMap(e => e.group === 'addons' ? [] : [e.item]), members.flatMap(e => e.group === 'addons' ? [e.decision] : []))[id], oldest, rows: id === 'addons' ? foldRows(members as DecisionEntry[]) : members.map((e): Row => ({ kind: 'one', id: e.id, entry: e })) }]
  })
}

function foldRows(entries: DecisionEntry[]): Row[] {
  const key = (e: DecisionEntry) => `${e.decision.addon}/${e.decision.action}`
  const sizes = new Map<string, number>()
  for (const e of entries) sizes.set(key(e), (sizes.get(key(e)) ?? 0) + 1)
  const rows: Row[] = []
  const folds = new Map<string, Extract<Row, { kind: 'fold' }>>()
  for (const e of entries) {
    const k = key(e)
    if ((sizes.get(k) ?? 0) < FOLD_MIN) {
      rows.push({ kind: 'one', id: e.id, entry: e })
      continue
    }
    let f = folds.get(k)
    if (!f) {
      f = { kind: 'fold', id: `fold:${k}`, addon: e.decision.addon, action: e.decision.action, entries: [] }
      folds.set(k, f)
      rows.push(f)
    }
    f.entries.push(e)
  }
  return rows
}

/** "5 × Quick tasks: outgrew its limit": the count, the addon, and the words every title ends with. */
export function foldLabel(n: number, addonTitle: string, titles: string[]): string {
  const words = titles.map((t) => t.trim().split(/\s+/))
  const common: string[] = []
  for (let i = 1; words.every((w) => w.length >= i); i++) {
    const w = words[0][words[0].length - i]
    if (!words.every((x) => x[x.length - i] === w)) break
    common.unshift(w)
  }
  const shared = common.join(' ')
  if (!shared) return `${n} × ${addonTitle}`
  if (shared.toLowerCase().startsWith(addonTitle.toLowerCase())) return `${n} × ${shared}`
  return `${n} × ${addonTitle}: ${shared}`
}

/** How many items the rows hold (a fold counts each of its decisions). */
export const itemsIn = (rows: Row[]) => rows.reduce((n, r) => n + (r.kind === 'fold' ? r.entries.length : 1), 0)
