// The catalog's core types beyond the first four (bars, table, checks, kv live in parse.ts): line, sparkline, metric,
// progress, timeline, diff, status, callout. Each is a strict check over a block that strictJson already parsed into
// prototype-free objects: unknown keys are refused at every level, every field is read as an own key, and a check
// returns a one-line reason or undefined. Nothing here draws, executes or fetches.
import { MAX_LABEL, MAX_ROWS } from './limits'

export interface CoreType {
  keys: string[]
  /** A one-line reason, or undefined when the block is valid. */
  check: (v: Record<string, unknown>) => string | undefined
}

export const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const has = (o: Record<string, unknown>, k: string) => Object.prototype.hasOwnProperty.call(o, k)
const shortStr = (v: unknown, max: number) => typeof v === 'string' && v.length <= max
const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const oneOf = (v: unknown, list: readonly string[]) => typeof v === 'string' && list.includes(v)

/** The reason for the first key of `o` that is not in `allowed`, naming where it was found. */
function unknownKey(o: Record<string, unknown>, allowed: readonly string[], where: string): string | undefined {
  for (const k of Object.keys(o)) if (!allowed.includes(k)) return `unknown key "${k}" in ${where}`
}

/** A list of item objects: 1..max entries, each an object with only `allowed` keys, then `each` per item. */
function items(v: unknown, opts: { name: string; min?: number; max: number; allowed: readonly string[]; where: string; each: (o: Record<string, unknown>) => string | undefined; size: string }): string | undefined {
  const min = opts.min ?? 1
  if (!Array.isArray(v) || v.length < min || v.length > opts.max) return `${opts.name} needs ${opts.size}`
  for (const o of v) {
    if (!isObj(o)) return `each entry of ${opts.name} must be an object`
    const r = unknownKey(o, opts.allowed, opts.where) ?? opts.each(o)
    if (r) return r
  }
}

const unitOk = (v: Record<string, unknown>, key = 'unit') => !has(v, key) || shortStr(v[key], 20)
const labelReq = (o: Record<string, unknown>, key: string, what: string) => (shortStr(o[key], MAX_LABEL) && (o[key] as string).length > 0 ? undefined : `${what} must be 1 to ${MAX_LABEL} characters`)
const optText = (o: Record<string, unknown>, key: string, max: number) => (has(o, key) && !shortStr(o[key], max) ? `${key} must be a string of at most ${max} characters` : undefined)

