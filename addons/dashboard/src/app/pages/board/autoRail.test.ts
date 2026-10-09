import { describe, expect, it } from 'vitest'
import { BOARD_COL_MIN, BOARD_GAP, BOARD_RAIL, boardWidth, railedColumns } from './autoRail'

const cols = ['backlog', 'open', 'in-progress', 'waiting', 'testing', 'done', 'gh/issues'].map((id) => ({ id, empty: false }))
const order = ['gh/issues', 'done', 'backlog', 'testing', 'waiting', 'open', 'in-progress']

describe('board auto rails (N11)', () => {
  it('measures columns and rails', () => {
    expect(boardWidth(cols.slice(0, 2), new Set(['open']))).toBe(BOARD_COL_MIN + BOARD_RAIL + BOARD_GAP)
  })

  it('adds no rails before the board is measured or when everything fits', () => {
    expect(railedColumns(cols, 0, { collapsed: ['done'], order })).toEqual(['done'])
    expect(railedColumns(cols, 2000, { collapsed: ['done'], order })).toEqual(['done'])
  })

  it('rails in order until the columns fit', () => {
    // 672 px: three open columns (3 * 168) + four rails (4 * 40) + six gaps = 712 > 672, so only two stay open.
    const r = railedColumns(cols, 672, { collapsed: ['done'], order })
    expect(r).toEqual(['backlog', 'waiting', 'testing', 'done', 'gh/issues'])
    expect(boardWidth(cols, new Set(r))).toBeLessThanOrEqual(672)
  })

  it('rails empty columns first', () => {
    const withEmpty = cols.map((c) => (c.id === 'in-progress' ? { ...c, empty: true } : c))
    expect(railedColumns(withEmpty, 900, { collapsed: ['done'], order })).toEqual(['in-progress', 'done', 'gh/issues'])
  })

  it('a column the person opened stays open; others rail instead', () => {
    const r = railedColumns(cols, 672, { collapsed: [], order, opened: ['backlog'] })
    expect(r).not.toContain('backlog')
    expect(boardWidth(cols, new Set(r))).toBeLessThanOrEqual(672)
  })

  it('always keeps one column open', () => {
    expect(railedColumns(cols, 100, { collapsed: [], order })).toEqual(cols.filter((c) => c.id !== 'in-progress').map((c) => c.id))
  })
})
