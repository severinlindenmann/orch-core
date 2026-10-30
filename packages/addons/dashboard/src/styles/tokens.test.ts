// #421: the light theme's tokens. Contrast is computed here from tokens.css itself, so a changed value cannot slip below the line.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const css = readFileSync(join(__dirname, 'tokens.css'), 'utf8')

function block(open: string): Record<string, string> {
  const start = css.indexOf(open)
  expect(start, open).toBeGreaterThan(-1)
  const body = css.slice(css.indexOf('{', start) + 1, css.indexOf('\n  }', start) > start && open.startsWith('  ') ? css.indexOf('\n  }', start) : css.indexOf('\n}', start))
  const out: Record<string, string> = {}
  for (const m of body.matchAll(/--([a-z0-9-]+):\s*([^;]+);/g)) out[m[1]] = m[2].trim()
  return out
}
// The dark :root holds the first block (before the shadcn names); the light set is the explicit selector.
const dark = block(':root {')
const light = block(":root[data-theme='light'] {")
const lightMedia = block("  :root:not([data-theme='dark']) {")

type C = { r: number; g: number; b: number; a: number }
function parse(v: string, theme: Record<string, string>): C {
  const ref = /^var\(--([a-z0-9-]+)\)$/.exec(v)
  if (ref) return parse(theme[ref[1]], theme)
  if (v.startsWith('#')) {
    const h = v.slice(1)
    return { r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16), b: parseInt(h.slice(4, 6), 16), a: 1 }
  }
  const n = v.match(/[\d.]+/g)!.map(Number)
  return { r: n[0], g: n[1], b: n[2], a: n[3] ?? 1 }
}
const over = (f: C, b: C): C => ({ r: f.r * f.a + b.r * (1 - f.a), g: f.g * f.a + b.g * (1 - f.a), b: f.b * f.a + b.b * (1 - f.a), a: 1 })
const lin = (c: number) => ((c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
const lum = (c: C) => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b)
const ratio = (a: C, b: C) => (Math.max(lum(a), lum(b)) + 0.05) / (Math.min(lum(a), lum(b)) + 0.05)

const tok = (t: Record<string, string>, name: string, on?: string) => {
  const c = parse(t[name], t)
  return c.a < 1 && on ? over(c, parse(t[on], t)) : c
}
/** [label, text token, background token (soft tokens are laid over `under`), under, minimum] */
const PAIRS: [string, string, string, string | undefined, number][] = [
  ['text on page', 'text', 'bg', undefined, 4.5],
  ['text on card', 'text', 'surface', undefined, 4.5],
  ['muted on page', 'text-muted', 'bg', undefined, 4.5],
  ['muted on surface-2', 'text-muted', 'surface-2', undefined, 4.5],
  ['muted on sidebar', 'text-muted', 'sidebar', undefined, 4.5],
  ['faint on page', 'text-faint', 'bg', undefined, 4.5],
  ['faint on surface-3', 'text-faint', 'surface-3', undefined, 4.5],
  ['faint on sidebar', 'text-faint', 'sidebar', undefined, 4.5],
  ['on-brand on brand (primary button)', 'on-brand', 'brand', undefined, 4.5],
  ['brand link on card', 'brand', 'surface', undefined, 4.5],
  ['brand-chip-text on brand-soft', 'brand-chip-text', 'brand-soft', 'surface', 4.5],
  ['warning on risk line (warning-soft over surface-2)', 'warning', 'warning-soft', 'surface-2', 4.5],
  ['text on risk line', 'text', 'warning-soft', 'surface-2', 4.5],
  ['danger on danger-soft', 'danger', 'danger-soft', 'surface', 4.5],
  ['success on success-soft', 'success', 'success-soft', 'surface', 4.5],
  ['info on info-soft', 'info', 'info-soft', 'surface', 4.5],
  ['addon-text on addon-soft', 'addon-text', 'addon-soft', 'surface', 4.5],
  ['disabled text on disabled bg', 'disabled-text', 'disabled-bg', undefined, 4.5],
  ['text on selected nav', 'text', 'nav-selected', undefined, 4.5],
  ['code keyword on code block', 'tok-keyword', 'surface-2', undefined, 4.5],
  ['code keyword on a removed line', 'tok-keyword', 'del-bg', 'surface-2', 4.5],
  ['code comment on code block', 'tok-comment', 'surface-2', undefined, 4.5],
  ['code string on an added line', 'tok-string', 'add-bg', 'surface-2', 4.5],
  ['input outline on card (non-text 3:1)', 'border-strong', 'surface', undefined, 3],
]

describe('light theme tokens (#421)', () => {
  it.each(PAIRS)('%s is at least its minimum contrast', (_label, fg, bg, under, min) => {
    const bgc = tok(light, bg, under)
    const fgc = tok(light, fg)
    expect(ratio(fgc.a < 1 ? over(fgc, bgc) : fgc, bgc)).toBeGreaterThanOrEqual(min)
  })

  it('the media-query block (System) carries the same values as the explicit light block', () => {
    expect(lightMedia).toEqual(light)
  })

  it('every token the light theme sets exists in the dark :root, and the new tokens exist in both', () => {
    const NEW = ['disabled-bg', 'disabled-text', 'disabled-border', 'focus-ring', 'nav-selected', 'nav-selected-border', 'brand-chip-text', 'risk-bar', 'risk-danger-bar', 'scrim', 'add-bg', 'del-bg', 'addon-text']
    for (const n of NEW) {
      expect(dark[n], 'dark ' + n).toBeDefined()
      expect(light[n], 'light ' + n).toBeDefined()
    }
    const darkAll = { ...dark, ...block(':root {\n  --tok-keyword') }
    for (const n of Object.keys(light)) expect(n in darkAll || n.startsWith('tok-') || n === 'chart-4', n).toBe(true)
  })

  it('a disabled Sign is not the primary teal, in either theme', () => {
    for (const t of [dark, light]) {
      expect(t['disabled-bg']).not.toBe(t['brand'])
      expect(ratio(tok(t, 'disabled-bg'), tok(t, 'brand'))).toBeGreaterThan(1.3)
    }
  })

  it('light borders and focus are the strengthened ones', () => {
    expect(light['border']).toBe('#d6cfc2')
    expect(light['focus-ring']).toContain('#1c1917')
  })
})
