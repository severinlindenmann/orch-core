import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { blockText, CATALOG } from '@/api/widgetCatalog'
import { findTemplate, templateDigest } from '@/api/widgetTemplates'
import { widgetText } from './CoreWidget'
import { parseBlock, type Block } from './parse'
import { WidgetBlock } from './WidgetBlock'

const NO_TICKET = { key: 'DEMO-1', artifacts: [] }
const blockOf = (raw: string): Block => ({ section: 'context', line: 1, raw, ...parseBlock(raw) })
const draw = (raw: string, agentHtml = true) => render(<WidgetBlock block={blockOf(raw)} ticket={NO_TICKET} agentHtml={agentHtml} sectionLabel="Context" />)
const example = (ref: string) => blockText(CATALOG.find((c) => c.ref === ref)!.example)

describe('newer core types are drawn by core, without a frame, with words as well as colour', () => {
  it('every core example draws without a frame and has a text alternative', () => {
    for (const c of CATALOG.filter((x) => x.kind === 'core')) {
      const { container, unmount } = draw(blockText(c.example))
      expect(container.querySelector('[data-state="refused"]'), c.ref).toBeNull()
      expect(container.querySelector('iframe'), c.ref).toBeNull()
      expect(container.querySelector('[data-addon]'), c.ref).toBeNull()
      expect(widgetText(parseBlock(blockText(c.example)).spec!).trim().length, c.ref).toBeGreaterThan(5)
      unmount()
    }
  })
  it('stats show value, the change and the role in words', () => {
    const { container } = draw(example('stats'))
    expect(screen.getByText('Tables loaded')).toBeInTheDocument()
    expect(screen.getByText('+9 since 09:00')).toBeInTheDocument()
    expect(screen.getByText('−12')).toBeInTheDocument()
    expect(screen.getByText('OK')).toBeInTheDocument()
    expect(screen.getByText('Warning')).toBeInTheDocument()
    expect(container.textContent).toContain('31 of 40')
  })
  it('series draws one polyline per run of values (a null is a gap), a marker and a legend', () => {
    const { container } = draw(example('series'))
    const seed = container.querySelector('[data-series="seed"]')!
    expect(seed.querySelectorAll('polyline')).toHaveLength(2) // 10-06 is null
    expect(seed.querySelector('polyline')!.getAttribute('class')).toMatch(/stroke-chart-1/)
    expect(container.querySelector('[data-series="test"] polyline')!.getAttribute('class')).toMatch(/stroke-chart-2/)
    expect(container.querySelector('[data-marker="typed columns"]')).toBeTruthy()
    expect(container.querySelector('svg title')!.textContent).toMatch(/seed: 10-02 61 s.*10-06 no value/)
    expect(screen.getAllByRole('listitem').map((li) => li.textContent)).toEqual(['seed', 'test'])
  })
  it('series in the widgets.md points shape draws one line over numeric x, and never uses the danger colour for a series', () => {
    const { container } = draw(JSON.stringify({ type: 'series', unit: 'ms', points: [[1, 120], [2, 140], [5, 90]], markers: [{ x: 2, label: 'cache on' }] }))
    expect(container.querySelectorAll('[data-series] polyline')).toHaveLength(1)
    expect(container.querySelector('[data-marker="cache on"]')).toBeTruthy()
    expect(container.innerHTML).not.toMatch(/chart-5/)
  })
  it('a series at the edge of the allowed range draws promptly; a degenerate one is refused promptly', () => {
    const t0 = performance.now()
    const edge = draw(JSON.stringify({ type: 'series', points: [[1, -1e15], [2, 1e15]] }))
    expect(edge.container.querySelectorAll('[data-series] polyline')).toHaveLength(1)
    const bad = draw(JSON.stringify({ type: 'series', points: [[1, -300000000000000000], [2, -299999999999999936]] }))
    expect(bad.container.querySelector('[data-state="refused"]')).toBeTruthy()
    expect(performance.now() - t0).toBeLessThan(2000)
  })
  it('progress (proposed) segments are sized against max and named with their state', () => {
    const { container } = draw(example('progress'))
    const segs = [...container.querySelectorAll<HTMLElement>('[data-segment]')]
    expect(segs.map((s) => s.style.width)).toEqual(['25%', '50%', '25%'])
    expect(screen.getByRole('img', { name: /Tasks: Done: 1, Blocked on Q2: 2, Not started: 1; 4 of 4/ })).toBeInTheDocument()
    expect(screen.getByText('(Warning)')).toBeInTheDocument()
  })
  it('gates and timeline carry a glyph and a word per state', () => {
    draw(example('gates'))
    for (const w of ['Passed', 'Failed', 'Skipped', 'Running']) expect(screen.getAllByText(w).length).toBeGreaterThan(0)
    expect(screen.getByText('4 min 12 s')).toBeInTheDocument()
    draw(example('timeline'))
    expect(screen.getAllByText('Done')).toHaveLength(2)
    expect(screen.getByText('Now')).toBeInTheDocument()
    expect(screen.getByText('Next')).toBeInTheDocument()
  })
  it('diff marks added and removed lines for screen readers too', () => {
    const { container } = draw(example('diff'))
    expect(container.querySelectorAll('[data-line="add"]')).toHaveLength(2)
    expect(container.querySelectorAll('[data-line="del"]')).toHaveLength(1)
    expect(container.querySelector('[data-line="del"]')).toHaveTextContent(/^removed: -/)
    expect(screen.getByText('models/fct_usage_forecast.sql')).toBeInTheDocument()
  })
  it('callout says its role in words; its text is never markup', () => {
    const { container } = draw(JSON.stringify({ type: 'callout', role: 'warn', text: '<img src=x onerror=alert(1)> **not bold**' }))
    expect(container.querySelector('[data-callout="warn"]')).toHaveTextContent('Warning: <img src=x onerror=alert(1)> **not bold**')
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('strong')).toBeNull()
  })
  it('spark sits inside its sentence, at {spark}', () => {
    const { container } = draw(example('spark'))
    const p = container.querySelector('p')!
    expect(p.textContent).toMatch(/^CI time over the last 12 runs\s*\d.*now 6\.1 min\.$/s)
    expect(p.querySelector('svg[role="img"] title')!.textContent).toMatch(/min 6\.1, max 9\.1/)
  })
  it('callout note is shown as Info', () => {
    const { container } = draw(JSON.stringify({ type: 'callout', role: 'note', text: 'x' }))
    expect(container.querySelector('[data-callout="info"]')).toHaveTextContent('Info: x')
  })
})

