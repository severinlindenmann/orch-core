import { describe, expect, it } from 'vitest'
import { validDate } from './coreTypes'
import { niceTicks } from './CoreViews'
import { parseBlock } from './parse'

// The catalog's newer core types: orch.widgets.v1 shapes (stats, series, spark, gates, diff, callout) plus two
// proposals (timeline, progress). Each is accepted in its documented shape and refused, with a reason, for anything
// else. Unknown keys are refused at the top level and inside every item object.
const reason = (b: unknown) => parseBlock(JSON.stringify(b)).reason ?? ''
const ok = (b: unknown) => {
  const r = parseBlock(JSON.stringify(b))
  expect(r.reason, JSON.stringify(b).slice(0, 120)).toBeUndefined()
  return r.spec!
}

describe('series', () => {
  const multi = { type: 'series', unit: 'min', x: ['10-01', '10-02', '10-03'], series: [{ name: 'CI', values: [6, 7.5, null] }] }
  it('accepts the widgets.md shape: points [[x, y]] with markers at x', () => {
    ok({ type: 'series', unit: 'ms', points: [[1, 120], [2, 140], [3, 90]], markers: [{ x: 2, label: 'cache on' }] })
  })
  it('accepts shared x labels with up to four named series', () => {
    ok(multi)
    ok({ ...multi, x: [1, 2, 3], markers: [{ x: 2, label: 'deploy' }] })
    ok({ ...multi, series: Array.from({ length: 4 }, (_, i) => ({ name: `s${i}`, values: [1, 2, 3] })) })
  })
  it('refuses bad shapes', () => {
    expect(reason({ type: 'series', points: [[1, 2]] })).toMatch(/points needs 2 to 500/)
    expect(reason({ type: 'series', points: [[1, 'a'], [2, 3]] })).toMatch(/two numbers/)
    expect(reason({ type: 'series', points: [[1, 2], [2, 3]], series: [] })).toMatch(/exactly one of points or x with series/)
    expect(reason({ type: 'series', points: [[1, 2], [2, 3]], x: [1, 2] })).toMatch(/x goes with series/)
    expect(reason({ type: 'series', points: [[1, 2], [2, 3]], markers: [{ x: '9', label: 'late' }] })).toMatch(/marker x must be a number/)
    expect(reason({ ...multi, x: ['a'] })).toMatch(/x needs 2 to 500 values/)
    expect(reason({ ...multi, x: ['a', 2, 'c'] })).toMatch(/all numbers or all labels/)
    expect(reason({ ...multi, series: [] })).toMatch(/series needs 1 to 4 lines/)
    expect(reason({ ...multi, series: Array.from({ length: 5 }, (_, i) => ({ name: `s${i}`, values: [1, 2, 3] })) })).toMatch(/1 to 4/)
    expect(reason({ ...multi, series: [{ name: 'a', values: [1, 2] }] })).toMatch(/has 2 values, expected 3/)
    expect(reason({ ...multi, series: [{ name: 'a', values: [null, null, null] }] })).toMatch(/at least one number/)
    expect(reason({ ...multi, series: [{ name: 'a', values: [1, 2, 3], colour: 'red' }] })).toMatch(/unknown key "colour" in a series/)
    expect(reason({ ...multi, series: [{ name: 'a', values: [1, 2, 3] }, { name: 'a', values: [1, 2, 3] }] })).toMatch(/used twice/)
    expect(reason({ ...multi, markers: [{ at: '10-02', label: 'x' }] })).toMatch(/unknown key "at" in a marker/)
    expect(reason({ ...multi, markers: [{ x: '11-30', label: 'x' }] })).toMatch(/not one of the x labels/)
    expect(reason({ ...multi, smooth: true })).toMatch(/unknown key "smooth"/)
  })
  it('refuses only numbers beyond ±1e15 (where the axis math could fail)', () => {
    // -3e17 and the next representable double: the spread is below the values' own precision.
    expect(reason({ type: 'series', points: [[1, -300000000000000000], [2, -299999999999999936]] })).toMatch(/between -1e15 and 1e15/)
    expect(reason({ type: 'series', points: [[1, 1e308], [2, -1e308]] })).toMatch(/between -1e15 and 1e15/)
    expect(reason({ type: 'series', points: [[1e16, 1], [2e16, 2]] })).toMatch(/x values must be between/)
    expect(reason({ type: 'spark', values: [-300000000000000000, -299999999999999936], text: '{spark}' })).toMatch(/between -1e15 and 1e15/)
  })
})

