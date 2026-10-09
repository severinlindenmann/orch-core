import { describe, expect, it } from 'vitest'
import { parseBlock } from './parse'

// The catalog's new core types (wave 3, N4): each one is accepted in its documented shape and refused, with a reason,
// for anything else. Unknown keys are refused at the top level and inside every item object.
const reason = (b: unknown) => parseBlock(JSON.stringify(b)).reason ?? ''
const ok = (b: unknown) => {
  const r = parseBlock(JSON.stringify(b))
  expect(r.reason, JSON.stringify(b).slice(0, 120)).toBeUndefined()
  return r.spec!
}

describe('line', () => {
  const base = { type: 'line', unit: 'min', x: ['10-01', '10-02', '10-03'], series: [{ name: 'CI', values: [6, 7.5, null] }] }
  it('accepts shared x labels and up to four series', () => {
    expect(ok(base).fields.series).toHaveLength(1)
    ok({ ...base, x: [1, 2, 3], markers: [{ at: 2, label: 'cache on' }] })
    ok({ ...base, series: Array.from({ length: 4 }, (_, i) => ({ name: `s${i}`, values: [1, 2, 3] })) })
  })
  it('refuses bad shapes', () => {
    expect(reason({ ...base, x: ['a'] })).toMatch(/x needs 2 to 500 values/)
    expect(reason({ ...base, x: ['a', 2, 'c'] })).toMatch(/x must be all numbers or all labels/)
    expect(reason({ ...base, series: [] })).toMatch(/series needs 1 to 4 lines/)
    expect(reason({ ...base, series: Array.from({ length: 5 }, (_, i) => ({ name: `s${i}`, values: [1, 2, 3] })) })).toMatch(/series needs 1 to 4/)
    expect(reason({ ...base, series: [{ name: 'a', values: [1, 2] }] })).toMatch(/has 2 values, expected 3/)
    expect(reason({ ...base, series: [{ name: 'a', values: [1, 'x', 3] }] })).toMatch(/number or null/)
    expect(reason({ ...base, series: [{ name: 'a', values: [null, null, null] }] })).toMatch(/at least one number/)
    expect(reason({ ...base, series: [{ name: 'a', values: [1, 2, 3], colour: 'red' }] })).toMatch(/unknown key "colour" in a series/)
    expect(reason({ ...base, series: [{ name: 'a', values: [1, 2, 3] }, { name: 'a', values: [1, 2, 3] }] })).toMatch(/series name "a" is used twice/)
    expect(reason({ ...base, markers: [{ at: 'nope', label: 'x' }] })).toMatch(/marker at "nope" is not a value of x/)
    expect(reason({ ...base, smooth: true })).toMatch(/unknown key "smooth"/)
  })
})

describe('sparkline', () => {
  it('accepts values with an optional label and unit', () => {
    ok({ type: 'sparkline', label: 'CI time', unit: 'min', values: [9, 8, 7, 6] })
  })
  it('refuses too few or non-numeric values', () => {
    expect(reason({ type: 'sparkline', values: [1] })).toMatch(/values needs 2 to 500 numbers/)
    expect(reason({ type: 'sparkline', values: [1, '2'] })).toMatch(/values needs 2 to 500 numbers/)
    expect(reason({ type: 'sparkline', values: [1, 2], unit: 'x'.repeat(21) })).toMatch(/unit must be a short string/)
  })
})

describe('metric', () => {
  const item = { label: 'Seeds loaded', value: 31, unit: 'tables', delta: 9, delta_unit: 'since 09:00', better: 'up', hint: 'of 40' }
  it('accepts 1 to 8 items with value, unit and delta', () => {
    ok({ type: 'metric', items: [item] })
    ok({ type: 'metric', items: [{ label: 'Status', value: 'green' }] })
  })
  it('refuses bad items', () => {
    expect(reason({ type: 'metric', items: [] })).toMatch(/items needs 1 to 8 metrics/)
    expect(reason({ type: 'metric', items: [{ ...item, better: 'sideways' }] })).toMatch(/better must be up or down/)
    expect(reason({ type: 'metric', items: [{ ...item, delta: '9' }] })).toMatch(/delta must be a number/)
    expect(reason({ type: 'metric', items: [{ ...item, value: { a: 1 } }] })).toMatch(/value must be a number or a short string/)
    expect(reason({ type: 'metric', items: [{ ...item, tone: 'red' }] })).toMatch(/unknown key "tone" in a metric/)
    expect(reason({ type: 'metric', items: [{ value: 1 }] })).toMatch(/label/)
  })
})

