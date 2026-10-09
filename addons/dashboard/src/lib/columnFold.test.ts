import { describe, expect, it } from 'vitest'
import { FOLD_ACTIONS, foldedColumns } from './columnFold'

const cols = (n: number) => Array.from({ length: n }, (_, i) => ({ key: `c${i}` }))

describe('foldedColumns (N11 column priority rule)', () => {
  it('folds nothing before the table is measured', () => {
    expect(foldedColumns(cols(8), 0)).toEqual([])
  })

  it('folds nothing when every column gets its minimum', () => {
    expect(foldedColumns(cols(6), 600)).toEqual([])
  })

  it('folds from the right once the columns no longer fit', () => {
    // 450 px fits four columns of 100.
    expect(foldedColumns(cols(7), 450)).toEqual(['c4', 'c5', 'c6'])
  })

  it('keeps room for the actions', () => {
    expect(foldedColumns(cols(5), 400 + FOLD_ACTIONS, { actions: true })).toEqual(['c4'])
  })

  it('never folds the first two columns', () => {
    expect(foldedColumns(cols(5), 120)).toEqual(['c2', 'c3', 'c4'])
    expect(foldedColumns([{ key: 'a', hideBelow: 9999 }, { key: 'b', hideBelow: 9999 }, { key: 'c', hideBelow: 9999 }], 500)).toEqual(['c'])
  })

  it('a declared hideBelow folds below its width and is not folded by the budget', () => {
    const c = [{ key: 'a' }, { key: 'b' }, { key: 'note', hideBelow: 800 }, { key: 'd' }, { key: 'e' }]
    expect(foldedColumns(c, 900)).toEqual([])
    expect(foldedColumns(c, 700)).toEqual(['note'])
    // 350 px fits three: 'note' is folded by its threshold, then 'e' by the budget.
    expect(foldedColumns(c, 350)).toEqual(['note', 'e'])
  })

  it('a kept column stays; the next one to its left folds instead', () => {
    const c = [{ key: 'a' }, { key: 'b' }, { key: 'c' }, { key: 'd', keep: true }]
    expect(foldedColumns(c, 300)).toEqual(['c'])
  })
})