describe('caps match the v1 Python schemas (no v1-valid block is refused)', () => {
  it('series: a tiny spread and a flat line are accepted (no precision refusal)', () => {
    ok({ type: 'series', points: [[1, 1e14], [2, 1e14 + 0.02]] })
    ok({ type: 'series', points: [[1, 5], [2, 5], [3, 5]] })
    ok({ type: 'series', points: [[1, 1e-7], [2, 2e-7], [3, 1.5e-7]] })
  })
  it('series: a marker x may be any finite number on a numeric axis, also outside the data', () => {
    ok({ type: 'series', points: [[1, 2], [2, 3]], markers: [{ x: 9, label: 'later' }, { x: -1e12, label: 'far' }] })
    ok({ type: 'series', x: [1, 2, 3], series: [{ name: 'a', values: [1, 2, 3] }], markers: [{ x: 2.5, label: 'between' }] })
  })
  it('stats: up to 12 items, value and delta strings up to 200 characters', () => {
    ok({ type: 'stats', items: Array.from({ length: 12 }, (_, i) => ({ label: `s${i}`, value: i })) })
    expect(reason({ type: 'stats', items: Array.from({ length: 13 }, (_, i) => ({ label: `s${i}`, value: i })) })).toMatch(/1 to 12/)
    ok({ type: 'stats', items: [{ label: 'a', value: 'v'.repeat(200), delta: 'd'.repeat(200) }] })
    expect(reason({ type: 'stats', items: [{ label: 'a', value: 'v'.repeat(201) }] })).toMatch(/at most 200/)
  })
  it('diff: file up to 500 characters', () => {
    ok({ type: 'diff', file: 'f'.repeat(500), lines: '+x' })
    expect(reason({ type: 'diff', file: 'f'.repeat(501), lines: '+x' })).toMatch(/longer than 500/)
  })
  it('gates: seconds up to 1e12', () => {
    ok({ type: 'gates', items: [{ name: 'soak', status: 'pass', seconds: 1e12 }] })
    expect(reason({ type: 'gates', items: [{ name: 'soak', status: 'pass', seconds: 1e12 + 1 }] })).toMatch(/0 to 1e12/)
  })
})

describe('niceTicks (the y axis) always ends', () => {
  it('returns at most 12 ticks at once, also for spreads below a step\'s precision or overflowing ranges', () => {
    const t0 = performance.now()
    for (const [lo, hi] of [[-300000000000000000, -299999999999999936], [-1e308, 1e308], [0, Number.MAX_VALUE], [1e-320, 2e-320], [0, 72], [5, 5]] as [number, number][]) {
      const t = niceTicks(lo, hi)
      expect(t.length, `${lo}..${hi}`).toBeGreaterThan(0)
      expect(t.length, `${lo}..${hi}`).toBeLessThanOrEqual(12)
    }
    expect(niceTicks(0, 72)).toEqual([0, 25, 50, 75])
    // Ticks for 1e-7-sized values stay distinct (no rounding collisions).
    const tiny = niceTicks(0, 3e-7)
    expect(new Set(tiny).size).toBe(tiny.length)
    expect(tiny.length).toBeGreaterThan(2)
    expect(niceTicks(0, 0.3)).toEqual([0, 0.1, 0.2, 0.3])
    expect(performance.now() - t0).toBeLessThan(100)
  })
})