describe('progress', () => {
  it('accepts a value or segments against a max', () => {
    ok({ type: 'progress', value: 31, max: 40, unit: 'tables' })
    ok({ type: 'progress', max: 4, segments: [{ label: 'done', value: 1, status: 'ok' }, { label: 'blocked', value: 2, status: 'warn' }] })
  })
  it('refuses inconsistent numbers', () => {
    expect(reason({ type: 'progress', value: 41, max: 40 })).toMatch(/value must be from 0 to max/)
    expect(reason({ type: 'progress', value: 1, max: 0 })).toMatch(/max must be a positive number/)
    expect(reason({ type: 'progress', max: 40 })).toMatch(/exactly one of value or segments/)
    expect(reason({ type: 'progress', max: 4, value: 1, segments: [{ label: 'a', value: 1 }] })).toMatch(/exactly one of value or segments/)
    expect(reason({ type: 'progress', max: 2, segments: [{ label: 'a', value: 2 }, { label: 'b', value: 1 }] })).toMatch(/segments add up to more than max/)
    expect(reason({ type: 'progress', max: 2, segments: [{ label: 'a', value: 1, status: 'red' }] })).toMatch(/status must be ok, warn, fail or neutral/)
  })
})

describe('timeline', () => {
  it('accepts dated steps', () => {
    ok({ type: 'timeline', items: [{ at: '2026-10-07', label: 'Plan approved', status: 'done' }, { at: '2026-10-09T09:55Z', label: 'Check run', status: 'current', note: 'fails on midnight' }] })
  })
  it('refuses bad dates and states', () => {
    expect(reason({ type: 'timeline', items: [] })).toMatch(/items needs 1 to 50 steps/)
    expect(reason({ type: 'timeline', items: [{ at: 'yesterday', label: 'x' }] })).toMatch(/at must be a date/)
    expect(reason({ type: 'timeline', items: [{ at: '2026-10-07', label: 'x', status: 'late' }] })).toMatch(/status must be done, current, next or failed/)
    expect(reason({ type: 'timeline', items: [{ at: '2026-10-07', label: 'x', icon: 'y' }] })).toMatch(/unknown key "icon" in a step/)
  })
})

describe('diff', () => {
  it('accepts a unified diff of a few lines', () => {
    ok({ type: 'diff', file: 'models/fct_usage.sql', lines: '@@ -1,2 +1,2 @@\n-where day = d\n+where hour = h\n context' })
  })
  it('refuses empty or long diffs', () => {
    expect(reason({ type: 'diff', lines: '' })).toMatch(/lines must be a non-empty unified diff/)
    expect(reason({ type: 'diff', lines: Array.from({ length: 201 }, () => '+x').join('\n') })).toMatch(/at most 200 lines/)
    expect(reason({ type: 'diff', lines: '+x', file: 5 })).toMatch(/file must be a string/)
  })
})

describe('status', () => {
  it('accepts items with ok/warn/fail', () => {
    ok({ type: 'status', items: [{ name: 'dbt build', status: 'ok' }, { name: 'lint', status: 'warn', detail: '2 warnings' }, { name: 'tests', status: 'fail' }] })
  })
  it('refuses unknown states and keys', () => {
    expect(reason({ type: 'status', items: [{ name: 'a', status: 'green' }] })).toMatch(/status must be ok, warn, fail, skip or running/)
    expect(reason({ type: 'status', items: [{ name: 'a', status: 'ok', url: 'x' }] })).toMatch(/unknown key "url" in an item/)
    expect(reason({ type: 'status', items: [] })).toMatch(/items needs at least one item/)
  })
})

describe('callout', () => {
  it('accepts a note, a warning or a decision', () => {
    for (const role of ['note', 'ok', 'warn', 'err', 'decision']) ok({ type: 'callout', role, text: 'Mask by hour.' })
  })
  it('refuses other roles and empty text', () => {
    expect(reason({ type: 'callout', role: 'shout', text: 'x' })).toMatch(/role must be note, ok, warn, err or decision/)
    expect(reason({ type: 'callout', role: 'note', text: '' })).toMatch(/text must be 1 to 2000 characters/)
    expect(reason({ type: 'callout', role: 'note', text: 'x', markdown: '<b>' })).toMatch(/unknown key "markdown"/)
  })
})

describe('item objects are read safely', () => {
  it('a __proto__ key inside an item is an unknown key, not a prototype', () => {
    expect(parseBlock('{"type":"status","items":[{"name":"a","status":"ok","__proto__":{"x":1}}]}').reason).toMatch(/unknown key "__proto__"/)
    expect(parseBlock('{"type":"metric","items":[{"label":"a","value":1,"constructor":1}]}').reason).toMatch(/unknown key "constructor"/)
    expect(({} as Record<string, unknown>).x).toBeUndefined()
  })
  it('an inherited name is not a field: hasOwnProperty as a role is refused', () => {
    expect(reason({ type: 'callout', role: 'hasOwnProperty', text: 'x' })).toMatch(/role must be/)
    expect(reason({ type: 'toString' })).toMatch(/unknown widget type "toString"/)
  })
})