const DATE_RE = /^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(T([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?Z?)?$/

export const LINE_STATES = ['ok', 'warn', 'fail', 'skip', 'running'] as const
export const SEGMENT_STATES = ['ok', 'warn', 'fail', 'neutral'] as const
export const STEP_STATES = ['done', 'current', 'next', 'failed'] as const
export const CALLOUT_ROLES = ['note', 'ok', 'warn', 'err', 'decision'] as const
export const MAX_SERIES = 4
export const MAX_DIFF_LINES = 200

export const MORE_CORE: Record<string, CoreType> = {
  line: {
    keys: ['unit', 'x', 'series', 'markers'],
    check(v) {
      const x = v.x
      if (!Array.isArray(x) || x.length < 2 || x.length > MAX_ROWS) return `x needs 2 to ${MAX_ROWS} values`
      const numeric = x.every(num)
      if (!numeric && !x.every((s) => shortStr(s, MAX_LABEL))) return 'x must be all numbers or all labels'
      if (!unitOk(v)) return 'unit must be a short string'
      const names = new Set<string>()
      const s = items(v.series, {
        name: 'series', max: MAX_SERIES, size: `1 to ${MAX_SERIES} lines`, allowed: ['name', 'values'], where: 'a series',
        each(o) {
          const r = labelReq(o, 'name', 'a series name')
          if (r) return r
          const n = o.name as string
          if (names.has(n)) return `series name "${n}" is used twice`
          names.add(n)
          const vals = o.values
          if (!Array.isArray(vals)) return `values of "${n}" must be a list`
          if (vals.length !== x.length) return `series "${n}" has ${vals.length} values, expected ${x.length}`
          if (!vals.every((y) => y === null || num(y))) return `each value of "${n}" must be a number or null`
          if (!vals.some(num)) return `series "${n}" needs at least one number`
        },
      })
      if (s) return s
      if (has(v, 'markers'))
        return items(v.markers, {
          name: 'markers', min: 0, max: 20, size: 'at most 20 markers', allowed: ['at', 'label'], where: 'a marker',
          each(o) {
            if (!x.some((xv) => xv === o.at)) return `marker at "${String(o.at)}" is not a value of x`
            return labelReq(o, 'label', 'a marker label')
          },
        })
    },
  },
  sparkline: {
    keys: ['values', 'label', 'unit'],
    check(v) {
      if (!Array.isArray(v.values) || v.values.length < 2 || v.values.length > MAX_ROWS || !v.values.every(num)) return `values needs 2 to ${MAX_ROWS} numbers`
      if (!unitOk(v)) return 'unit must be a short string'
      if (has(v, 'label') && !shortStr(v.label, MAX_LABEL)) return `label must be at most ${MAX_LABEL} characters`
    },
  },
  metric: {
    keys: ['items'],
    check(v) {
      return items(v.items, {
        name: 'items', max: 8, size: '1 to 8 metrics', allowed: ['label', 'value', 'unit', 'delta', 'delta_unit', 'better', 'hint'], where: 'a metric',
        each(o) {
          const r = labelReq(o, 'label', 'a metric label')
          if (r) return r
          if (!num(o.value) && !shortStr(o.value, 40)) return 'value must be a number or a short string'
          if (!unitOk(o) || !unitOk(o, 'delta_unit')) return 'unit must be a short string'
          if (has(o, 'delta') && !num(o.delta)) return 'delta must be a number'
          if (has(o, 'better') && !oneOf(o.better, ['up', 'down'])) return 'better must be up or down'
          return optText(o, 'hint', MAX_LABEL)
        },
      })
    },
  },
  progress: {
    keys: ['value', 'max', 'unit', 'segments'],
    check(v) {
      if (!num(v.max) || v.max <= 0) return 'max must be a positive number'
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
          if (has(o, 'status') && !oneOf(o.status, SEGMENT_STATES)) return 'status must be ok, warn, fail or neutral'
        },
      })
      if (r) return r
      if (sum > max) return 'segments add up to more than max'
    },
  },
  timeline: {
    keys: ['items'],
    check(v) {
      return items(v.items, {
        name: 'items', max: 50, size: '1 to 50 steps', allowed: ['at', 'label', 'status', 'note'], where: 'a step',
        each(o) {
          if (typeof o.at !== 'string' || !DATE_RE.test(o.at)) return 'at must be a date (YYYY-MM-DD, optionally with THH:MM)'
          const r = labelReq(o, 'label', 'a step label')
          if (r) return r
          if (has(o, 'status') && !oneOf(o.status, STEP_STATES)) return 'status must be done, current, next or failed'
          return optText(o, 'note', 500)
        },
      })
    },
  },
  diff: {
    keys: ['file', 'lines'],
    check(v) {
      if (has(v, 'file') && typeof v.file !== 'string') return 'file must be a string'
      if (has(v, 'file') && (v.file as string).length > 300) return 'file is longer than 300 characters'
      if (typeof v.lines !== 'string' || v.lines.trim() === '') return 'lines must be a non-empty unified diff'
      if (v.lines.split('\n').length > MAX_DIFF_LINES) return `a diff shows at most ${MAX_DIFF_LINES} lines`
    },
  },
  status: {
    keys: ['items'],
    check(v) {
      return items(v.items, {
        name: 'items', max: MAX_ROWS, size: 'at least one item', allowed: ['name', 'status', 'detail'], where: 'an item',
        each(o) {
          const r = labelReq(o, 'name', 'an item name')
          if (r) return r
          if (!oneOf(o.status, LINE_STATES)) return 'status must be ok, warn, fail, skip or running'
          return optText(o, 'detail', 500)
        },
      })
    },
  },
  callout: {
    keys: ['role', 'text'],
    check(v) {
      if (!oneOf(v.role, CALLOUT_ROLES)) return 'role must be note, ok, warn, err or decision'
      if (typeof v.text !== 'string' || v.text.length === 0 || v.text.length > 2000) return 'text must be 1 to 2000 characters'
    },
  },
}
