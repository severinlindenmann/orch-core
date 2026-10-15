import { describe, expect, it } from 'vitest'
import type { TicketSummary } from '@/api/types'
import { COLLAPSE_OVER, groupByEpic, hasEpics, isCollapsed } from './grouping'

const t = (key: string, o: Partial<TicketSummary> = {}): TicketSummary =>
  ({ key, title: key, type: 'feature', status: 'open', parent: null, ...o }) as TicketSummary

const epic = t('E-1', { type: 'epic' })
const kids = Array.from({ length: 10 }, (_, i) => t(`K-${i}`, { parent: 'E-1', status: i < 2 ? 'done' : 'open' }))
const all = [epic, ...kids, t('S-1'), t('S-2', { parent: 'GONE-1' })]

describe('groupByEpic', () => {
  it('puts children under their epic, the rest under no epic, and counts progress from all children', () => {
    expect(hasEpics(all)).toBe(true)
    const g = groupByEpic(all, all, false)
    expect(g.lanes).toHaveLength(1)
    expect(g.lanes[0].children).toHaveLength(10)
    expect(g.lanes[0]).toMatchObject({ total: 10, done: 2, open: 8 })
    // a parent that is not a visible epic does not make a lane
    expect(g.none.map((x) => x.key)).toEqual(['S-1', 'S-2'])
  })
  it('keeps progress over all children while a filter hides some, and drops a lane with no visible child', () => {
    const shown = all.filter((x) => x.key === 'K-5' || x.key === 'S-1')
    const g = groupByEpic(all, shown, true)
    expect(g.lanes[0].children.map((x) => x.key)).toEqual(['K-5'])
    expect(g.lanes[0].total).toBe(10)
    expect(groupByEpic(all, [all.find((x) => x.key === 'S-1')!], true).lanes).toHaveLength(0)
    expect(groupByEpic(all, [], false).lanes).toHaveLength(1)
  })
  it('folds an epic with more than 8 open children by default; an explicit choice wins', () => {
    const lane = groupByEpic(all, all, false).lanes[0]
    expect(lane.open).toBe(COLLAPSE_OVER)
    expect(isCollapsed(lane, {})).toBe(false)
    const big = groupByEpic([...all, t('K-x', { parent: 'E-1' })], [...all, t('K-x', { parent: 'E-1' })], false).lanes[0]
    expect(isCollapsed(big, {})).toBe(true)
    expect(isCollapsed(big, { 'E-1': false })).toBe(false)
    expect(isCollapsed(lane, { 'E-1': true })).toBe(true)
  })

  it('orders lanes by the latest activity of the epic or any child (R-a)', () => {
    const all = [t('E-1', { type: 'epic', updated_at: '2026-10-08' }), t('E-2', { type: 'epic', updated_at: '2026-10-09' }), t('K-1', { parent: 'E-1', updated_at: '2026-10-10' })]
    expect(groupByEpic(all, all, false).lanes.map((l) => l.epic.key)).toEqual(['E-1', 'E-2'])
    expect(groupByEpic(all.slice(0, 2), all.slice(0, 2), false).lanes.map((l) => l.epic.key)).toEqual(['E-2', 'E-1'])
  })
})
