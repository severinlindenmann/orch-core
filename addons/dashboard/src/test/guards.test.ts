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
  // Files that legitimately draw the addon marker (orange hairline / soft fill) around addon-owned content.
  const ADDON_MARKER_OWNERS = new Set([
    'app/pages/board/AddonLane.tsx', // the board lane an addon contributes
    'app/pages/ticket/Artifacts.tsx', // artifacts produced by an addon
    'app/pages/today/cards.tsx', // the addon-requested confirmation card, inside AddonFrame
  ])
  const ORANGE = [
    /(?<![\w-])(?:[a-z0-9-]+:)*[a-z]+-orange-\d/, // Tailwind palette: bg-orange-500, text-orange-300 ...
    /(?<![\w-])(?:[a-z0-9-]+:)*(?:bg|text|border|ring|fill|stroke|outline|from|to|via|shadow|divide|decoration|accent|caret)-(?:on-)?addon(?:-soft|-border)?(?![\w-])/, // the addon token as a utility
    /var\(--(?:on-)?addon/, // the token used directly
    /chart-4/, // --chart-4 is the addon orange
    /#f07a2e/i,
    /240,\s*122,\s*46/,
  ]
  it('no file outside src/addon-ui and the addon-marker owners uses the addon orange', () => {
    const hits: string[] = []
    for (const { file, text } of FILES) {
      if (file.startsWith('addon-ui/') || ADDON_MARKER_OWNERS.has(file)) continue
      text.split('\n').forEach((line, i) => {
        if (ORANGE.some((re) => re.test(line))) hits.push(`${file}:${i + 1}: ${line.trim().slice(0, 120)}`)
      })
    }
    expect(hits).toEqual([])
  })
  it('the whitelist only lists files that exist and really use the addon orange', () => {
    for (const f of ADDON_MARKER_OWNERS) {
      const hit = FILES.find((x) => x.file === f)
      expect(hit, `${f} exists`).toBeDefined()
      expect(ORANGE.some((re) => re.test(hit!.text)), `${f} uses it`).toBe(true)
    }
  })
})
