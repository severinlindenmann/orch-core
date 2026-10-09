// The catalog's core types beyond the first four (bars, table, checks, kv live in parse.ts). Names and shapes follow
// orch.widgets.v1 (plugins/orch-core/docs/widgets.md, "Core types"), which the Python renderer shares: stats, gates,
// series, spark, diff, callout. Two more are proposals of this mockup and not in v1 yet: timeline and progress
// (PROPOSED_TYPES; the gallery marks them). Each is a strict check over a block that strictJson already parsed into
// prototype-free objects: unknown keys are refused at every level, every field is read as an own key, and a check
// returns a one-line reason or undefined. Nothing here draws, executes or fetches.
import { has, isObj, unknownKey, type Obj } from '@/api/strictObject'
import { MAX_LABEL, MAX_ROWS } from './limits'

export { isObj }

export interface CoreType {
  keys: string[]
  /** A one-line reason, or undefined when the block is valid. */
  check: (v: Obj) => string | undefined
}

/** Types this mockup draws that orch.widgets.v1 does not define yet (shown as "Proposed" in the gallery). */
export const PROPOSED_TYPES: readonly string[] = ['timeline', 'progress']

const shortStr = (v: unknown, max: number) => typeof v === 'string' && v.length <= max
const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const oneOf = (v: unknown, list: readonly string[]) => typeof v === 'string' && list.includes(v)

/**
 * Numbers a chart can place: |v| ≤ 1e15. Within that bound the axis math (spread, ticks, scale) is always finite, so
 * nothing else is refused: equal values draw as a flat line, a tiny spread draws with fine ticks. (The Python schema
 * bounds chart numbers at ±1e12, so every v1-valid block passes.)
 */
export const MAX_MAGNITUDE = 1e15
function plottable(values: number[], what: string): string | undefined {
  if (values.some((v) => Math.abs(v) > MAX_MAGNITUDE)) return `${what} must be between -1e15 and 1e15`
}

/** A list of item objects: min..max entries, each an object with only `allowed` keys, then `each` per item. */
function items(v: unknown, opts: { name: string; min?: number; max: number; allowed: readonly string[]; where: string; each: (o: Obj) => string | undefined; size: string }): string | undefined {
  const min = opts.min ?? 1
  if (!Array.isArray(v) || v.length < min || v.length > opts.max) return `${opts.name} needs ${opts.size}`
  for (const o of v) {
    if (!isObj(o)) return `each entry of ${opts.name} must be an object`
    const r = unknownKey(o, opts.allowed, opts.where) ?? opts.each(o)
    if (r) return r
  }
}

const unitOk = (v: Obj, key = 'unit') => !has(v, key) || shortStr(v[key], 20)
const labelReq = (o: Obj, key: string, what: string) => (shortStr(o[key], MAX_LABEL) && (o[key] as string).length > 0 ? undefined : `${what} must be 1 to ${MAX_LABEL} characters`)
const optText = (o: Obj, key: string, max: number) => (has(o, key) && !shortStr(o[key], max) ? `${key} must be a string of at most ${max} characters` : undefined)

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})(T([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?Z?)?$/
/** YYYY-MM-DD[THH:MM[:SS]][Z] naming a day that exists (no 2026-02-31). */
export function validDate(s: unknown): boolean {
  if (typeof s !== 'string') return false
  const m = DATE_RE.exec(s)
  if (!m) return false
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])]
  const t = new Date(Date.UTC(y, mo - 1, d))
  return t.getUTCFullYear() === y && t.getUTCMonth() === mo - 1 && t.getUTCDate() === d
}

/** widgets.md role tokens; `note` is accepted as another name for `info`. */
export const ROLES = ['ok', 'info', 'warn', 'err', 'neu'] as const
export type Role = (typeof ROLES)[number]
export const roleOf = (r: unknown): Role => (r === 'note' ? 'info' : (r as Role))
const roleOk = (r: unknown) => r === 'note' || oneOf(r, ROLES)

/** gates statuses; `ok` is accepted as another name for `pass`. */
export const GATE_STATES = ['pass', 'fail', 'skip', 'running'] as const
export const gateOf = (s: unknown) => (s === 'ok' ? 'pass' : (s as (typeof GATE_STATES)[number]))
export const SEGMENT_STATES = ['ok', 'warn', 'err', 'neu'] as const
export const STEP_STATES = ['done', 'current', 'next', 'failed'] as const
export const MAX_SERIES = 4
export const MAX_DIFF_LINES = 200

