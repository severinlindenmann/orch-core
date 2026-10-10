// Which table columns stay and which fold into the row's second line, by the table's own width (N11: every page fits
// a 13" notebook with the terminal docked on the right). Core's rule for every table it draws, addon tables included:
// the first two columns and the actions always stay; a column with `hideBelow` folds below that width; the others stay
// left to right while each still gets FOLD_COLUMN_MIN px, and fold from the right after that; if that is not
// enough, columns with a `hideBelow` that still show fold too (from the right).

/** The width a column is assumed to need when it says nothing (px). */
export const FOLD_COLUMN_MIN = 100
/** The width kept for a row's actions (one button and a "More" menu, px). */
export const FOLD_ACTIONS = 120

export interface FoldColumn {
  key: string
  /** Fold this column into the second line when the table is narrower than this (px). */
  hideBelow?: number
  /** Never folds (besides the first two, which never do). */
  keep?: boolean
}

/**
 * The keys that fold, in column order. `width` is the table's width; 0 (not measured yet, or no layout as in jsdom)
 * folds nothing.
 */
export function foldedColumns(columns: readonly FoldColumn[], width: number, opts: { actions?: boolean; columnMin?: number; extra?: number } = {}): string[] {
  if (!(width > 0)) return []
  const min = opts.columnMin ?? FOLD_COLUMN_MIN
  const folded = new Set<string>()
  // Declared thresholds first.
  columns.forEach((c, i) => {
    if (i >= 2 && !c.keep && c.hideBelow !== undefined && width < c.hideBelow) folded.add(c.key)
  })
  // Then the budget: how many columns fit at `min` each beside the actions.
  // `extra`: columns to fold beyond the budget, when the table measured wider than its box anyway (long unbreakable cells).
  const fits = Math.max(2, Math.floor((width - (opts.actions ? FOLD_ACTIONS : 0)) / min) - (opts.extra ?? 0))
  let shown = columns.filter((c) => !folded.has(c.key)).length
  // From the right: columns without a threshold first, then (still too many) the ones with one.
  for (const declared of [false, true]) {
    for (let i = columns.length - 1; i >= 2 && shown > fits; i--) {
      const c = columns[i]
      if (c.keep || folded.has(c.key) || (c.hideBelow !== undefined) !== declared) continue
      folded.add(c.key)
      shown--
    }
  }
  return columns.filter((c) => folded.has(c.key)).map((c) => c.key)
}
