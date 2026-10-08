import { describe, expect, it } from 'vitest'
import { MAX_BLOCKS, parseSection, resolveTicketWidgets, strictJson, type Block } from './parse'

const SHA = 'a'.repeat(64)
const fence = (body: unknown, ticks = '```') => `${ticks}orch\n${typeof body === 'string' ? body : JSON.stringify(body)}\n${ticks}`
const bars = { type: 'bars', data: { main: 412, branch: 286 } }
const blocksOf = (text: string, section = 'context'): Block[] => parseSection(text, section).flatMap((s) => (s.kind === 'widget' ? [s.block] : []))
const one = (body: unknown, ticks?: string) => blocksOf(fence(body, ticks))[0]

describe('fences (CommonMark rules)', () => {
  it('splits prose and one orch block, keeping the prose', () => {
    const segs = parseSection(`Before.\n\n${fence({ ...bars, title: 'Bundle size, kB', source: 'size.txt', caption: 'c', id: 'size' })}\n\nAfter.`, 'context')
    expect(segs.map((s) => s.kind)).toEqual(['markdown', 'widget', 'markdown'])
    const b = (segs[1] as { block: Block }).block
    expect(b.reason).toBeUndefined()
    expect(b.spec).toMatchObject({ layer: 'type', type: 'bars', title: 'Bundle size, kB', source: 'size.txt', caption: 'c', id: 'size' })
    expect(b.line).toBe(3)
  })
  it('leaves other info strings as prose', () => {
    for (const info of ['json', 'orch-x', 'orch extra', 'Orch', '']) {
      const segs = parseSection('```' + info + '\n{"type":"bars"}\n```', 'context')
      expect(segs.every((s) => s.kind === 'markdown'), info).toBe(true)
    }
  })
  it('accepts tilde fences and longer runs; closes only on the same character, at least as long', () => {
    expect(one(bars, '~~~').spec?.type).toBe('bars')
    expect(one(bars, '````').spec?.type).toBe('bars')
    const inner = '````orch\n{"type":"bars","data":{"a":1}}\n```\nstill inside\n````'
    expect(blocksOf(inner)[0].reason).toMatch(/invalid JSON/)
    const tilde = '~~~orch\n{"type":"bars","data":{"a":1}}\n```\n~~~'
    expect(blocksOf(tilde)[0].reason).toMatch(/invalid JSON/)
  })
  it('does not close on a run followed by text', () => {
    const b = blocksOf('```orch\n{"type":"bars","data":{"a":1}}\n``` nope')[0]
    expect(b.reason).toMatch(/not closed/)
  })
  it('an unclosed fence is an error block that runs to the end of the section', () => {
    const b = blocksOf('Intro\n```orch\n{"type":"bars"')[0]
    expect(b.reason).toMatch(/not closed/)
    expect(b.raw).toContain('{"type":"bars"')
  })
  it('ignores an orch fence nested in a longer outer fence, an indented code block or a list item', () => {
    expect(blocksOf('````md\n' + fence(bars) + '\n````')).toHaveLength(0)
    expect(blocksOf('    ' + '```orch\n    {}\n    ```')).toHaveLength(0)
    expect(blocksOf('- ```orch\n  {}\n  ```')).toHaveLength(0)
    expect(blocksOf('> ```orch\n> {}\n> ```')).toHaveLength(0)
  })
  it('allows up to three spaces of indent on the fence', () => {
    expect(blocksOf('   ' + fence(bars).replace(/\n/g, '\n   '))[0].spec?.type).toBe('bars')
  })
})

