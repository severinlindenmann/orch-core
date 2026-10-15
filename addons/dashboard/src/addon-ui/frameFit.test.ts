import { describe, expect, it } from 'vitest'
import { frameDocument } from '@/api/widgetTemplates'
import { FIT_START, LOOP_MS, MIN_FIT, QUIET_MS, settleFit, stepFit, type FitState } from './FrameNode'

// Owner bug report G1 #3: a sandboxed chart (DEMO-0219 "Duplicates per daily build") drew big, then shrank again and
// again. The frame took the height its document reported as its own border-box height (2 px of it border), and core's
// wrapper capped the chart at the frame's own viewport (100vh): every round the report came back 2 px smaller.

const BORDER = 2 // the iframe's 1 px border, top and bottom

/**
 * Plays a document whose content height is `content(viewport)` against the frame, the way SandboxFrame does: the
 * report plus the frame's border goes to stepFit, and after a quiet spell settleFit runs. Returns the heights applied.
 */
function play(content: (viewport: number) => number, rounds: number, max = 280, msPerRound = 16) {
  let s: FitState = FIT_START
  const applied: number[] = []
  let now = 0
  let lastReport: number | null = null
  for (let i = 0; i < rounds; i++) {
    const viewport = (s.fit ?? max) - BORDER
    const report = Math.ceil(content(viewport))
    // The size reporter only posts a changed height.
    let next = s
    if (report !== lastReport) {
      next = stepFit(s, report + BORDER, max, now)
      lastReport = report
    } else next = settleFit(s, max, now)
    if ((next.fit ?? max) !== (s.fit ?? max)) applied.push(next.fit!) // what the frame shows changed
    s = next
    now += msPerRound
  }
  return { s, applied, visible: (s.fit ?? max) - BORDER, content: Math.ceil(content((s.fit ?? max) - BORDER)) }
}

describe('fitted frame: no resize feedback loop', () => {
  it("core's wrapper caps a chart at a fixed height, never at the frame's own viewport", () => {
    const doc = frameDocument('<svg></svg>', {}, 280)
    expect(doc).not.toMatch(/\d\s*vh|100%\s*-|vh\s*-/)
    expect(doc).toContain('svg{max-height:262px}') // 280 minus the frame's border (2) and the body's padding (16)
    expect(frameDocument('<svg></svg>', {}, 720)).toContain('svg{max-height:702px}')
  })

  it('a document sized from its viewport is a fixed point: no resize, and the content fits the visible area', () => {
    const r = play((vp) => vp, 200)
    expect(r.applied).toEqual([])
    expect(r.visible).toBeGreaterThanOrEqual(r.content)
  })

  it('a stable document fits once, exactly: content = visible area', () => {
    const r = play(() => 190, 50)
    expect(r.applied).toEqual([192])
    expect(r.visible).toBe(190)
  })

  it('backstop: a document that shrinks with the viewport stops after a bounded number of resizes, never tiny', () => {
    for (const f of [(vp: number) => vp - 40, (vp: number) => vp * 0.9]) {
      const r = play(f, 400)
      expect(r.applied.length).toBeLessThanOrEqual(6)
      expect(r.s.fit).toBeGreaterThanOrEqual(200)
      expect(Math.min(...r.applied)).toBeGreaterThan(MIN_FIT)
      expect(r.s.confirmed).toBe(true)
    }
  })

  it('backstop: a document that flips between two heights (a scrollbar on and off) settles on the larger one', () => {
    const r = play((vp) => (vp >= 200 ? 150 : 220), 400)
    expect(r.applied.length).toBeLessThanOrEqual(4)
    expect(r.s.fit).toBe(222)
    expect(r.s.confirmed).toBe(true)
  })

  it('a shrink that settles in steps ends at its final height (after a quiet spell)', () => {
    let s = stepFit(FIT_START, 200, 280, 0)
    s = stepFit(s, 150, 280, 100)
    s = stepFit(s, 120, 280, 200) // looks like a loop for now: held
    expect(s.frozen).toBe(true)
    expect(settleFit(s, 280, 200 + QUIET_MS - 1)).toBe(s) // not quiet yet
    s = settleFit(s, 280, 200 + QUIET_MS)
    expect(s.fit).toBe(120)
    // The document stays at 120: nothing answers, and a much later change is a normal change again.
    s = stepFit(s, 100, 280, 200 + QUIET_MS + LOOP_MS + 5000)
    expect(s.fit).toBe(100)
  })

  it('real changes still apply: content that grows, and a single shrink (a filter that hides rows)', () => {
    let s = stepFit(FIT_START, 200, 280, 0)
    s = stepFit(s, 260, 280, 1000)
    expect(s.fit).toBe(260)
    s = stepFit(s, 300, 280, 2000)
    expect(s.fit).toBe(280) // never past the frame's height
    s = stepFit(s, 150, 280, 5000)
    expect(s.fit).toBe(150)
    s = stepFit(s, 151, 280, 5100) // under 2 px: ignored
    expect(s.fit).toBe(150)
  })
})