/** One shape for both series forms: widgets.md's `points`, or this mockup's shared `x` plus named `series`. */
export interface SeriesData {
  x: (number | string)[]
  numeric: boolean
  lines: { name: string; values: (number | null)[] }[]
  markers: { x: number | string; label: string }[]
}
export function seriesData(f: Obj): SeriesData {
  const markers = ((f.markers as Obj[] | undefined) ?? []).map((m) => ({ x: m.x as number | string, label: String(m.label) }))
  if (Array.isArray(f.points)) {
    const pts = f.points as [number, number][]
    return { x: pts.map((p) => p[0]), numeric: true, lines: [{ name: typeof f.unit === 'string' ? f.unit : 'value', values: pts.map((p) => p[1]) }], markers }
  }
  const x = f.x as (number | string)[]
  return { x, numeric: x.every((v) => typeof v === 'number'), lines: f.series as SeriesData['lines'], markers }
}

function checkSeries(v: Obj): string | undefined {
  if (!unitOk(v)) return 'unit must be a short string'
  const pointsForm = has(v, 'points')
  if (pointsForm === has(v, 'series')) return 'series needs exactly one of points or x with series'
  let xs: (number | string)[]
  const ys: number[] = []
  if (pointsForm) {
    if (has(v, 'x')) return 'x goes with series, not with points'
    const p = v.points
    if (!Array.isArray(p) || p.length < 2 || p.length > MAX_ROWS) return `points needs 2 to ${MAX_ROWS} [x, y] pairs`
    if (!p.every((q) => Array.isArray(q) && q.length === 2 && num(q[0]) && num(q[1]))) return 'each point must be [x, y] with two numbers'
    xs = p.map((q) => q[0] as number)
    ys.push(...p.map((q) => q[1] as number))
  } else {
    const x = v.x
    if (!Array.isArray(x) || x.length < 2 || x.length > MAX_ROWS) return `x needs 2 to ${MAX_ROWS} values`
    if (!x.every(num) && !x.every((s) => shortStr(s, MAX_LABEL))) return 'x must be all numbers or all labels'
    xs = x as (number | string)[]
    const names = new Set<string>()
    const r = items(v.series, {
      name: 'series', max: MAX_SERIES, size: `1 to ${MAX_SERIES} lines`, allowed: ['name', 'values'], where: 'a series',
      each(o) {
        const l = labelReq(o, 'name', 'a series name')
        if (l) return l
        const n = o.name as string
        if (names.has(n)) return `series name "${n}" is used twice`
        names.add(n)
        const vals = o.values
        if (!Array.isArray(vals)) return `values of "${n}" must be a list`
        if (vals.length !== x.length) return `series "${n}" has ${vals.length} values, expected ${x.length}`
        if (!vals.every((y) => y === null || num(y))) return `each value of "${n}" must be a number or null`
        if (!vals.some(num)) return `series "${n}" needs at least one number`
        ys.push(...vals.filter(num))
      },
    })
    if (r) return r
  }
  const numericX = xs.every(num)
  const p = plottable(ys, 'values') ?? (numericX ? plottable(xs as number[], 'x values') : undefined)
  if (p) return p
  if (has(v, 'markers'))
    return items(v.markers, {
      name: 'markers', min: 0, max: 20, size: 'at most 20 markers', allowed: ['x', 'label'], where: 'a marker',
      each(o) {
        // On a numeric axis any finite x is accepted (as in the v1 schema); one outside the data range is not drawn.
        const ok = numericX ? num(o.x) : xs.some((xv) => xv === o.x)
        if (!ok) return numericX ? 'a marker x must be a number' : `marker at "${String(o.x)}" is not one of the x labels`
        return labelReq(o, 'label', 'a marker label')
      },
    })
}