describe('strict JSON', () => {
  const reasons = (s: string) => one(s).reason ?? ''
  it('rejects invalid JSON with a reason', () => {
    expect(reasons('{"type":"bars",}')).toMatch(/invalid JSON/)
    expect(reasons("{'type':'bars'}")).toMatch(/invalid JSON/)
    expect(reasons('{"type":"bars"} x')).toMatch(/invalid JSON/)
    expect(reasons('')).toMatch(/invalid JSON/)
  })
  it('rejects duplicate keys, also nested', () => {
    expect(reasons('{"type":"bars","type":"table"}')).toMatch(/duplicate key "type"/)
    expect(reasons('{"type":"bars","data":{"a":1,"a":2}}')).toMatch(/duplicate key "a"/)
  })
  it('rejects NaN and Infinity', () => {
    expect(reasons('{"type":"bars","data":{"a":NaN}}')).toMatch(/NaN|Infinity/)
    expect(reasons('{"type":"bars","data":{"a":-Infinity}}')).toMatch(/NaN|Infinity/)
  })
  it('limits nesting depth to 8', () => {
    const deep = (n: number) => '['.repeat(n) + ']'.repeat(n)
    expect(() => strictJson(deep(8))).not.toThrow()
    expect(() => strictJson(deep(9))).toThrow(/nesting/)
    expect(reasons(`{"type":"bars","data":{"a":${deep(9)}}}`)).toMatch(/nesting/)
  })
  it('parses a normal document', () => {
    expect(strictJson('{"a":[1,2.5e1,"x\\n",true,null],"b":{}}')).toEqual({ a: [1, 25, 'x\n', true, null], b: {} })
  })
  it('requires a JSON object', () => {
    expect(reasons('[1,2]')).toMatch(/must be a JSON object/)
    expect(reasons('"bars"')).toMatch(/must be a JSON object/)
  })
  it('caps a block at 64 KiB', () => {
    const big = JSON.stringify({ type: 'table', columns: ['a'], rows: Array.from({ length: 400 }, () => ['x'.repeat(200)]) })
    expect(big.length).toBeGreaterThan(64 * 1024)
    expect(reasons(big)).toMatch(/64 KiB/)
  })
})

describe('layers and common keys', () => {
  const reason = (b: unknown) => one(b).reason ?? ''
  it('needs exactly one of type, widget or html', () => {
    expect(reason({ data: {} })).toMatch(/exactly one of type, widget or html/)
    expect(reason({ ...bars, widget: 'before-after@1', sha256: SHA })).toMatch(/exactly one of type, widget or html/)
    expect(reason({ type: 'bars', html: 'artifact:a.html', sha256: SHA })).toMatch(/exactly one/)
  })
  it('refuses unknown top-level keys', () => {
    expect(reason({ ...bars, colour: 'red' })).toMatch(/unknown key "colour"/)
    expect(reason({ widget: 'before-after@1', sha256: SHA, unit: 'kB' })).toMatch(/unknown key "unit"/)
  })
  it('checks id, title, source and caption', () => {
    for (const id of ['1abc', 'Abc', 'a_b', '-a', '', 'a'.repeat(41)]) expect(reason({ ...bars, id }), id).toMatch(/id must match/)
    for (const id of ['a', 'a1-b', 'a'.repeat(40)]) expect(one({ ...bars, id }).reason, id).toBeUndefined()
    expect(reason({ ...bars, title: 'x'.repeat(201) })).toMatch(/title is longer than 200/)
    expect(reason({ ...bars, source: 'x'.repeat(501) })).toMatch(/source is longer than 500/)
    expect(reason({ ...bars, caption: 'x'.repeat(501) })).toMatch(/caption is longer than 500/)
    expect(reason({ ...bars, title: 5 })).toMatch(/title must be a string/)
  })
  it('refuses strings longer than 20000 characters', () => {
    expect(reason({ type: 'table', columns: ['a'], rows: [['x'.repeat(20001)]] })).toMatch(/20000/)
  })
})