describe('templates: core checks the data before the frame (fail closed)', () => {
  const pin = templateDigest(findTemplate('image-compare@1')!)
  it('an image-compare block with a link instead of a data: image is refused, and no frame is drawn', async () => {
    const raw = JSON.stringify({ widget: 'image-compare@1', sha256: pin, id: 'shots', data: { before: { src: 'https://example.com/a.png' }, after: { src: 'https://example.com/b.png' } } })
    const { container } = draw(raw)
    expect(container.querySelector('iframe')).toBeNull()
    expect(within(container).getByRole('alert')).toHaveTextContent("This widget's data does not fit the image compare template, so it is not shown.")
    await userEvent.click(within(container).getByRole('button', { name: 'Details' }))
    expect(container).toHaveTextContent('data does not fit image-compare@1: before.src must be a data:image')
  })
  it('the gallery example draws as a sandboxed frame with the data as inert JSON', () => {
    const { container } = draw(example('image-compare@1'))
    const f = container.querySelector('iframe')!
    expect(f).toHaveAttribute('sandbox', 'allow-scripts')
    expect(f.srcdoc).toContain("img-src data:")
    expect(f.srcdoc).toContain('data:image/png;base64,')
    expect(container.querySelector('[data-addon="widgets"]')).toBeTruthy()
  })
  it('flow and table-explorer examples draw; a looping flow is refused', () => {
    expect(draw(example('flow@1')).container.querySelector('iframe')).toBeTruthy()
    expect(draw(example('table-explorer@1')).container.querySelector('iframe')).toBeTruthy()
    const loop = JSON.stringify({ widget: 'flow@1', sha256: templateDigest(findTemplate('flow@1')!), data: { nodes: [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }], edges: [{ from: 'a', to: 'b' }, { from: 'b', to: 'a' }] } })
    const { container } = draw(loop)
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.querySelector('[data-state="refused"]')).toBeTruthy()
  })
})
