// The Board in a narrow page area (N11): columns narrow down to BOARD_COL_MIN, then more of them become rails (the X3
// collapsed rails) until the board fits its width, so it never scrolls sideways. The person's own collapsed columns
// stay rails; a column they open from an automatic rail stays open and another one rails instead.

/** The narrowest an open column gets (px). */
export const BOARD_COL_MIN = 168
/** A rail's width and the gap between columns (px). */
export const BOARD_RAIL = 40
export const BOARD_GAP = 8

export interface BoardColumn {
  id: string
  /** No cards in it. */
  empty: boolean
}

/** The width the columns take with `railed` as rails. */
export function boardWidth(columns: readonly BoardColumn[], railed: ReadonlySet<string>): number {
  return columns.reduce((n, c) => n + (railed.has(c.id) ? BOARD_RAIL : BOARD_COL_MIN), 0) + Math.max(0, columns.length - 1) * BOARD_GAP
}

/**
 * The columns shown as rails at `width`, in column order. `collapsed` are the person's rails; `order` is which columns
 * rail first when room runs out (empty columns go before the rest either way); `opened` are columns the person opened
 * from an automatic rail, most recent last: they rail only after everything else, the most recent one last of all.
 * One column always stays open. `width` 0 (not measured) adds no rails.
 */
export function railedColumns(columns: readonly BoardColumn[], width: number, opts: { collapsed: readonly string[]; order: readonly string[]; opened?: readonly string[] }): string[] {
  const railed = new Set(columns.filter((c) => opts.collapsed.includes(c.id)).map((c) => c.id))
  if (width > 0) {
    const opened = opts.opened ?? []
    const rank = (c: BoardColumn) => {
      const o = opened.indexOf(c.id)
      if (o >= 0) return 2000 + o
      const i = opts.order.indexOf(c.id)
      return (c.empty ? 0 : 1000) + (i < 0 ? 999 : i)
    }
    const candidates = columns.filter((c) => !railed.has(c.id)).sort((a, b) => rank(a) - rank(b))
    // Keep the last candidate open whatever happens.
    for (const c of candidates.slice(0, -1)) {
      if (boardWidth(columns, railed) <= width) break
      railed.add(c.id)
    }
  }
  return columns.filter((c) => railed.has(c.id)).map((c) => c.id)
}