describe('spark', () => {
  it('accepts values and a sentence with {spark} once', () => {
    ok({ type: 'spark', values: [9, 8, 7, 6], text: 'CI {spark} now 6m' })
  })
  it('refuses missing or repeated placeholders and bad values', () => {
    expect(reason({ type: 'spark', values: [1, 2], text: 'no placeholder' })).toMatch(/\{spark\} exactly once/)
    expect(reason({ type: 'spark', values: [1, 2], text: '{spark} and {spark}' })).toMatch(/\{spark\} exactly once/)
    expect(reason({ type: 'spark', values: [1], text: '{spark}' })).toMatch(/values needs 2 to 500 numbers/)
    expect(reason({ type: 'spark', values: [1, 2], text: '{spark}', label: 'x' })).toMatch(/unknown key "label"/)
  })
})

describe('stats', () => {
  it('accepts label, value, delta and role', () => {
    ok({ type: 'stats', items: [{ label: 'Seeds', value: 31, delta: 9, role: 'ok' }, { label: 'Status', value: 'green', delta: '+2 today' }, { label: 'Old', value: 1, role: 'info' }] })
  })
  it('accepts a blank value, as orch.widgets.v1 does', () => {
    ok({ type: 'stats', items: [{ label: 'a', value: '' }, { label: 'b', value: '   ' }] })
  })
  it('refuses bad items', () => {
    expect(reason({ type: 'stats', items: [] })).toMatch(/1 to 12 numbers/)
    expect(reason({ type: 'stats', items: [{ label: 'a', value: 'x'.repeat(201) }] })).toMatch(/at most 200 characters/)
    expect(reason({ type: 'stats', items: [{ label: 'a', value: 1, role: 'decision' }] })).toMatch(/role must be ok, info, warn, err or neu/)
    expect(reason({ type: 'stats', items: [{ label: 'a', value: 1, role: 'note' }] })).toMatch(/role must be ok, info, warn, err or neu/)
    expect(reason({ type: 'stats', items: [{ label: 'a', value: 1, unit: 'kB' }] })).toMatch(/unknown key "unit" in a stat/)
    expect(reason({ type: 'stats', items: [{ value: 1 }] })).toMatch(/label/)
  })
})

describe('gates', () => {
  it('accepts pass/fail/skip/running with seconds', () => {
    ok({ type: 'gates', items: [{ name: 'build', status: 'pass', seconds: 252 }, { name: 'test', status: 'fail' }, { name: 'docs', status: 'skip' }, { name: 'e2e', status: 'running' }] })
  })
  it('has one name per state: ok is not another name for pass', () => {
    expect(reason({ type: 'gates', items: [{ name: 'lint', status: 'ok' }] })).toMatch(/status must be pass, fail, skip or running/)
  })
  it('refuses other states, bad seconds and unknown keys', () => {
    expect(reason({ type: 'gates', items: [{ name: 'a', status: 'warn' }] })).toMatch(/status must be pass, fail, skip or running/)
    expect(reason({ type: 'gates', items: [{ name: 'a', status: 'pass', seconds: -1 }] })).toMatch(/seconds/)
    expect(reason({ type: 'gates', items: [{ name: 'a', status: 'pass', detail: 'x' }] })).toMatch(/unknown key "detail" in a gate/)
    expect(reason({ type: 'gates', items: [] })).toMatch(/at least one gate/)
  })
})

describe('diff', () => {
  it('accepts a file and a unified diff', () => {
    ok({ type: 'diff', file: 'models/fct_usage.sql', lines: '@@ -1,2 +1,2 @@\n-where day = d\n+where hour = h\n context' })
  })
  it('needs the file, and refuses empty or long diffs', () => {
    expect(reason({ type: 'diff', lines: '+x' })).toMatch(/diff needs file/)
    expect(reason({ type: 'diff', file: 'a', lines: '' })).toMatch(/non-empty unified diff/)
    expect(reason({ type: 'diff', file: 'a', lines: Array.from({ length: 201 }, () => '+x').join('\n') })).toMatch(/at most 200 lines/)
  })
})