describe('core types', () => {
  const reason = (b: unknown) => one(b).reason ?? ''
  it('knows bars, table, checks and kv', () => {
    expect(one(bars).spec?.type).toBe('bars')
    expect(one({ type: 'bars', unit: 'kB', data: [['a', 1], ['b', 2]], highlight: 'b' }).reason).toBeUndefined()
    expect(one({ type: 'table', columns: ['a', 'b'], rows: [[1, 'x']] }).reason).toBeUndefined()
    expect(one({ type: 'checks', rows: [{ ac: 'AC1', verdict: 'met', evidence: 'ok' }] }).reason).toBeUndefined()
    expect(one({ type: 'kv', items: { Owner: 'Mara', Count: 3 } }).reason).toBeUndefined()
  })
  it('refuses an unknown type and a documented type this mockup does not draw', () => {
    expect(reason({ type: 'sparkly' })).toMatch(/unknown widget type "sparkly"/)
    expect(reason({ type: 'stats', items: [] })).toMatch(/type "stats" is not drawn in this mockup/)
  })
  it('validates bars', () => {
    expect(reason({ type: 'bars' })).toMatch(/data/)
    expect(reason({ type: 'bars', data: {} })).toMatch(/at least one/)
    expect(reason({ type: 'bars', data: { a: 'x' } })).toMatch(/number/)
    expect(reason({ type: 'bars', data: { a: 1 }, highlight: 'zzz' })).toMatch(/highlight/)
    expect(reason({ type: 'bars', data: { a: 1 }, extra: 1 })).toMatch(/unknown key "extra"/)
    expect(reason({ type: 'bars', data: [['a']] })).toMatch(/\[label, number\]/)
  })
  it('validates table size and shape', () => {
    expect(reason({ type: 'table', columns: ['a', 'b'], rows: [[1]] })).toMatch(/row 1 has 1 cells, expected 2/)
    expect(reason({ type: 'table', columns: ['a'], rows: Array.from({ length: 501 }, () => [1]) })).toMatch(/more than 500 rows/)
    expect(reason({ type: 'table', columns: Array.from({ length: 51 }, (_, i) => `c${i}`), rows: [] })).toMatch(/more than 50 columns/)
    expect(reason({ type: 'table', columns: ['a'], rows: [[{}]] })).toMatch(/cell/)
    expect(one({ type: 'table', columns: ['a'], rows: Array.from({ length: 500 }, () => [1]) }).reason).toBeUndefined()
  })
  it('validates checks', () => {
    expect(reason({ type: 'checks', rows: [{ ac: 'A1', verdict: 'met', evidence: 'x' }] })).toMatch(/ac must be AC<n>/)
    expect(reason({ type: 'checks', rows: [{ ac: 'AC1', verdict: 'maybe', evidence: 'x' }] })).toMatch(/verdict/)
    expect(reason({ type: 'checks', rows: [{ ac: 'AC1', verdict: 'met', evidence: 'x', zzz: 1 }] })).toMatch(/unknown key "zzz"/)
    expect(reason({ type: 'checks', rows: [] })).toMatch(/at least one/)
  })
  it('validates kv', () => {
    expect(reason({ type: 'kv', items: {} })).toMatch(/at least one/)
    expect(reason({ type: 'kv', items: { a: {} } })).toMatch(/string, number or boolean/)
  })
})

describe('template and html layers', () => {
  it('needs name@version and a 64-hex sha256 pin for a template', () => {
    expect(one({ widget: 'before-after@1', sha256: SHA, data: {} }).spec).toMatchObject({ layer: 'widget', widget: 'before-after@1', sha256: SHA })
    expect(one({ widget: 'before-after', sha256: SHA }).reason).toMatch(/name@version/)
    expect(one({ widget: 'Before-After@1', sha256: SHA }).reason).toMatch(/name@version/)
    expect(one({ widget: 'before-after@1' }).reason).toMatch(/sha256 pin/)
    expect(one({ widget: 'before-after@1', sha256: 'abc' }).reason).toMatch(/64 hex/)
    expect(one({ widget: 'before-after@1', sha256: SHA, data: [] }).reason).toMatch(/data must be an object/)
  })
  it('names a ticket artifact by artifacts/<ID>/<name> or artifact:<name>, with a pin', () => {
    expect(one({ html: 'artifacts/DEMO-0041/p.html', sha256: SHA }).spec).toMatchObject({ layer: 'html', artifact: 'p.html' })
    expect(one({ html: 'artifact:p.html', sha256: SHA, height: 300 }).spec).toMatchObject({ layer: 'html', artifact: 'p.html', height: 300 })
    expect(one({ html: 'p.html', sha256: SHA }).reason).toMatch(/artifacts\/<ID>\/<name> or artifact:<name>/)
    expect(one({ html: 'artifact:../x.html', sha256: SHA }).reason).toMatch(/artifacts\/<ID>/)
    expect(one({ html: 'artifact:p.html' }).reason).toMatch(/sha256 pin/)
    expect(one({ html: 'artifact:p.html', sha256: SHA, height: 5 }).reason).toMatch(/height/)
  })
})

