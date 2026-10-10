import { describe, expect, it } from 'vitest'
import { frameDocument } from '@/api/widgetTemplates'
import { FIT_START, MIN_FIT, stepFit, type FitState } from './FrameNode'

// Owner bug report G1 #3: a sandboxed chart (DEMO-0219 "Duplicates per daily build") drew big, then shrank again and
// again. The frame took the height its document reported, and core's wrapper capped the chart at the frame's own
// viewport (100vh): every round the report came back 2 px (the frame's border) smaller.

/** Plays `rounds` reports of a document whose content height is `content(viewport)`; returns the heights applied. */
function play(content: (viewport: number) => number, rounds: number, max = 280, msPerRound = 16) {
  let s: FitState = FIT_START
  const applied: number[] = []
  for (let i = 0; i < rounds; i++) {
    const viewport = (s.fit ?? max) - 2 // the iframe's 1 px border on each side
    const next = stepFit(s, content(viewport), max, i * msPerRound)
    if (next.fit !== s.fit) applied.push(next.fit!)
    s = next
  }
  return { s, applied }
}

describe('fitted frame: no resize feedback loop', () => {
  it("core's wrapper caps a chart at a fixed height, never at the frame's own viewport", () => {
    const doc = frameDocument('<svg></svg>', {}, 280)
    expect(doc).not.toMatch(/\d\s*vh|100%\s*-|vh\s*-/)
    expect(doc).toContain('svg{max-height:262px}') // 280 minus the frame's border (2) and the body's padding (16)
    expect(frameDocument('<svg></svg>', {}, 720)).toContain('svg{max-height:702px}')
  })

  it('a document that sizes itself from the viewport stops shrinking after a bounded number of resizes', () => {
    // Exactly the old wrapper: svg max-height = 100vh - 16, body padding 16 → content = viewport.
    const { s, applied } = play((vp) => vp, 200)
    expect(applied.length).toBeLessThanOrEqual(4)
    expect(s.fit).toBeGreaterThanOrEqual(270)
    expect(Math.min(...applied)).toBeGreaterThan(MIN_FIT)
  })

  it('also when each round shrinks by a lot, or by a share of the viewport (height: 90vh)', () => {
    expect(play((vp) => vp - 40, 200).s.fit).toBeGreaterThanOrEqual(200)
    expect(play((vp) => vp * 0.9, 200).s.fit).toBeGreaterThanOrEqual(200)
  })

  it('a document with a stable height fits once and stays', () => {
    const { s, applied } = play(() => 190, 50)
    expect(applied).toEqual([190])
    expect(s.fit).toBe(190)
  })

  it('real changes still apply: content that grows, and a single shrink (a filter that hides rows)', () => {
    let s = stepFit(FIT_START, 200, 280, 0)
    s = stepFit(s, 260, 280, 100)
    expect(s.fit).toBe(260)
    s = stepFit(s, 300, 280, 200)
    expect(s.fit).toBe(280) // never past the frame's height
    s = stepFit(s, 150, 280, 5000)
    expect(s.fit).toBe(150)
    s = stepFit(s, 151, 280, 5100) // under 2 px: ignored
    expect(s.fit).toBe(150)
  })
})