describe('callout', () => {
  it('accepts the widgets.md roles, and only those (note is not another name for info)', () => {
    for (const role of ['ok', 'info', 'warn', 'err', 'neu']) ok({ type: 'callout', role, text: 'Mask by hour.' })
    expect(reason({ type: 'callout', role: 'note', text: 'x' })).toMatch(/role must be ok, info, warn, err or neu/)
  })
  it('refuses other roles and empty text', () => {
    expect(reason({ type: 'callout', role: 'decision', text: 'x' })).toMatch(/role must be ok, info, warn, err or neu/)
    expect(reason({ type: 'callout', role: 'info', text: '' })).toMatch(/text must be 1 to 2000 characters/)
    expect(reason({ type: 'callout', role: 'info', text: 'x', markdown: '<b>' })).toMatch(/unknown key "markdown"/)
  })
})

describe('proposed: timeline', () => {
  it('accepts dated steps', () => {
    ok({ type: 'timeline', items: [{ at: '2026-10-07', label: 'Plan approved', status: 'done' }, { at: '2026-10-09T09:55Z', label: 'Check run', status: 'current', note: 'fails on midnight' }] })
  })
  it('refuses impossible dates, bad states and unknown keys', () => {
    expect(reason({ type: 'timeline', items: [] })).toMatch(/1 to 50 steps/)
    for (const at of ['yesterday', '2026-02-31', '2026-13-01', '2026-00-10', '2025-02-29', '2026-10-09T24:00']) expect(reason({ type: 'timeline', items: [{ at, label: 'x' }] }), at).toMatch(/real date/)
    expect(validDate('2028-02-29')).toBe(true)
    expect(reason({ type: 'timeline', items: [{ at: '2026-10-07', label: 'x', status: 'late' }] })).toMatch(/done, current, next or failed/)
    expect(reason({ type: 'timeline', items: [{ at: '2026-10-07', label: 'x', icon: 'y' }] })).toMatch(/unknown key "icon" in a step/)
  })
})

describe('proposed: progress', () => {
  it('accepts a value or segments against a max', () => {
    ok({ type: 'progress', value: 31, max: 40, unit: 'tables' })
    ok({ type: 'progress', max: 4, segments: [{ label: 'done', value: 1, status: 'ok' }, { label: 'blocked', value: 2, status: 'warn' }] })
  })
  it('refuses inconsistent numbers', () => {
    expect(reason({ type: 'progress', value: 41, max: 40 })).toMatch(/value must be from 0 to max/)
    expect(reason({ type: 'progress', value: 1, max: 0 })).toMatch(/max must be a positive number/)
    expect(reason({ type: 'progress', max: 40 })).toMatch(/exactly one of value or segments/)
    expect(reason({ type: 'progress', max: 2, segments: [{ label: 'a', value: 2 }, { label: 'b', value: 1 }] })).toMatch(/more than max/)
    expect(reason({ type: 'progress', max: 2, segments: [{ label: 'a', value: 1, status: 'red' }] })).toMatch(/ok, warn, err or neu/)
  })
})

describe('item objects are read safely', () => {
  it('a __proto__ key inside an item is an unknown key, not a prototype', () => {
    expect(parseBlock('{"type":"gates","items":[{"name":"a","status":"pass","__proto__":{"x":1}}]}').reason).toMatch(/unknown key "__proto__"/)
    expect(parseBlock('{"type":"stats","items":[{"label":"a","value":1,"constructor":1}]}').reason).toMatch(/unknown key "constructor"/)
    expect(({} as Record<string, unknown>).x).toBeUndefined()
  })
  it('an inherited name is not a field: hasOwnProperty as a role is refused', () => {
    expect(reason({ type: 'callout', role: 'hasOwnProperty', text: 'x' })).toMatch(/role must be/)
    expect(reason({ type: 'toString' })).toMatch(/unknown widget type "toString"/)
  })
})
