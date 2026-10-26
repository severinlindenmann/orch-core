import { readFileSync } from 'node:fs'
import { resolve as resolvePath } from 'node:path'
import { describe, expect, it } from 'vitest'

// Every non-test source file, read as text by Vite (keys look like "/src/app/router.tsx").
const RAW = import.meta.glob<string>(['/src/**/*.{ts,tsx}', '!/src/**/*.test.{ts,tsx}'], { query: '?raw', import: 'default', eager: true })
const FILES = Object.entries(RAW).map(([key, text]) => ({ file: key.replace('/src/', ''), text }))

describe('layout guard (the real 1024 px check is the browser pass)', () => {
  // The app is desktop-only from 1024 px: nothing may force the page wider than that.
  const FIXED = /(?<![\w-])(?:[a-z0-9-]+:)*(min-w|w)-\[(\d+(?:\.\d+)?)(px|rem)\]/g
  it('no className sets a fixed width or min-width above 1024 px', () => {
    const hits: string[] = []
    for (const { file, text } of FILES) {
      for (const m of text.matchAll(FIXED)) {
        const px = Number(m[2]) * (m[3] === 'rem' ? 16 : 1)
        if (px > 1024) hits.push(`${file}: ${m[0]} (${px}px)`)
      }
    }
    expect(hits).toEqual([])
  })
})

describe('orange guard (orange is reserved for addons)', () => {
  // Nothing outside src/addon-ui may spell the addon orange; addon-marker classes come from addon-ui/addonClasses.ts.
  const ORANGE = [
    /(?<![\w-])(?:[a-z0-9-]+:)*[a-z]+-orange-\d/, // Tailwind palette: bg-orange-500, text-orange-300 ...
    /(?<![\w-])(?:[a-z0-9-]+:)*(?:bg|text|border|ring|fill|stroke|outline|from|to|via|shadow|divide|decoration|accent|caret)-(?:on-)?addon(?:-soft|-border)?(?![\w-])/, // the addon token as a utility
    /var\(--(?:on-)?addon/, // the token used directly
    /#f07a2e/i,
    /240,\s*122,\s*46/,
  ]
  it('no file outside src/addon-ui uses the addon orange', () => {
    const hits: string[] = []
    for (const { file, text } of FILES) {
      if (file.startsWith('addon-ui/')) continue
      text.split('\n').forEach((line, i) => {
        if (ORANGE.some((re) => re.test(line))) hits.push(`${file}:${i + 1}: ${line.trim().slice(0, 120)}`)
      })
    }
    expect(hits).toEqual([])
  })
  it('no --chart-* token resolves to the addon orange', () => {
    const css = readFileSync(resolvePath(process.cwd(), 'src/styles/tokens.css'), 'utf8')
    const tokens = new Map([...css.matchAll(/--([a-z0-9-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].replace(/\/\*.*?\*\//g, '').trim().toLowerCase()]))
    const resolve = (v: string, depth = 0): string => {
      const m = /^var\(--([a-z0-9-]+)\)$/.exec(v)
      return m && depth < 10 && tokens.has(m[1]) ? resolve(tokens.get(m[1])!, depth + 1) : v
    }
    const orange = resolve('var(--addon)')
    expect(orange).toBe('#f07a2e')
    const charts = [...tokens.keys()].filter((k) => /^chart-\d+$/.test(k))
    expect(charts.length).toBeGreaterThanOrEqual(4)
    for (const k of charts) {
      const v = resolve(tokens.get(k)!)
      expect(v, k).not.toBe(orange)
      expect(v, k).not.toMatch(/240,\s*122,\s*46/)
    }
  })
})

describe('refusal guard (refusals are 4xx with a code, never a success)', () => {
  // A sentence that says the request was not honoured must come back as a StoreFailure (refusal/notFound/invalid/conflict).
  const REFUSAL = /ok: true, message: [`'"](?:No such|That [^`'"]* (?:no longer|is closed)|Pick |Only |Write |Choose |Nothing to|[^`'"]* is not |[^`'"]* already (?:has|exists|merged))/
  it('no addon module returns ok:true with a refusal sentence', () => {
    const hits: string[] = []
    for (const { file, text } of FILES) {
      if (!file.startsWith('mocks/')) continue
      text.split('\n').forEach((line, i) => {
        if (REFUSAL.test(line)) hits.push(`${file}:${i + 1}: ${line.trim().slice(0, 140)}`)
      })
    }
    expect(hits).toEqual([])
  })
})

describe('ticket visibility guard (one rule for addon modules)', () => {
  it('addon modules ask canSeeTicket, never store.isVisible directly', () => {
    const hits: string[] = []
    for (const { file, text } of FILES) {
      if (!file.startsWith('mocks/addons/') || file === 'mocks/addons/registry.ts') continue
      text.split('\n').forEach((line, i) => {
        if (/\.isVisible\(/.test(line)) hits.push(`${file}:${i + 1}: ${line.trim().slice(0, 140)}`)
      })
    }
    expect(hits).toEqual([])
  })
})
