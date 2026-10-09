import { describe, expect, it } from 'vitest'
import type { AddonDecision, NeedsYouItem } from '@/api/types'
import { acceptOrder, buildGroups, foldLabel, pruneOrder, reconcile, sortEntries, toEntries } from './queue'

const q = (ticket: string, since: string, blocking = false, ref = 'Q1'): NeedsYouItem => ({ kind: 'question', ticket, title: `T ${ticket}`, text: 'Which?', since, ref, blocking })
const ap = (ticket: string, since: string): NeedsYouItem => ({ kind: 'approval', ticket, title: `T ${ticket}`, text: 'Approve the plan.', since, ref: 'plan' })
const ve = (ticket: string, since: string): NeedsYouItem => ({ kind: 'verdict', ticket, title: `T ${ticket}`, text: 'Verdict needed.', since, ref: 'verify' })
const dec = (id: string, addon = 'quick', action = 'decide', title = `${id} outgrew its limit`): AddonDecision => ({
  kind: 'decision', id, addon, action, title, question: `${title}?`, options: [{ key: 'a', label: 'A' }],
})

describe('today queue: grouping and order', () => {
  it('orders newest `since` first, then blocking, then the id, whatever the input order', () => {
    const items = [q('D-3', '2026-10-09T09:00:00Z'), q('D-1', '2026-10-09T10:00:00Z', true), q('D-2', '2026-10-08T09:00:00Z'), q('D-4', '2026-10-08T09:00:00Z')]
    const a = sortEntries(toEntries(items, [])).map((e) => e.id)
    const b = sortEntries(toEntries([...items].reverse(), [])).map((e) => e.id)
    expect(a).toEqual(['question:D-1:Q1', 'question:D-3:Q1', 'question:D-2:Q1', 'question:D-4:Q1'])
    expect(b).toEqual(a) // `since` decides, never the position in the list
  })

  it('puts entries in four fixed groups, hides empty ones and names the oldest age', () => {
    const entries = sortEntries(toEntries([ve('D-9', '2026-10-09T08:00:00Z'), q('D-1', '2026-10-09T10:00:00Z'), q('D-2', '2026-10-07T10:00:00Z')], [dec('x1')]))
    const groups = buildGroups(entries)
    expect(groups.map((g) => g.id)).toEqual(['questions', 'verdicts', 'addons'])
    expect(groups[0]).toMatchObject({ label: 'Questions', count: 2, oldest: '2026-10-07T10:00:00Z' })
    expect(groups[2].oldest).toBeUndefined()
  })

  it('folds three or more open decisions with the same addon and action into one row, at the first one\'s place', () => {
    const ds = [dec('a1', 'publish', 'decide', 'Publish report'), dec('q1'), dec('q2'), dec('q3'), dec('m1', 'models', 'escalate', 'Model routing'), dec('m2', 'models', 'escalate', 'Model routing')]
    const [g] = buildGroups(sortEntries(toEntries([], ds)))
    expect(g.count).toBe(6)
    expect(g.rows.map((r) => (r.kind === 'fold' ? `fold:${r.entries.length}` : r.entry.id))).toEqual(['addon:a1', 'addon:m1', 'addon:m2', 'fold:3'])
  })

  it('names a fold by what its titles share', () => {
    expect(foldLabel(5, 'Quick tasks', ['Q-004 outgrew its limit', 'Q-007 outgrew its limit'])).toBe('5 × Quick tasks: outgrew its limit')
    expect(foldLabel(5, 'AI Factory', ['AI Factory permit', 'AI Factory permit'])).toBe('5 × AI Factory permit')
    expect(foldLabel(3, 'Schedules', ['Weekly cost', 'Daily build'])).toBe('3 × Schedules')
  })
})

describe('today queue: new items are buffered', () => {
  const first = toEntries([q('D-1', '2026-10-09T10:00:00Z', true), ap('D-2', '2026-10-08T10:00:00Z')], [])

  it('shows only accepted items, in the accepted order; new ones wait in `fresh`', () => {
    const order = acceptOrder(first)
    const later = toEntries([q('D-0', '2026-10-01T10:00:00Z', true), ...[q('D-1', '2026-10-09T10:00:00Z', true), ap('D-2', '2026-10-08T10:00:00Z')]], [dec('n1')])
    const { shown, fresh } = reconcile(order, later)
    expect(shown.map((e) => e.id)).toEqual(['question:D-1:Q1', 'approval:D-2:plan'])
    expect(fresh.map((e) => e.id).sort()).toEqual(['addon:n1', 'question:D-0:Q1'])
  })

  it('drops resolved items at once', () => {
    const { shown, fresh } = reconcile(acceptOrder(first), first.slice(1))
    expect(shown.map((e) => e.id)).toEqual(['approval:D-2:plan'])
    expect(fresh).toEqual([])
  })

  it('accepting re-sorts everything once', () => {
    const all = toEntries([ap('D-2', '2026-10-08T10:00:00Z'), q('D-0', '2026-10-01T10:00:00Z', true), q('D-1', '2026-10-09T10:00:00Z', true)], [])
    expect(acceptOrder(all)).toEqual(['question:D-1:Q1', 'approval:D-2:plan', 'question:D-0:Q1'])
  })

  it('an item resolved and later reopened comes back as new, not in its old place', () => {
    const order = acceptOrder(first)
    const pruned = pruneOrder(order, first.slice(1)) // D-1 resolved
    expect(pruned).toEqual(['approval:D-2:plan'])
    expect(pruneOrder(pruned, first.slice(1))).toBe(pruned) // nothing to drop: same array, no re-render
    const { shown, fresh } = reconcile(pruned, first) // D-1 reopened
    expect(shown.map((e) => e.id)).toEqual(['approval:D-2:plan'])
    expect(fresh.map((e) => e.id)).toEqual(['question:D-1:Q1'])
  })
})
