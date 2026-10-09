import { describe, expect, it } from 'vitest'
import { CORE_TYPES, parseBlock, parseSection } from '@/app/pages/ticket/widgets/parse'
import { blockText, CATALOG, fenced, SAMPLE_AFTER_PNG } from './widgetCatalog'
import { findTemplate, TEMPLATES, templateDigest } from './widgetTemplates'
import { checkFlow, checkImageCompare, checkTableExplorer } from './widgetTemplatesMore'

describe('widget catalog', () => {
  it('has one example per core type and per template, in that order of kinds', () => {
    expect(CATALOG.filter((c) => c.kind === 'core').map((c) => c.ref).sort()).toEqual([...CORE_TYPES].sort())
    expect(CATALOG.filter((c) => c.kind === 'template').map((c) => c.ref).sort()).toEqual(TEMPLATES.map((t) => `${t.name}@${t.version}`).sort())
    expect(CATALOG.findIndex((c) => c.kind === 'template')).toBe(CORE_TYPES.length)
  })
  it('every example parses with the strict parser, ids are unique, and template examples pin the current digest and fit the data check', () => {
    const ids = new Set<string>()
    for (const c of CATALOG) {
      const r = parseBlock(blockText(c.example))
      expect(r.reason, c.ref).toBeUndefined()
      expect(ids.has(r.spec!.id!), c.ref).toBe(false)
      ids.add(r.spec!.id!)
      if (c.kind === 'template') {
        const t = findTemplate(c.ref)!
        expect(r.spec!.sha256).toBe(templateDigest(t))
        expect(t.check?.(r.spec!.data!), c.ref).toBeUndefined()
      }
      expect(c.allowed.length, c.ref).toBeGreaterThan(20)
    }
  })
  it('the copied source is a fenced orch block that the section parser reads back', () => {
    for (const c of CATALOG) {
      const segs = parseSection(`Intro.\n\n${fenced(c.example)}\n`, 'context')
      const w = segs.find((s) => s.kind === 'widget')
      expect(w && w.kind === 'widget' && w.block.reason, c.ref).toBeUndefined()
    }
  })
})

describe('template data checks (core, before the frame)', () => {
  const obj = (v: unknown) => JSON.parse(JSON.stringify(v)) as Record<string, unknown>
  const img = (src: string) => obj({ before: { src: SAMPLE_AFTER_PNG }, after: { src } })
  it('image-compare takes inline raster data: URIs only', () => {
    expect(checkImageCompare(img(SAMPLE_AFTER_PNG))).toBeUndefined()
    for (const src of ['https://example.com/a.png', '//evil/a.png', 'javascript:alert(1)', 'data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=', 'data:text/html;base64,PGI+', 'data:image/png;base64,abc"onerror=x', 'artifact:shot.png', 'blob:null/x', 'data:image/png,raw'])
      expect(checkImageCompare(img(src)), src).toMatch(/data:image/)
    expect(checkImageCompare(obj({ before: { src: SAMPLE_AFTER_PNG } }))).toMatch(/after must be an object/)
    expect(checkImageCompare(obj({ ...img(SAMPLE_AFTER_PNG), url: 'x' }))).toMatch(/unknown key "url" in data/)
    expect(checkImageCompare(obj({ before: { src: SAMPLE_AFTER_PNG, href: 'x' }, after: { src: SAMPLE_AFTER_PNG } }))).toMatch(/unknown key "href" in before/)
  })
  it('flow refuses loops, unknown ids, duplicates and extra keys', () => {
    const nodes = [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }]
    expect(checkFlow(obj({ nodes, edges: [{ from: 'a', to: 'b' }] }))).toBeUndefined()
    expect(checkFlow(obj({ nodes, edges: [{ from: 'a', to: 'b' }, { from: 'b', to: 'a' }] }))).toMatch(/loop/)
    expect(checkFlow(obj({ nodes, edges: [{ from: 'a', to: 'z' }] }))).toMatch(/join two node ids/)
    expect(checkFlow(obj({ nodes, edges: [{ from: 'a', to: 'a' }] }))).toMatch(/two different nodes/)
    expect(checkFlow(obj({ nodes: [...nodes, { id: 'a', label: 'again' }] }))).toMatch(/used twice/)
    expect(checkFlow(obj({ nodes: [{ id: 'a', label: 'A', onclick: 'x' }] }))).toMatch(/unknown key "onclick" in a node/)
    expect(checkFlow(obj({ nodes: [{ id: 'A B', label: 'A' }] }))).toMatch(/node id must match/)
    expect(checkFlow(obj({ nodes: [] }))).toMatch(/1 to 30/)
  })
  it('table-explorer checks the shape', () => {
    expect(checkTableExplorer(obj({ columns: ['a'], rows: [[1], ['x'], [null]] }))).toBeUndefined()
    expect(checkTableExplorer(obj({ columns: ['a', 'b'], rows: [[1]] }))).toMatch(/row 1 must have 2 cells/)
    expect(checkTableExplorer(obj({ columns: ['a'], rows: [[{ x: 1 }]] }))).toMatch(/a cell in row 1/)
    expect(checkTableExplorer(obj({ columns: [], rows: [] }))).toMatch(/1 to 12/)
  })
  it('a __proto__ key in template data is an unknown key, not a prototype', () => {
    const r = parseBlock(`{"widget":"flow@1","sha256":"${templateDigest(findTemplate('flow@1')!)}","data":{"nodes":[{"id":"a","label":"A"}],"__proto__":{"edges":[]}}}`)
    expect(r.reason).toBeUndefined() // the parser keeps data opaque; the template check refuses it
    expect(checkFlow(r.spec!.data!)).toMatch(/unknown key "__proto__" in data/)
  })
})