export const MORE_CORE: Record<string, CoreType> = {
  stats: {
    keys: ['items'],
    check(v) {
      return items(v.items, {
        name: 'items', max: 12, size: '1 to 12 numbers', allowed: ['label', 'value', 'delta', 'role'], where: 'a stat',
        each(o) {
          const r = labelReq(o, 'label', 'a stat label')
          if (r) return r
          if (!num(o.value) && !shortStr(o.value, 200)) return 'value must be a number or a string of at most 200 characters'
          if (has(o, 'delta') && !num(o.delta) && !shortStr(o.delta, 200)) return 'delta must be a number or a string of at most 200 characters'
          if (has(o, 'role') && !roleOk(o.role)) return 'role must be ok, info, warn, err or neu'
        },
      })
    },
  },
  series: { keys: ['unit', 'points', 'x', 'series', 'markers'], check: checkSeries },
  spark: {
    keys: ['values', 'text'],
    check(v) {
      if (!Array.isArray(v.values) || v.values.length < 2 || v.values.length > MAX_ROWS || !v.values.every(num)) return `values needs 2 to ${MAX_ROWS} numbers`
      const p = plottable(v.values as number[], 'values')
      if (p) return p
      if (typeof v.text !== 'string' || v.text.length > 500) return 'text must be a string of at most 500 characters'
      if (v.text.split('{spark}').length !== 2) return 'text must contain {spark} exactly once'
    },
  },
  gates: {
    keys: ['items'],
    check(v) {
      return items(v.items, {
        name: 'items', max: MAX_ROWS, size: 'at least one gate', allowed: ['name', 'status', 'seconds'], where: 'a gate',
        each(o) {
          const r = labelReq(o, 'name', 'a gate name')
          if (r) return r
          if (o.status !== 'ok' && !oneOf(o.status, GATE_STATES)) return 'status must be pass, fail, skip or running'
          if (has(o, 'seconds') && !(num(o.seconds) && o.seconds >= 0 && o.seconds <= 1e12)) return 'seconds must be a number from 0 to 1e12'
        },
      })
    },
  },
  diff: {
    keys: ['file', 'lines'],
    check(v) {
      if (typeof v.file !== 'string' || v.file.length === 0) return 'diff needs file (the path the lines come from)'
      if (v.file.length > 500) return 'file is longer than 500 characters'
      if (typeof v.lines !== 'string' || v.lines.trim() === '') return 'lines must be a non-empty unified diff'
      if (v.lines.split('\n').length > MAX_DIFF_LINES) return `a diff shows at most ${MAX_DIFF_LINES} lines`
    },
  },
  callout: {
    keys: ['role', 'text'],
    check(v) {
      if (!roleOk(v.role)) return 'role must be ok, info, warn, err or neu'
      if (typeof v.text !== 'string' || v.text.length === 0 || v.text.length > 2000) return 'text must be 1 to 2000 characters'
    },
  },
  // Proposed, not in orch.widgets.v1 yet.
  timeline: {
    keys: ['items'],
    check(v) {
      return items(v.items, {
        name: 'items', max: 50, size: '1 to 50 steps', allowed: ['at', 'label', 'status', 'note'], where: 'a step',
        each(o) {
          if (!validDate(o.at)) return 'at must be a real date (YYYY-MM-DD, optionally with THH:MM)'
          const r = labelReq(o, 'label', 'a step label')
          if (r) return r
          if (has(o, 'status') && !oneOf(o.status, STEP_STATES)) return 'status must be done, current, next or failed'
          return optText(o, 'note', 500)
        },
      })
    },
  },
  // Proposed, not in orch.widgets.v1 yet (widgets.md: "progress is not a type"; Tasks are drawn by core).
  progress: {
    keys: ['value', 'max', 'unit', 'segments'],
    check(v) {
      if (!num(v.max) || v.max <= 0 || v.max > MAX_MAGNITUDE) return 'max must be a positive number up to 1e15'
      if (has(v, 'value') === has(v, 'segments')) return 'progress needs exactly one of value or segments'
      if (!unitOk(v)) return 'unit must be a short string'
      const max = v.max
      if (has(v, 'value')) return num(v.value) && v.value >= 0 && v.value <= max ? undefined : 'value must be from 0 to max'
      let sum = 0
      const r = items(v.segments, {
        name: 'segments', max: 12, size: '1 to 12 segments', allowed: ['label', 'value', 'status'], where: 'a segment',
        each(o) {
          const l = labelReq(o, 'label', 'a segment label')
          if (l) return l
          if (!num(o.value) || o.value < 0) return 'a segment value must be a number of 0 or more'
          sum += o.value
          if (has(o, 'status') && !oneOf(o.status, SEGMENT_STATES)) return 'status must be ok, warn, err or neu'
        },
      })
      if (r) return r
      if (sum > max) return 'segments add up to more than max'
    },
  },
}