describe('ticket-wide rules', () => {
  const body = (o: Record<string, string>) => resolveTicketWidgets(o)
  const all = (o: Record<string, string>) => Object.values(body(o)).flatMap((segs) => segs.flatMap((s) => (s.kind === 'widget' ? [s.block] : [])))
  it('refuses every block that shares an id, in any section', () => {
    const bs = all({ context: fence({ ...bars, id: 'size' }), current_state: fence({ ...bars, id: 'size' }), verification: fence({ ...bars, id: 'other' }) })
    expect(bs.map((b) => [b.section, !!b.reason])).toEqual([['context', true], ['verification', false], ['current_state', true]])
    expect(bs[0].reason).toMatch(/id "size" is used by more than one widget in this ticket/)
  })
  it('lets blocks without an id coexist', () => {
    expect(all({ context: fence(bars) + '\n\n' + fence(bars) }).every((b) => !b.reason)).toBe(true)
  })
  it('allows context, current_state and verification only', () => {
    for (const k of ['context', 'current_state', 'verification']) expect(all({ [k]: fence(bars) })[0].reason, k).toBeUndefined()
    for (const k of ['summary', 'requirements', 'out_of_scope', 'plan', 'decisions']) {
      const b = all({ [k]: fence(bars) })[0]
      expect(b.reason, k).toMatch(/widgets are not drawn in/)
      expect(b.raw).toContain('"bars"')
    }
  })
  it('draws the first 40 blocks and shows the rest as code', () => {
    const text = Array.from({ length: MAX_BLOCKS + 2 }, () => fence(bars)).join('\n\n')
    const bs = all({ context: text })
    expect(bs).toHaveLength(MAX_BLOCKS + 2)
    expect(bs.slice(0, MAX_BLOCKS).every((b) => !b.reason)).toBe(true)
    expect(bs[MAX_BLOCKS].reason).toMatch(/first 40/)
    expect(bs[MAX_BLOCKS + 1].reason).toMatch(/first 40/)
  })
  it('numbers the drawable blocks in section order', () => {
    const bs = all({ context: fence(bars), verification: '{"x"}\n' + fence({ type: 'nope' }) + '\n' + fence(bars), current_state: fence(bars) })
    expect(bs.filter((b) => !b.reason).map((b) => b.index)).toEqual([0, 1, 2])
  })
})

describe('review fixes', () => {
  it('refuses Object.prototype names as a type, in any section, without throwing', () => {
    for (const name of ['constructor', '__proto__', 'toString', 'valueOf', 'hasOwnProperty']) {
      for (const section of ['context', 'current_state', 'verification', 'summary']) {
        const segs = resolveTicketWidgets({ [section]: fence(`{"type":"${name}"}`) })
        const b = (segs[section][0] as { block: Block }).block
        expect(b.reason, `${name} in ${section}`).toBeTruthy()
        expect(b.spec).toBeUndefined()
      }
      expect(one({ type: name }).reason).toMatch(/unknown widget type/)
    }
  })
  it('a section key named like an Object.prototype member does not break labels', () => {
    expect(() => resolveTicketWidgets({ constructor: fence(bars) }, { order: ['constructor'] })).not.toThrow()
  })
  it('keeps a fence indented under a list item as code (2-space case), but not after the list ends', () => {
    expect(blocksOf('- item\n  ' + fence(bars).replace(/\n/g, '\n  '))).toHaveLength(0)
    expect(blocksOf('1. item\n\n   ' + fence(bars).replace(/\n/g, '\n   '))).toHaveLength(0)
    expect(blocksOf('- item\n\nPlain paragraph.\n\n' + fence(bars))).toHaveLength(1)
    expect(blocksOf('- item\n' + fence(bars))).toHaveLength(1)
  })
  it('a lazy continuation line keeps the list item open, so an indented fence after it stays code', () => {
    const indented = '  ' + fence(bars).replace(/\n/g, '\n  ')
    expect(blocksOf('- item\nlazy text\n' + indented)).toHaveLength(0) // the reviewer's probe
    expect(blocksOf('- item\nlazy one\nlazy two\n' + indented)).toHaveLength(0)
    // A blank line, then a paragraph at the margin, ends the list; a heading is never a lazy line.
    expect(blocksOf('- item\n\nnot lazy\n' + indented)).toHaveLength(1)
    expect(blocksOf('- item\n# Heading\n' + indented)).toHaveLength(1)
  })
  it('refuses negative bar values with a reason', () => {
    expect(one({ type: 'bars', data: { a: 3, b: -1 } }).reason).toMatch(/"b" is negative/)
    expect(one({ type: 'bars', data: [['a', -2]] }).reason).toMatch(/negative/)
    expect(one({ type: 'bars', data: { a: 0 } }).reason).toBeUndefined()
  })
})
